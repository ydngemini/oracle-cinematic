"""Approval-gated EMAIL, CALL, and CALENDAR command router."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import ws_hub
from approval_service import create_approval, decide_approval, list_approvals
from audit_ledger import AuditCategory, ledger
from automation_jobs import enqueue_job, register_handler
from command_providers import (
    ProviderConfigurationError,
    create_google_calendar_event,
    place_twilio_call,
    send_ses_email,
    verify_twilio_signature,
)
from crypto import decrypt_pii, derive_tenant_key, encrypt_pii
from db.connection import tenant_tx
from intelligence_engine import negotiation_guidance
from outreach_compliance import Channel, guard_outreach
from platform_policy import (
    ActionRisk,
    Feature,
    enforce_public_property_data,
    require_feature,
)
from tenancy import Role, TenantContext, require_context, require_role

router = APIRouter(prefix="/api/commands", tags=["commands"])


class CommandType(str, Enum):
    EMAIL = "EMAIL"
    CALL = "CALL"
    CALENDAR = "CALENDAR"


_RISK = {
    CommandType.EMAIL: ActionRisk.OUTREACH,
    CommandType.CALL: ActionRisk.LIVE_CALL,
    CommandType.CALENDAR: ActionRisk.CALENDAR_WRITE,
}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class ClassificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=2, max_length=4_000)


class CommandCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_type: CommandType
    target: dict[str, Any]
    draft: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=240)
    scheduled_at: Optional[datetime] = None
    context: dict[str, Any] = Field(default_factory=dict)
    approval_expires_minutes: int = Field(default=1_440, ge=5, le=10_080)

    @model_validator(mode="after")
    def validate_channel_payload(self) -> "CommandCreate":
        _validate_command_payload(self.command_type, self.target, self.draft)
        if self.scheduled_at and self.scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone")
        return self


class CommandEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: dict[str, Any]
    draft: dict[str, Any]
    scheduled_at: Optional[datetime] = None
    context: dict[str, Any] = Field(default_factory=dict)
    approval_expires_minutes: int = Field(default=1_440, ge=5, le=10_080)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=8, max_length=500)


class CallTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    transcript_excerpt: str = Field(min_length=1, max_length=2_000)
    counter_offer: float = Field(ge=0, le=1_000_000_000)
    arv: float = Field(gt=0, le=1_000_000_000)
    rehab: float = Field(ge=0, le=1_000_000_000)
    acquisition_ratio: float = Field(default=0.70, gt=0, le=1)
    amber_tolerance: float = Field(default=0.05, ge=0, le=0.50)


class CallConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    consent_recorded: bool
    consent_basis: str = Field(min_length=8, max_length=500)


class ProviderCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    account_label: str = Field(default="default", min_length=1, max_length=120)
    token: str = Field(min_length=8, max_length=20_000)
    refresh_token: Optional[str] = Field(default=None, min_length=8, max_length=20_000)
    scopes: list[str] = Field(default_factory=list, max_length=100)
    expires_at: Optional[datetime] = None


_COMMAND_PROVIDERS = {"google", "twilio", "ses"}


def _provider_key(tenant_id: str) -> str:
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise HTTPException(status_code=503, detail="Provider credential encryption is not configured.")
    return derive_tenant_key(tenant_id, master)


async def _load_provider_credential(
    ctx: TenantContext,
    provider: str,
    *,
    account_label: Optional[str] = None,
) -> Any:
    """Decrypt a tenant credential only inside the executing worker."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT token_ciphertext,expires_at FROM provider_credentials
             WHERE tenant_id=$1::uuid AND provider=$2 AND disabled_at IS NULL
               AND ($3::text IS NULL OR account_label IN ($3,'default'))
             ORDER BY CASE WHEN account_label=$3 THEN 0 ELSE 1 END, updated_at DESC
             LIMIT 1
            """,
            ctx.tenant_id,
            provider,
            account_label,
        )
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc):
            return None
        return await decrypt_pii(conn, row["token_ciphertext"], _provider_key(ctx.tenant_id))


def _validate_command_payload(
    command_type: CommandType,
    target: dict[str, Any],
    draft: dict[str, Any],
) -> None:
    enforce_public_property_data({"target": target, "draft": draft})
    if command_type is CommandType.EMAIL:
        email = str(target.get("email") or "").strip()
        if not _EMAIL_RE.match(email):
            raise ValueError("EMAIL target requires a valid email")
        if not str(draft.get("subject") or "").strip():
            raise ValueError("EMAIL draft requires subject")
        if not str(draft.get("body") or "").strip():
            raise ValueError("EMAIL draft requires body")
        if len(str(draft["subject"])) > 200 or len(str(draft["body"])) > 20_000:
            raise ValueError("EMAIL draft exceeds size limits")
        return
    if command_type is CommandType.CALL:
        phone = str(target.get("phone") or "").strip()
        if not _E164_RE.match(phone):
            raise ValueError("CALL target requires an E.164 phone number")
        if not target.get("lead_id") and not target.get("client_id"):
            raise ValueError("CALL target must reference a lead or client")
        for key in ("lead_id", "client_id"):
            if target.get(key):
                try:
                    uuid.UUID(str(target[key]))
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ValueError(f"CALL target {key} must be a UUID") from exc
        state_code = str(target.get("state_code") or "").upper()
        if len(state_code) != 2:
            raise ValueError("CALL target requires a two-letter state_code")
        return
    event = draft.get("event")
    if not isinstance(event, dict):
        raise ValueError("CALENDAR draft requires an event object")
    if not event.get("summary") or not event.get("start") or not event.get("end"):
        raise ValueError("CALENDAR event requires summary, start, and end")


def _execution_payload(
    *,
    command_id: str,
    command_type: CommandType,
    target: dict[str, Any],
    draft: dict[str, Any],
    context: dict[str, Any],
    tenant_id: str,
) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "command_type": command_type.value,
        "target": target,
        "draft": draft,
        "context": context,
        "tenant_id": tenant_id,
    }


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _command_dict(row: Any) -> dict[str, Any]:
    value = dict(row)
    for field in ("target", "draft"):
        value[field] = _decode(value.get(field))
    for key, item in list(value.items()):
        if isinstance(item, uuid.UUID):
            value[key] = str(item)
        elif isinstance(item, datetime):
            value[key] = item.isoformat()
    return value


async def _get_command(ctx: TenantContext, command_id: str, *, lock: bool = False):
    suffix = " FOR UPDATE" if lock else ""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM command_executions WHERE id=$1::uuid{suffix}", command_id
        )
    return row


@router.post("/classify")
async def classify_command(
    body: ClassificationRequest,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    text = body.text.strip().lower()
    if re.match(r"^(email|e-mail|send (an )?email)\b", text):
        command_type = CommandType.EMAIL
    elif re.match(r"^(call|phone|dial)\b", text):
        command_type = CommandType.CALL
    elif re.match(r"^(calendar|schedule|book|set up (a )?meeting)\b", text):
        command_type = CommandType.CALENDAR
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Command must explicitly request EMAIL, CALL, or CALENDAR.",
        )
    return {
        "command_type": command_type.value,
        "risk_class": _RISK[command_type].value,
        "execution": "editable_approval_draft",
        "classification_basis": "explicit_command_verb",
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_command(
    body: CommandCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    enforce_public_property_data(body.context)
    async with tenant_tx(ctx) as conn:
        existing = await conn.fetchrow(
            """
            SELECT * FROM command_executions
            WHERE tenant_id=$1::uuid AND idempotency_key=$2
            """,
            ctx.tenant_id,
            body.idempotency_key,
        )
    if existing:
        return {"command": _command_dict(existing), "created": False}

    command_id = str(uuid.uuid4())
    execution = _execution_payload(
        command_id=command_id,
        command_type=body.command_type,
        target=body.target,
        draft=body.draft,
        context=body.context,
        tenant_id=ctx.tenant_id,
    )
    approval = await create_approval(
        ctx,
        action_type=f"command:{body.command_type.value}",
        risk=_RISK[body.command_type],
        target_type="command",
        target_id=command_id,
        draft_payload=execution,
        expires_in_minutes=body.approval_expires_minutes,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO command_executions (
                id, tenant_id, command_type, classification, risk_class,
                target, draft, state, approval_id, idempotency_key,
                scheduled_at, created_by
            ) VALUES (
                $1::uuid,$2::uuid,$3,$4,$5,$6::jsonb,$7::jsonb,
                'awaiting_approval',$8::uuid,$9,$10,$11
            )
            ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
               SET updated_at=command_executions.updated_at
            RETURNING *
            """,
            command_id,
            ctx.tenant_id,
            body.command_type.value,
            f"explicit_{body.command_type.value.lower()}",
            _RISK[body.command_type].value,
            json.dumps(body.target),
            json.dumps({"content": body.draft, "context": body.context}),
            str(approval["id"]),
            body.idempotency_key,
            body.scheduled_at,
            ctx.agent_id,
        )
    return {"command": _command_dict(row), "approval": approval, "created": True}


@router.get("")
async def commands(
    state_filter: Optional[str] = Query(default=None, alias="state"),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    async with tenant_tx(ctx) as conn:
        if state_filter:
            rows = await conn.fetch(
                """
                SELECT * FROM command_executions
                WHERE state=$1 ORDER BY created_at DESC LIMIT $2
                """,
                state_filter,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM command_executions ORDER BY created_at DESC LIMIT $1",
                limit,
            )
    return {"commands": [_command_dict(row) for row in rows]}


@router.get("/approvals")
async def approvals(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    return {"approvals": await list_approvals(ctx, status_filter=status_filter, limit=limit)}


@router.get("/providers")
async def provider_status(ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.AUTOMATION)
    async with tenant_tx(ctx) as conn:
        if ctx.role in {Role.BROKER_OWNER, Role.PLATFORM_ADMIN}:
            rows = await conn.fetch(
                """
                SELECT id,provider,account_label,scopes,expires_at,last_validated_at,
                       disabled_at,created_by,created_at,updated_at
                  FROM provider_credentials
                 WHERE provider=ANY($1::text[]) ORDER BY provider,account_label
                """,
                sorted(_COMMAND_PROVIDERS),
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id,provider,account_label,scopes,expires_at,last_validated_at,
                       disabled_at,created_by,created_at,updated_at
                  FROM provider_credentials
                 WHERE provider='google' AND account_label=$1
                 ORDER BY updated_at DESC
                """,
                ctx.agent_id,
            )
    return {
        "providers": [
            {
                **dict(row),
                "id": str(row["id"]),
                **{
                    key: row[key].isoformat() if row[key] else None
                    for key in ("expires_at", "last_validated_at", "disabled_at", "created_at", "updated_at")
                },
                "credential_exposed": False,
            }
            for row in rows
        ]
    }


@router.put("/providers/{provider}")
async def store_provider_credential(
    provider: str,
    body: ProviderCredentialInput,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    provider = provider.lower()
    if provider not in _COMMAND_PROVIDERS:
        raise HTTPException(status_code=422, detail="Unsupported command provider.")
    if ctx.role is Role.AGENT:
        if provider != "google" or body.account_label != ctx.agent_id:
            raise HTTPException(
                status_code=403,
                detail="Agents may manage only their own Google OAuth credential.",
            )
    else:
        require_role(ctx, Role.BROKER_OWNER)
    key = _provider_key(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        encrypted = await encrypt_pii(conn, body.token, key)
        refresh = await encrypt_pii(conn, body.refresh_token, key) if body.refresh_token else None
        row = await conn.fetchrow(
            """
            INSERT INTO provider_credentials (
                tenant_id,provider,account_label,token_ciphertext,
                refresh_ciphertext,scopes,expires_at,last_validated_at,created_by
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6::text[],$7,now(),$8)
            ON CONFLICT (tenant_id,provider,account_label) DO UPDATE SET
                token_ciphertext=EXCLUDED.token_ciphertext,
                refresh_ciphertext=EXCLUDED.refresh_ciphertext,
                scopes=EXCLUDED.scopes,expires_at=EXCLUDED.expires_at,
                last_validated_at=now(),disabled_at=NULL,updated_at=now()
            RETURNING id,provider,account_label,scopes,expires_at,last_validated_at
            """,
            ctx.tenant_id,
            provider,
            body.account_label,
            encrypted,
            refresh,
            body.scopes,
            body.expires_at,
            ctx.agent_id,
        )
    return {
        **dict(row),
        "id": str(row["id"]),
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "last_validated_at": row["last_validated_at"].isoformat(),
        "credential_exposed": False,
    }


@router.delete("/providers/{provider}/{account_label}")
async def disable_provider_credential(
    provider: str,
    account_label: str,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    provider = provider.lower()
    if ctx.role is Role.AGENT:
        if provider != "google" or account_label != ctx.agent_id:
            raise HTTPException(
                status_code=403,
                detail="Agents may disable only their own Google OAuth credential.",
            )
    else:
        require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE provider_credentials SET disabled_at=now()
             WHERE provider=$1 AND account_label=$2 AND disabled_at IS NULL
            RETURNING id
            """,
            provider,
            account_label,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Provider credential not found.")
    return {"disabled": True, "id": str(row["id"])}


@router.get("/{command_id}")
async def command_detail(command_id: str, ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.AUTOMATION)
    row = await _get_command(ctx, command_id)
    if not row:
        raise HTTPException(status_code=404, detail="Command not found.")
    return _command_dict(row)


@router.put("/{command_id}")
async def edit_command(
    command_id: str,
    body: CommandEdit,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM command_executions WHERE id=$1::uuid FOR UPDATE", command_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Command not found.")
        if row["state"] not in {"draft", "awaiting_approval"}:
            raise HTTPException(status_code=409, detail="Command can no longer be edited.")
        command_type = CommandType(row["command_type"])
        try:
            _validate_command_payload(command_type, body.target, body.draft)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await conn.execute(
            """
            UPDATE action_approvals
               SET status='revoked', decided_by=$2, decided_at=now(),
                   reason='Draft edited; prior approval payload revoked.'
             WHERE id=$1 AND status='pending'
            """,
            row["approval_id"],
            ctx.agent_id,
        )

    execution = _execution_payload(
        command_id=command_id,
        command_type=command_type,
        target=body.target,
        draft=body.draft,
        context=body.context,
        tenant_id=ctx.tenant_id,
    )
    approval = await create_approval(
        ctx,
        action_type=f"command:{command_type.value}",
        risk=_RISK[command_type],
        target_type="command",
        target_id=command_id,
        draft_payload=execution,
        expires_in_minutes=body.approval_expires_minutes,
    )
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchrow(
            """
            UPDATE command_executions
               SET target=$2::jsonb, draft=$3::jsonb, approval_id=$4::uuid,
                   scheduled_at=$5, state='awaiting_approval', updated_at=now()
             WHERE id=$1::uuid
            RETURNING *
            """,
            command_id,
            json.dumps(body.target),
            json.dumps({"content": body.draft, "context": body.context}),
            str(approval["id"]),
            body.scheduled_at,
        )
    return {"command": _command_dict(updated), "approval": approval}


@router.post("/{command_id}/approve")
async def approve_command(
    command_id: str,
    body: ApprovalDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    row = await _get_command(ctx, command_id)
    if not row:
        raise HTTPException(status_code=404, detail="Command not found.")
    if row["state"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"Command is {row['state']}.")
    approval = await decide_approval(
        ctx, str(row["approval_id"]), decision="approved", reason=body.reason
    )
    stored_draft = _decode(row["draft"])
    execution = _execution_payload(
        command_id=command_id,
        command_type=CommandType(row["command_type"]),
        target=_decode(row["target"]),
        draft=dict(stored_draft.get("content") or {}),
        context=dict(stored_draft.get("context") or {}),
        tenant_id=ctx.tenant_id,
    )
    job, _ = await enqueue_job(
        ctx,
        job_type="command:execute",
        payload=execution,
        idempotency_key=f"command:{command_id}:execute",
        created_by=ctx.agent_id,
        scheduled_at=row["scheduled_at"],
        priority=10,
        risk=_RISK[CommandType(row["command_type"])],
        approval_id=str(row["approval_id"]),
    )
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchrow(
            """
            UPDATE command_executions
               SET state='queued', job_id=$2::uuid, updated_at=now()
             WHERE id=$1::uuid
            RETURNING *
            """,
            command_id,
            job["id"],
        )
    return {"command": _command_dict(updated), "approval": approval, "job": job}


@router.post("/{command_id}/reject")
async def reject_command(
    command_id: str,
    body: ApprovalDecision,
    ctx: TenantContext = Depends(require_context),
):
    row = await _get_command(ctx, command_id)
    if not row:
        raise HTTPException(status_code=404, detail="Command not found.")
    approval = await decide_approval(
        ctx, str(row["approval_id"]), decision="rejected", reason=body.reason
    )
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchrow(
            "UPDATE command_executions SET state='cancelled' WHERE id=$1::uuid RETURNING *",
            command_id,
        )
    return {"command": _command_dict(updated), "approval": approval}


@router.post("/calls/{call_session_id}/telemetry")
async def call_telemetry(
    call_session_id: str,
    body: CallTelemetry,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    guidance = negotiation_guidance(
        counter_offer=body.counter_offer,
        arv=body.arv,
        rehab=body.rehab,
        acquisition_ratio=body.acquisition_ratio,
        amber_tolerance=body.amber_tolerance,
    )
    async with tenant_tx(ctx) as conn:
        call = await conn.fetchrow(
            "SELECT * FROM live_call_sessions WHERE id=$1::uuid FOR SHARE",
            call_session_id,
        )
        if not call:
            raise HTTPException(status_code=404, detail="Call session not found.")
        if not call["consent_recorded"]:
            raise HTTPException(status_code=409, detail="Consented transcription is required.")
        event = await conn.fetchrow(
            """
            INSERT INTO negotiation_events (
                tenant_id, call_session_id, event_type, transcript_excerpt,
                counter_offer, arv, rehab, mao, threshold, payload,
                model_version, created_by
            ) VALUES (
                $1::uuid,$2::uuid,'counter_offer',$3,$4,$5,$6,$7,$8,$9::jsonb,
                'objective-negotiation-2026.07',$10
            ) RETURNING id, created_at
            """,
            ctx.tenant_id,
            call_session_id,
            body.transcript_excerpt,
            guidance["counter_offer"],
            body.arv,
            body.rehab,
            guidance["mao"],
            guidance["threshold"],
            json.dumps(guidance),
            ctx.agent_id,
        )
    frame = {
        "type": "NEGOTIATION_TELEMETRY",
        "version": 1,
        "call_session_id": call_session_id,
        "event_id": event["id"],
        **guidance,
    }
    await ws_hub.broadcast(ctx.tenant_id, frame)
    return frame


@router.post("/calls/{call_session_id}/consent")
async def record_call_consent(
    call_session_id: str,
    body: CallConsent,
    ctx: TenantContext = Depends(require_context),
):
    """Record or withdraw explicit transcription consent before telemetry."""
    require_feature(Feature.AUTOMATION)
    async with tenant_tx(ctx) as conn:
        call = await conn.fetchrow(
            """
            UPDATE live_call_sessions
               SET consent_recorded=$2,consent_basis=$3,
                   transcript_status=CASE WHEN $2 THEN 'active' ELSE 'complete' END,
                   ended_at=CASE WHEN $2 THEN ended_at ELSE COALESCE(ended_at,now()) END
             WHERE id=$1::uuid
            RETURNING id
            """,
            call_session_id,
            body.consent_recorded,
            body.consent_basis,
        )
        if not call:
            raise HTTPException(status_code=404, detail="Call session not found.")
        event = await conn.fetchrow(
            """
            INSERT INTO negotiation_events (
                tenant_id,call_session_id,event_type,payload,model_version,created_by
            ) VALUES ($1::uuid,$2::uuid,'consent',$3::jsonb,
                      'explicit-transcription-consent-2026.07',$4)
            RETURNING id,created_at
            """,
            ctx.tenant_id,
            call_session_id,
            json.dumps(
                {
                    "consent_recorded": body.consent_recorded,
                    "basis": body.consent_basis,
                }
            ),
            ctx.agent_id,
        )
    frame = {
        "type": "CALL_CONSENT",
        "version": 1,
        "call_session_id": call_session_id,
        "event_id": event["id"],
        "consent_recorded": body.consent_recorded,
    }
    await ws_hub.broadcast(ctx.tenant_id, frame)
    return frame


@router.post("/webhooks/twilio", include_in_schema=False)
async def twilio_webhook(request: Request):
    form_data = await request.form()
    form = {key: str(value) for key, value in form_data.items()}
    public_base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{public_base}{request.url.path}" if public_base else str(request.url)
    signature_valid = verify_twilio_signature(
        url=url,
        form=form,
        signature=request.headers.get("X-Twilio-Signature", ""),
    )
    if not signature_valid and form.get("AccountSid"):
        platform_ctx = TenantContext(
            agent_id="twilio-webhook-auth",
            tenant_id=os.getenv(
                "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
            ),
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(platform_ctx) as conn:
            credential = await conn.fetchrow(
                """
                SELECT tenant_id,token_ciphertext FROM provider_credentials
                 WHERE provider='twilio' AND account_label=$1 AND disabled_at IS NULL
                 ORDER BY updated_at DESC LIMIT 1
                """,
                form["AccountSid"],
            )
            if credential:
                raw = await decrypt_pii(
                    conn,
                    credential["token_ciphertext"],
                    _provider_key(str(credential["tenant_id"])),
                )
                try:
                    tenant_twilio = json.loads(raw or "{}")
                except ValueError:
                    tenant_twilio = {}
                signature_valid = verify_twilio_signature(
                    url=url,
                    form=form,
                    signature=request.headers.get("X-Twilio-Signature", ""),
                    auth_token=tenant_twilio.get("auth_token"),
                )
    if not signature_valid:
        raise HTTPException(status_code=403, detail="Invalid provider signature.")
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")[:80]
    if call_sid:
        platform_ctx = TenantContext(
            agent_id="twilio-webhook",
            tenant_id=os.getenv(
                "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
            ),
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(platform_ctx) as conn:
            await conn.execute(
                """
                UPDATE live_call_sessions
                   SET started_at=CASE WHEN $2 IN ('in-progress','answered')
                                       THEN COALESCE(started_at,now()) ELSE started_at END,
                       ended_at=CASE WHEN $2 IN ('completed','failed','busy','no-answer','canceled')
                                     THEN now() ELSE ended_at END
                 WHERE provider_call_id=$1
                """,
                call_sid,
                call_status,
            )
    return {"accepted": True}


async def _execute_command_job(payload: dict[str, Any], reporter) -> dict[str, Any]:
    command_type = CommandType(payload["command_type"])
    target = dict(payload.get("target") or {})
    draft = dict(payload.get("draft") or {})
    tenant_id = str(reporter.job["tenant_id"])
    ctx = TenantContext(agent_id="command-worker", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    command_id = str(payload["command_id"])

    # A process crash between provider submission and acknowledgement leaves an
    # `executing` row.  Re-sending could duplicate an email/call, so a new lease
    # moves it to manual reconciliation instead of guessing.
    async with tenant_tx(ctx) as conn:
        command = await conn.fetchrow(
            "SELECT * FROM command_executions WHERE id=$1::uuid FOR UPDATE", command_id
        )
        if command is None:
            raise RuntimeError("command execution row not found")
        if command["state"] == "succeeded":
            if command_type is CommandType.CALL and command["provider_reference"]:
                await conn.execute(
                    """
                    INSERT INTO live_call_sessions (
                        tenant_id,command_id,client_id,lead_id,consent_recorded,
                        consent_basis,transcript_status,provider_call_id,created_by
                    ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,false,
                              'awaiting_explicit_live_transcription_consent',
                              'pending',$5,$6)
                    ON CONFLICT (command_id) DO NOTHING
                    """,
                    tenant_id,
                    command_id,
                    target.get("client_id"),
                    target.get("lead_id"),
                    command["provider_reference"],
                    reporter.job.get("created_by") or "command-worker",
                )
            return {
                "provider": command["provider"],
                "reference": command["provider_reference"],
                "status": "already_submitted",
                "detail": {},
            }
        if command["state"] == "executing":
            await conn.execute(
                """
                UPDATE command_executions
                   SET state='reconciliation_required',
                       reconciliation_reason='Worker lease ended during provider submission; duplicate send suppressed.',
                       updated_at=now()
                 WHERE id=$1::uuid
                """,
                command_id,
            )
            return {
                "provider": command["provider"],
                "reference": command["provider_reference"],
                "status": "reconciliation_required",
                "detail": {"duplicate_submission_suppressed": True},
            }
        if command["state"] not in {"queued", "failed"}:
            raise RuntimeError(f"command is not executable from state {command['state']}")
        await conn.execute(
            """
            UPDATE command_executions
               SET state='executing',provider='pending',last_error=NULL,
                   reconciliation_reason=NULL,updated_at=now()
             WHERE id=$1::uuid
            """,
            command_id,
        )

    await reporter.progress(15, "compliance check")
    submission_started = False
    try:
        if command_type is CommandType.EMAIL:
            decision = await guard_outreach(
                ctx,
                channel=Channel.EMAIL,
                contact=str(target["email"]),
                state_code=target.get("state_code"),
            )
            if not decision.allowed:
                raise RuntimeError("Outreach blocked: " + "; ".join(decision.blockers))
            await reporter.progress(45, "submitting approved email")
            submission_started = True
            provider_result = await send_ses_email({**draft, "target": target})
        elif command_type is CommandType.CALL:
            decision = await guard_outreach(
                ctx,
                channel=Channel.VOICE,
                contact=str(target["phone"]),
                state_code=str(target.get("state_code") or ""),
            )
            if not decision.allowed:
                raise RuntimeError("Call blocked: " + "; ".join(decision.blockers))
            await reporter.progress(45, "placing approved call")
            twilio_raw = await _load_provider_credential(ctx, "twilio")
            twilio_credentials = None
            if twilio_raw:
                try:
                    twilio_credentials = json.loads(twilio_raw)
                except (TypeError, ValueError) as exc:
                    raise ProviderConfigurationError(
                        "Encrypted Twilio credential must be a JSON object"
                    ) from exc
                if not isinstance(twilio_credentials, dict):
                    raise ProviderConfigurationError(
                        "Encrypted Twilio credential must be a JSON object"
                    )
            submission_started = True
            provider_result = await place_twilio_call(
                {**draft, "target": target}, credentials=twilio_credentials
            )
        else:
            await reporter.progress(45, "creating approved calendar event")
            event_draft = dict(draft)
            event = dict(event_draft.get("event") or {})
            # Google accepts caller-supplied base32hex event IDs.  A retry then
            # conflicts with the same event instead of creating a duplicate.
            event.setdefault(
                "id",
                hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:32].replace("f", "v"),
            )
            event_draft["event"] = event
            google_token = await _load_provider_credential(
                ctx,
                "google",
                account_label=str(reporter.job.get("created_by") or ""),
            )
            submission_started = True
            provider_result = await create_google_calendar_event(
                event_draft, access_token=google_token
            )
    except Exception as exc:
        uncertain = submission_started and not isinstance(exc, ProviderConfigurationError)
        try:
            async with tenant_tx(ctx) as conn:
                await conn.execute(
                    """
                    UPDATE command_executions
                       SET state=$3,last_error=$2,
                           provider=CASE WHEN $3='failed' THEN NULL ELSE provider END,
                           reconciliation_reason=CASE WHEN $3='reconciliation_required'
                               THEN 'Provider submission began but no acknowledgement was received; duplicate retry suppressed.'
                               ELSE NULL END,
                           updated_at=now()
                     WHERE id=$1::uuid AND state='executing'
                    """,
                    command_id,
                    str(exc)[:2_000],
                    "reconciliation_required" if uncertain else "failed",
                )
        except Exception:
            pass
        if uncertain:
            return {
                "provider": None,
                "reference": None,
                "status": "reconciliation_required",
                "detail": {"duplicate_submission_suppressed": True},
            }
        raise

    await reporter.progress(80, "recording provider acknowledgement")
    recorded = False
    for attempt in range(3):
        try:
            async with tenant_tx(ctx) as conn:
                await conn.execute(
                    """
                    UPDATE command_executions
                       SET state='succeeded',provider=$2,provider_reference=$3,
                           provider_submitted_at=now(),last_error=NULL,updated_at=now()
                     WHERE id=$1::uuid
                    """,
                    command_id,
                    provider_result.provider,
                    provider_result.reference,
                )
                if command_type is CommandType.CALL:
                    await conn.execute(
                        """
                        INSERT INTO live_call_sessions (
                            tenant_id,command_id,client_id,lead_id,consent_recorded,
                            consent_basis,transcript_status,provider_call_id,created_by
                        ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,false,
                                  'awaiting_explicit_live_transcription_consent',
                                  'pending',$5,$6)
                        ON CONFLICT (command_id) DO NOTHING
                        """,
                        tenant_id,
                        command_id,
                        target.get("client_id"),
                        target.get("lead_id"),
                        provider_result.reference,
                        reporter.job.get("created_by") or "command-worker",
                    )
            recorded = True
            break
        except Exception:
            if attempt < 2:
                await asyncio.sleep(0.25 * (2**attempt))
    if not recorded:
        # The provider may have accepted the action; never retry it blindly.
        raise RuntimeError("provider acknowledgement could not be persisted; manual reconciliation required")
    await ledger.record(
        category=AuditCategory.AI_PHONE_CALL
        if command_type is CommandType.CALL
        else AuditCategory.USER_STATE_CHANGE,
        action=f"approved_{command_type.value.lower()}_submitted",
        tenant_id=tenant_id,
        user_id=str(reporter.job.get("created_by") or "command-worker"),
        target_id=command_id,
        metadata={
            "provider": provider_result.provider,
            "provider_reference": provider_result.reference,
            "approval_id": reporter.job.get("approval_id"),
        },
    )
    return provider_result.as_dict()


register_handler("command:execute", _execute_command_job)
