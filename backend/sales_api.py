"""Our AI Sales workspace: capability truth, work queue, providers, and Smart Plans.

Outbound email, SMS, and voice steps are never sent here. They are staged in
the existing command/approval engine and require a human approval before a
provider job can run.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from automation_jobs import enqueue_job, register_handler
from command_providers import ProviderConfigurationError
from commands_api import (
    CommandCreate,
    CommandType,
    ProviderCredentialInput,
    _load_provider_credential,
    create_command,
    disable_provider_credential,
    store_provider_credential,
)
from contacts_api import _CONTACT_SELECT, _contact_json, search_contact_rows
from db.connection import tenant_tx
from outreach_compliance import Channel, VoiceMode, guard_outreach
from platform_policy import ActionRisk, Feature, feature_enabled, require_feature
from tenancy import Role, TenantContext, require_context, require_role


router = APIRouter(prefix="/api/sales", tags=["Our AI Sales"])

_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9A-Fa-f]{32}$")
_API_KEY_RE = re.compile(r"^SK[0-9A-Fa-f]{32}$")
_TWIML_APP_RE = re.compile(r"^AP[0-9A-Fa-f]{32}$")
_PREVIEW_TTL_SECONDS = 15 * 60


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a UUID") from exc


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _preview_key() -> bytes:
    raw = (
        os.getenv("ORACLE_SMART_PLAN_SIGNING_KEY", "").strip()
        or os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip()
    )
    if len(raw) < 32:
        raise HTTPException(
            status_code=503,
            detail="Smart Plan preview signing is not configured.",
        )
    return hashlib.sha256(("smart-plan-preview:" + raw).encode("utf-8")).digest()


def _sign_preview(payload: dict[str, Any]) -> str:
    raw = base64.urlsafe_b64encode(_canonical(payload).encode("utf-8")).rstrip(b"=")
    signature = hmac.new(_preview_key(), raw, hashlib.sha256).hexdigest().encode("ascii")
    return raw.decode("ascii") + "." + signature.decode("ascii")


def _verify_preview(token: str) -> dict[str, Any]:
    try:
        raw_text, supplied = token.split(".", 1)
        raw = raw_text.encode("ascii")
    except (ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(status_code=422, detail="Preview token is invalid.") from exc
    expected = hmac.new(_preview_key(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=422, detail="Preview token is invalid.")
    try:
        padded = raw + b"=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Preview token is invalid.") from exc
    issued_at = int(payload.get("issued_at") or 0)
    now = int(datetime.now(timezone.utc).timestamp())
    if issued_at <= 0 or now - issued_at > _PREVIEW_TTL_SECONDS or issued_at > now + 30:
        raise HTTPException(status_code=409, detail="Preview expired; run it again.")
    return payload


class CapabilityState(str, Enum):
    LIVE = "live"
    SETUP_REQUIRED = "setup_required"
    PARTIAL = "partial"
    DISABLED = "disabled"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    type: Literal["wait", "task", "email", "sms", "approved_call"]
    delay_minutes: int = Field(default=0, ge=0, le=525_600)
    title: Optional[str] = Field(default=None, max_length=200)
    subject: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=20_000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"

    @model_validator(mode="after")
    def validate_step(self) -> "PlanStep":
        if self.type == "task" and not self.title:
            raise ValueError("task steps require a title")
        if self.type == "email" and (not self.subject or not self.body):
            raise ValueError("email steps require subject and body")
        if self.type == "sms" and (not self.body or len(self.body) > 1_600):
            raise ValueError("SMS steps require a body of 1-1600 characters")
        if self.type == "approved_call" and not self.body:
            raise ValueError("approved call steps require a script")
        return self


class PlanDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[PlanStep] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_keys(self) -> "PlanDefinition":
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("Smart Plan step keys must be unique")
        return self


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2_000)
    scope: Literal["personal", "team"] = "personal"
    definition: PlanDefinition


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2_000)
    scope: Optional[Literal["personal", "team"]] = None
    definition: Optional[PlanDefinition] = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_ids: list[str] = Field(min_length=1, max_length=200)
    start_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("contact_ids")
    @classmethod
    def valid_contacts(cls, value: list[str]) -> list[str]:
        normalized = [str(uuid.UUID(item)) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("contact_ids must be unique")
        return normalized

    @field_validator("start_at")
    @classmethod
    def aware_start(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_at must include a timezone")
        return value.astimezone(timezone.utc)


class EnrollmentCreate(PreviewRequest):
    preview_token: str = Field(min_length=64, max_length=8_000)


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal[
        "summarize",
        "qualify",
        "draft_email",
        "draft_sms",
        "draft_call_script",
        "create_task",
    ]
    contact_id: str
    subject: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=20_000)
    title: Optional[str] = Field(default=None, max_length=200)
    due_at: Optional[datetime] = None
    stage_for_approval: bool = False

    @field_validator("contact_id")
    @classmethod
    def valid_contact_id(cls, value: str) -> str:
        return str(uuid.UUID(value))

    @field_validator("due_at")
    @classmethod
    def aware_due_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        return value


class ProviderSetupInput(BaseModel):
    """Strict union-shaped input; provider-specific rules run in the endpoint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    account_label: str = Field(default="default", min_length=1, max_length=120)
    account_sid: Optional[str] = Field(default=None, max_length=34)
    auth_token: Optional[str] = Field(default=None, min_length=8, max_length=2_000)
    api_key: Optional[str] = Field(default=None, max_length=34)
    api_secret: Optional[str] = Field(default=None, min_length=8, max_length=2_000)
    from_number: Optional[str] = Field(default=None, max_length=20)
    twiml_app_sid: Optional[str] = Field(default=None, max_length=34)
    sms_sender: Optional[str] = Field(default=None, max_length=20)
    sms_sender_type: Optional[
        Literal["twilio_registered", "ported", "toll_free_verified"]
    ] = None
    connection_string: Optional[str] = Field(default=None, min_length=8, max_length=8_000)
    from_email: Optional[str] = Field(default=None, max_length=254)
    region: Optional[str] = Field(default=None, max_length=40)
    aws_access_key_id: Optional[str] = Field(default=None, min_length=16, max_length=128)
    aws_secret_access_key: Optional[str] = Field(default=None, min_length=16, max_length=256)
    aws_session_token: Optional[str] = Field(default=None, min_length=16, max_length=4_096)

    @model_validator(mode="after")
    def validate_aws_credential_pair(self) -> "ProviderSetupInput":
        if bool(self.aws_access_key_id) != bool(self.aws_secret_access_key):
            raise ValueError(
                "aws_access_key_id and aws_secret_access_key must be configured together"
            )
        if self.aws_session_token and not self.aws_access_key_id:
            raise ValueError("aws_session_token requires an AWS access key pair")
        return self


def _provider_payload(provider: str, body: ProviderSetupInput) -> dict[str, Any]:
    raw = body.model_dump(exclude_none=True)
    raw.pop("account_label", None)
    if provider == "twilio":
        if not _ACCOUNT_SID_RE.fullmatch(str(raw.get("account_sid") or "")):
            raise HTTPException(status_code=422, detail="Twilio account_sid is invalid.")
        if not raw.get("auth_token") and not (
            _API_KEY_RE.fullmatch(str(raw.get("api_key") or "")) and raw.get("api_secret")
        ):
            raise HTTPException(status_code=422, detail="Twilio auth token or API key pair is required.")
        if not _E164_RE.fullmatch(str(raw.get("from_number") or "")):
            raise HTTPException(status_code=422, detail="Twilio voice from_number must be E.164.")
        if not _TWIML_APP_RE.fullmatch(str(raw.get("twiml_app_sid") or "")):
            raise HTTPException(status_code=422, detail="Twilio twiml_app_sid is invalid.")
        if bool(raw.get("sms_sender")) != bool(raw.get("sms_sender_type")):
            raise HTTPException(status_code=422, detail="SMS sender and registered sender type are required together.")
        if raw.get("sms_sender") and not _E164_RE.fullmatch(str(raw["sms_sender"])):
            raise HTTPException(status_code=422, detail="Twilio SMS sender must be E.164.")
    elif provider == "acs":
        if not raw.get("connection_string") or "endpoint=https://" not in str(raw["connection_string"]).lower():
            raise HTTPException(status_code=422, detail="ACS connection_string is invalid.")
        if raw.get("from_number") and not _E164_RE.fullmatch(str(raw["from_number"])):
            raise HTTPException(status_code=422, detail="ACS voice from_number must be E.164.")
        if raw.get("sms_sender") and not _E164_RE.fullmatch(str(raw["sms_sender"])):
            raise HTTPException(status_code=422, detail="ACS SMS sender must be E.164.")
    elif provider == "ses":
        email = str(raw.get("from_email") or "")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HTTPException(status_code=422, detail="SES from_email is invalid.")
        raw["region"] = str(raw.get("region") or "us-east-2")
    else:
        raise HTTPException(
            status_code=422,
            detail="Google is a calendar-only connection; use SMTP, ACS or SES for email.",
        )
    return raw


async def _provider_snapshot(ctx: TenantContext) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT provider,account_label,expires_at,last_validated_at,disabled_at,
                   validation_status,validation_error,validated_capabilities,updated_at
              FROM provider_credentials
             WHERE provider=ANY($1::text[])
             ORDER BY provider,updated_at DESC
            """,
            ["google", "smtp", "twilio", "acs", "ses"],
        )
        route = await conn.fetchrow(
            """
            SELECT inbound_did,twilio_account_sid,intake_mode,forwarding_mode,
                   forwarding_source_e164,sip_domain,
                   voice_caller_id_e164,voice_caller_id_verified,
                   sms_sender_e164,sms_sender_type,active,
                   agent_forward_e164,forward_on_request,
                   forward_when_ai_unavailable,forward_timeout_seconds
              FROM telephony_routes
             WHERE agent_id=$1 AND active=true
             LIMIT 1
            """,
            ctx.agent_id,
        )
    providers: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = str(row["provider"])
        if provider in providers:
            continue
        expired = bool(row["expires_at"] and row["expires_at"] <= now)
        disabled = bool(row["disabled_at"])
        status_value = "expired" if expired else str(row["validation_status"] or "unverified")
        is_valid = status_value == "valid" or (
            provider == "google"
            and status_value == "unverified"
            and row["last_validated_at"] is not None
        )
        providers[provider] = {
            "provider": provider,
            "account_label": row["account_label"],
            "configured": not expired and not disabled and is_valid,
            "validation_status": status_value,
            "validation_error": row["validation_error"],
            "last_validated_at": _iso(row["last_validated_at"]),
            "updated_at": _iso(row["updated_at"]),
            "capabilities": _json(row["validated_capabilities"], {}),
            "credential_exposed": False,
        }
    for provider in ("google", "smtp", "twilio", "acs", "ses"):
        providers.setdefault(
            provider,
            {
                "provider": provider,
                "configured": False,
                "validation_status": "unverified",
                "validation_error": None,
                "last_validated_at": None,
                "capabilities": {},
                "credential_exposed": False,
            },
        )
    route_data = dict(route) if route else {}
    env_twilio = bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and (os.getenv("TWILIO_AUTH_TOKEN") or (os.getenv("TWILIO_API_KEY") and os.getenv("TWILIO_API_SECRET")))
        and os.getenv("ORACLE_TWILIO_CREDENTIALS_VALIDATED", "").lower() in {"1", "true", "yes", "on"}
    )
    env_ses = bool(
        os.getenv("ORACLE_SES_FROM_EMAIL")
        and os.getenv("ORACLE_SES_CREDENTIALS_VALIDATED", "").lower() in {"1", "true", "yes", "on"}
    )
    env_acs = bool(
        os.getenv("ACS_CONNECTION_STRING")
        and os.getenv("ORACLE_ACS_CREDENTIALS_VALIDATED", "").lower() in {"1", "true", "yes", "on"}
    )
    twilio_capabilities = providers["twilio"].get("capabilities") or {}
    acs_capabilities = providers["acs"].get("capabilities") or {}
    ses_capabilities = providers["ses"].get("capabilities") or {}
    smtp_capabilities = providers["smtp"].get("capabilities") or {}
    # Platform-level senders count as an email channel even with no tenant
    # credential — they are what sends when a tenant has configured nothing.
    env_smtp = bool(os.getenv("ORACLE_SMTP_FROM_EMAIL") or os.getenv("ORACLE_SMTP_USERNAME"))
    env_acs_email = bool(
        os.getenv("ACS_CONNECTION_STRING") and os.getenv("ORACLE_ACS_FROM_EMAIL")
    )
    twilio_voice = providers["twilio"]["configured"] and bool(twilio_capabilities.get("voice"))
    twilio_sms = providers["twilio"]["configured"] and bool(twilio_capabilities.get("sms"))
    twilio_agent = providers["twilio"]["configured"] and bool(twilio_capabilities.get("agent_call"))
    acs_voice = providers["acs"]["configured"] and bool(acs_capabilities.get("voice"))
    acs_sms = providers["acs"]["configured"] and bool(acs_capabilities.get("sms"))
    acs_email = providers["acs"]["configured"] and bool(acs_capabilities.get("email"))
    ses_email = providers["ses"]["configured"] and bool(ses_capabilities.get("email"))
    smtp_email = providers["smtp"]["configured"] and bool(smtp_capabilities.get("email"))
    return {
        "providers": providers,
        "route": route_data,
        "channels": {
            # A connected Google account no longer implies email works — that
            # grant is calendar-only now, so only real senders count here.
            "email": (smtp_email or env_smtp or acs_email or env_acs_email or ses_email or env_ses),
            "sms": (
                (twilio_sms or env_twilio)
                and bool(route_data.get("sms_sender_e164") and route_data.get("sms_sender_type"))
            ) or acs_sms or env_acs,
            "ai_call": (
                twilio_voice
                or env_twilio
                or acs_voice
                or env_acs
            ),
            "agent_call": (
                (twilio_agent or (env_twilio and bool(os.getenv("TWILIO_TWIML_APP_SID"))))
                and bool(route_data.get("voice_caller_id_verified"))
                and bool(route_data.get("voice_caller_id_e164"))
            ),
        },
    }


@router.get("/capabilities")
async def capabilities(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    provider = await _provider_snapshot(ctx)
    sales_enabled = feature_enabled(Feature.SALES_AI)
    dialer_enabled = feature_enabled(Feature.POWER_DIALER)
    plans_enabled = feature_enabled(Feature.SMART_PLANS)
    async with tenant_tx(ctx) as conn:
        plans_table = bool(await conn.fetchval("SELECT to_regclass('public.smart_plans') IS NOT NULL"))
        routing_table = bool(await conn.fetchval("SELECT to_regclass('public.lead_intake_events') IS NOT NULL"))
    rows = [
        {
            "id": "sales-agent",
            "name": "Sales Agent",
            "state": CapabilityState.LIVE.value if sales_enabled else CapabilityState.DISABLED.value,
            "href": "/our-ai/sales/agent",
            "description": "Observed CRM facts, qualification, summaries, and approval-bound follow-up drafts.",
        },
        {
            "id": "power-dialer",
            "name": "Power Dialer",
            "state": (
                CapabilityState.LIVE.value
                if dialer_enabled and provider["channels"]["agent_call"]
                else CapabilityState.SETUP_REQUIRED.value
                if dialer_enabled
                else CapabilityState.DISABLED.value
            ),
            "href": "/our-ai/sales/dialer",
            "description": "Agent browser calling and approval-bound AI voice from verified routes.",
        },
        {
            "id": "smart-plans",
            "name": "Smart Plans",
            "state": (
                CapabilityState.LIVE.value
                if plans_enabled and plans_table
                else CapabilityState.PARTIAL.value
                if plans_enabled
                else CapabilityState.DISABLED.value
            ),
            "href": "/our-ai/sales/plans",
            "description": "Versioned email, SMS, approved-call, wait, and task workflows with manual enrollment.",
        },
        {
            "id": "provider-delivery",
            "name": "Provider delivery",
            "state": (
                CapabilityState.LIVE.value
                if all(provider["channels"][key] for key in ("email", "sms", "ai_call"))
                else CapabilityState.SETUP_REQUIRED.value
            ),
            "href": "/our-ai/sales/providers",
            "description": "Tenant-scoped delivery health; channels are connected only when credentials and routes are valid.",
        },
        {
            "id": "lead-routing",
            "name": "Lead Routing",
            "state": CapabilityState.LIVE.value if routing_table else CapabilityState.DISABLED.value,
            "href": "/our-ai/sales/routing",
            "description": "Signed lead capture, tenant-safe deduplication, ZIP and intent rules, capacity-aware assignment, and source analytics.",
        },
    ]
    return {"capabilities": rows, "channels": provider["channels"], "observed_at": datetime.now(timezone.utc).isoformat()}


async def _load_contact(ctx: TenantContext, contact_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            _CONTACT_SELECT + " WHERE contact.id=$1::uuid AND contact.deleted_at IS NULL",
            contact_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contact not found.")
        contact = await _contact_json(conn, ctx, row)
        client = await conn.fetchrow(
            """
            SELECT id,stage,lead_score,last_contacted_at,updated_at
              FROM clients
             WHERE contact_id=$1::uuid OR id=$2::uuid
             ORDER BY (contact_id=$1::uuid) DESC LIMIT 1
            """,
            contact_id,
            contact.get("legacy_client_id"),
        )
    return contact, dict(client) if client else {}


@router.get("/agent/work-queue")
async def work_queue(
    q: Optional[str] = Query(default=None, max_length=160),
    stage: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_feature(Feature.SALES_AI)
    async with tenant_tx(ctx) as conn:
        contacts: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        page_size = min(200, max(limit * 2, 50))
        while len(contacts) < limit:
            rows, next_cursor = await search_contact_rows(
                conn,
                ctx,
                query=q,
                limit=page_size,
                cursor=cursor,
            )
            page_contacts = [await _contact_json(conn, ctx, row) for row in rows]
            if not stage:
                contacts.extend(page_contacts)
            else:
                page_ids = [contact["id"] for contact in page_contacts]
                page_legacy_ids = [
                    contact["legacy_client_id"]
                    for contact in page_contacts
                    if contact.get("legacy_client_id")
                ]
                page_clients = await conn.fetch(
                    """
                    SELECT id,contact_id,stage
                      FROM clients
                     WHERE contact_id=ANY($1::uuid[]) OR id=ANY($2::uuid[])
                    """,
                    page_ids,
                    page_legacy_ids,
                ) if page_ids else []
                matching_contact_ids = {
                    str(row["contact_id"])
                    for row in page_clients
                    if row["contact_id"] and str(row["stage"] or "lead") == stage
                }
                matching_legacy_ids = {
                    str(row["id"])
                    for row in page_clients
                    if str(row["stage"] or "lead") == stage
                }
                contacts.extend(
                    contact
                    for contact in page_contacts
                    if contact["id"] in matching_contact_ids
                    or str(contact.get("legacy_client_id") or "") in matching_legacy_ids
                )
            if not next_cursor:
                break
            cursor = next_cursor
        contacts = contacts[:limit]
        ids = [contact["id"] for contact in contacts]
        legacy_ids = [contact["legacy_client_id"] for contact in contacts if contact.get("legacy_client_id")]
        clients = await conn.fetch(
            """
            SELECT id,contact_id,stage,lead_score,last_contacted_at,updated_at
              FROM clients
             WHERE contact_id=ANY($1::uuid[]) OR id=ANY($2::uuid[])
            """,
            ids,
            legacy_ids,
        ) if ids else []
        tasks = await conn.fetch(
            """
            SELECT client_id,count(*)::int AS open_tasks,min(due_at) AS next_due_at
              FROM client_tasks
             WHERE status='open' AND client_id=ANY($1::uuid[])
             GROUP BY client_id
            """,
            [str(row["id"]) for row in clients],
        ) if clients else []
    client_by_contact = {str(row["contact_id"]): row for row in clients if row["contact_id"]}
    client_by_id = {str(row["id"]): row for row in clients}
    task_by_client = {str(row["client_id"]): row for row in tasks if row["client_id"]}
    items: list[dict[str, Any]] = []
    for contact in contacts:
        client = client_by_contact.get(contact["id"]) or client_by_id.get(str(contact.get("legacy_client_id") or ""))
        client_id = str(client["id"]) if client else None
        task = task_by_client.get(client_id or "")
        reasons: list[str] = []
        if client and int(client["lead_score"] or 0) >= 70:
            reasons.append("High observed CRM lead score")
        if not client or not client["last_contacted_at"]:
            reasons.append("No recorded contact attempt")
        elif client["last_contacted_at"] < datetime.now(timezone.utc) - timedelta(days=7):
            reasons.append("Last recorded contact is more than 7 days old")
        if task and int(task["open_tasks"] or 0):
            reasons.append(f"{int(task['open_tasks'])} open task(s)")
        items.append(
            {
                "contact": contact,
                "client": {
                    "id": client_id,
                    "stage": str(client["stage"]) if client else "lead",
                    "lead_score": int(client["lead_score"] or 0) if client else 0,
                    "last_contacted_at": _iso(client["last_contacted_at"]) if client else None,
                },
                "open_tasks": int(task["open_tasks"] or 0) if task else 0,
                "next_due_at": _iso(task["next_due_at"]) if task else None,
                "reasons": reasons or ["Contact is available for agent review"],
                "evidence": ["agent_contacts", "clients", "client_tasks"],
            }
        )
        if len(items) >= limit:
            break
    return {"items": items, "count": len(items), "evidence_status": "observed"}


@router.post("/agent/actions")
async def run_agent_action(body: AgentAction, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SALES_AI)
    contact, client = await _load_contact(ctx, body.contact_id)
    name = str(contact.get("full_name") or "there")
    first_name = name.split()[0] if name else "there"
    facts = {
        "stage": client.get("stage") or "lead",
        "lead_score": int(client.get("lead_score") or 0),
        "last_contacted_at": _iso(client.get("last_contacted_at")),
        "preferred_channel": contact.get("preferred_channel"),
        "state_code": contact.get("state_code"),
    }
    if body.action == "summarize":
        return {
            "action": body.action,
            "summary": f"{name} is in {facts['stage']} with an observed CRM score of {facts['lead_score']}. Preferred channel: {facts['preferred_channel']}.",
            "facts": facts,
            "evidence_status": "observed",
            "warnings": ["No protected-class traits are used for scoring or recommendations."],
        }
    if body.action == "qualify":
        gaps = [key for key in ("email", "phone", "state_code") if not contact.get(key)]
        return {
            "action": body.action,
            "qualification": "priority" if facts["lead_score"] >= 70 else "standard_review",
            "score": facts["lead_score"],
            "data_gaps": gaps,
            "basis": ["clients.lead_score", "clients.stage", "agent_contacts channel readiness"],
            "evidence_status": "observed",
        }
    if body.action == "create_task":
        title = body.title or f"Follow up with {name}"
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO client_tasks (
                    tenant_id,client_id,title,details,due_at,status,priority,assignee_id,created_by
                ) VALUES ($1::uuid,$2::uuid,$3,$4,$5,'open','normal',$6,$6)
                RETURNING id,title,due_at,status,created_at
                """,
                ctx.tenant_id,
                str(client["id"]) if client else None,
                title,
                "Created from Our AI Sales Agent using observed CRM context.",
                body.due_at,
                ctx.agent_id,
            )
        return {"action": body.action, "task": {**dict(row), "id": str(row["id"]), "due_at": _iso(row["due_at"]), "created_at": _iso(row["created_at"])}}

    state_code = str(contact.get("state_code") or "")
    target = {
        "contact_id": contact["id"],
        "client_id": str(client["id"]) if client else None,
        "state_code": state_code,
        "timezone": contact.get("timezone"),
    }
    if body.action == "draft_email":
        target["email"] = contact.get("email")
        draft = {
            "subject": body.subject or "Following up on your real estate plans",
            "body": body.body or f"Hi {first_name},\n\nI wanted to follow up on your real estate plans. What would be most helpful right now?",
        }
        command_type = CommandType.EMAIL
    elif body.action == "draft_sms":
        target["phone"] = contact.get("phone")
        draft = {"body": body.body or f"Hi {first_name}, I’m following up on your real estate plans. What would be most helpful right now?"}
        command_type = CommandType.SMS
    else:
        target["phone"] = contact.get("phone")
        draft = {"script": body.body or f"Hi {first_name}, this is your real estate agent calling to follow up on your plans."}
        command_type = CommandType.CALL
    response: dict[str, Any] = {
        "action": body.action,
        "draft": draft,
        "target_readiness": {
            "has_destination": bool(target.get("email") or target.get("phone")),
            "has_state_code": bool(state_code),
            "requires_approval": True,
        },
        "evidence_status": "observed_plus_agent_editable_template",
    }
    if body.stage_for_approval:
        try:
            staged = await create_command(
                CommandCreate(
                    command_type=command_type,
                    target=target,
                    draft=draft,
                    idempotency_key=f"sales-agent:{body.action}:{contact['id']}:{uuid.uuid4()}",
                    context={"source": "our-ai-sales-agent", "contact_id": contact["id"]},
                ),
                ctx,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response["command"] = staged["command"]
        response["approval"] = staged.get("approval")
    return response


def _plan_json(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "scope": row["scope"],
        "status": row["status"],
        "owner_agent_id": row["owner_agent_id"],
        "definition": _json(row["draft_definition"], {"steps": []}),
        "current_revision_id": str(row["current_revision_id"]) if row["current_revision_id"] else None,
        "current_revision_number": row.get("current_revision_number") if hasattr(row, "get") else None,
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _plan_readable_by_agent(plan: Any, ctx: TenantContext) -> bool:
    if ctx.is_platform_admin or ctx.is_broker_owner:
        return True
    return plan["scope"] == "team" or plan["owner_agent_id"] == ctx.agent_id


def _plan_mutable_by_agent(plan: Any, ctx: TenantContext) -> bool:
    if ctx.is_platform_admin or ctx.is_broker_owner:
        return True
    return plan["scope"] == "personal" and plan["owner_agent_id"] == ctx.agent_id


def _require_plan_read(plan: Any, ctx: TenantContext) -> None:
    if not _plan_readable_by_agent(plan, ctx):
        raise HTTPException(status_code=404, detail="Smart Plan not found.")


def _require_plan_write(plan: Any, ctx: TenantContext) -> None:
    if not _plan_mutable_by_agent(plan, ctx):
        raise HTTPException(
            status_code=403,
            detail="Only the personal plan owner or a brokerage owner can change this Smart Plan.",
        )


@router.get("/plans")
async def list_plans(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT plan.*,revision.revision_number AS current_revision_number
              FROM smart_plans plan
              LEFT JOIN smart_plan_revisions revision ON revision.id=plan.current_revision_id
             WHERE plan.status <> 'archived'
               AND ($1::boolean OR plan.scope='team' OR plan.owner_agent_id=$2)
             ORDER BY plan.updated_at DESC
            """,
            ctx.is_platform_admin or ctx.is_broker_owner,
            ctx.agent_id,
        )
    return {"plans": [_plan_json(row) for row in rows]}


@router.post("/plans", status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanCreate, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    if body.scope == "team":
        require_role(ctx, Role.BROKER_OWNER)
    definition = body.definition.model_dump(mode="json")
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO smart_plans (
                tenant_id,owner_agent_id,name,description,scope,draft_definition,created_by
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6::jsonb,$2) RETURNING *
            """,
            ctx.tenant_id,
            ctx.agent_id,
            body.name,
            body.description,
            body.scope,
            _canonical(definition),
        )
    return {"plan": _plan_json(row)}


@router.get("/plans/enrollments")
async def list_enrollments(
    plan_id: Optional[str] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    if plan_id:
        plan_id = _uuid(plan_id, "plan_id")
    async with tenant_tx(ctx) as conn:
        privileged = ctx.is_platform_admin or ctx.is_broker_owner
        await conn.execute(
            """
            UPDATE smart_plan_step_runs run
               SET state=CASE
                    WHEN command.state='succeeded' THEN 'succeeded'
                    WHEN command.state IN ('failed','reconciliation_required') THEN 'failed'
                    WHEN command.state='cancelled' THEN 'cancelled'
                    ELSE run.state END,
                   finished_at=CASE WHEN command.state IN ('succeeded','failed','cancelled','reconciliation_required')
                                    THEN COALESCE(run.finished_at,now()) ELSE run.finished_at END
              FROM command_executions command
             WHERE run.command_id=command.id AND run.state='awaiting_approval'
               AND ($1::boolean OR EXISTS (
                    SELECT 1 FROM smart_plan_enrollments owned
                     WHERE owned.id=run.enrollment_id AND owned.created_by=$2
               ))
            """,
            privileged,
            ctx.agent_id,
        )
        await conn.execute(
            """
            UPDATE smart_plan_enrollments enrollment
               SET status='completed',completed_at=COALESCE(completed_at,now()),next_run_at=NULL
             WHERE status='active'
               AND ($1::boolean OR enrollment.created_by=$2)
               AND NOT EXISTS (
                    SELECT 1 FROM smart_plan_step_runs run
                     WHERE run.enrollment_id=enrollment.id
                       AND run.state IN ('scheduled','paused','running','awaiting_approval')
               )
            """,
            privileged,
            ctx.agent_id,
        )
        rows = await conn.fetch(
            """
            SELECT enrollment.*,plan.name AS plan_name,revision.revision_number,
                   count(run.id)::int AS step_count,
                   count(run.id) FILTER (WHERE run.state='awaiting_approval')::int AS approvals_waiting
              FROM smart_plan_enrollments enrollment
              JOIN smart_plans plan ON plan.id=enrollment.plan_id
              JOIN smart_plan_revisions revision ON revision.id=enrollment.revision_id
              LEFT JOIN smart_plan_step_runs run ON run.enrollment_id=enrollment.id
             WHERE ($1::uuid IS NULL OR enrollment.plan_id=$1::uuid)
               AND ($2::boolean OR enrollment.created_by=$3)
             GROUP BY enrollment.id,plan.name,revision.revision_number
             ORDER BY enrollment.created_at DESC LIMIT 500
            """,
            plan_id,
            privileged,
            ctx.agent_id,
        )
    return {
        "enrollments": [
            {
                **dict(row),
                "id": str(row["id"]),
                "plan_id": str(row["plan_id"]),
                "revision_id": str(row["revision_id"]),
                "contact_id": str(row["contact_id"]),
                **{key: _iso(row[key]) for key in ("next_run_at", "paused_at", "completed_at", "cancelled_at", "created_at", "updated_at")},
            }
            for row in rows
        ]
    }


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    plan_id = _uuid(plan_id, "plan_id")
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT plan.*,revision.revision_number AS current_revision_number
              FROM smart_plans plan
              LEFT JOIN smart_plan_revisions revision ON revision.id=plan.current_revision_id
             WHERE plan.id=$1::uuid
            """,
            plan_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Smart Plan not found.")
    _require_plan_read(row, ctx)
    return {"plan": _plan_json(row)}


@router.patch("/plans/{plan_id}")
async def update_plan(plan_id: str, body: PlanUpdate, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    plan_id = _uuid(plan_id, "plan_id")
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail="No Smart Plan fields supplied.")
    if values.get("scope") == "team":
        require_role(ctx, Role.BROKER_OWNER)
    sets: list[str] = []
    args: list[Any] = []
    for key in ("name", "description", "scope"):
        if key in values:
            args.append(values[key])
            sets.append(f"{key}=${len(args)}")
    if "definition" in values:
        args.append(_canonical(body.definition.model_dump(mode="json")))
        sets.append(f"draft_definition=${len(args)}::jsonb")
    args.append(plan_id)
    async with tenant_tx(ctx) as conn:
        current = await conn.fetchrow("SELECT owner_agent_id,scope FROM smart_plans WHERE id=$1::uuid", plan_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Smart Plan not found.")
        _require_plan_write(current, ctx)
        row = await conn.fetchrow(
            f"UPDATE smart_plans SET {', '.join(sets)} WHERE id=${len(args)}::uuid RETURNING *",
            *args,
        )
    return {"plan": _plan_json(row)}


@router.post("/plans/{plan_id}/publish")
async def publish_plan(plan_id: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    plan_id = _uuid(plan_id, "plan_id")
    async with tenant_tx(ctx) as conn:
        plan = await conn.fetchrow("SELECT * FROM smart_plans WHERE id=$1::uuid FOR UPDATE", plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Smart Plan not found.")
        _require_plan_write(plan, ctx)
        definition = PlanDefinition.model_validate(_json(plan["draft_definition"], {})).model_dump(mode="json")
        revision_number = int(await conn.fetchval(
            "SELECT COALESCE(max(revision_number),0)+1 FROM smart_plan_revisions WHERE plan_id=$1::uuid",
            plan_id,
        ))
        revision = await conn.fetchrow(
            """
            INSERT INTO smart_plan_revisions (
                tenant_id,plan_id,revision_number,definition,definition_hash,created_by
            ) VALUES ($1::uuid,$2::uuid,$3,$4::jsonb,$5,$6) RETURNING *
            """,
            ctx.tenant_id,
            plan_id,
            revision_number,
            _canonical(definition),
            _hash(definition),
            ctx.agent_id,
        )
        await conn.execute(
            "UPDATE smart_plans SET current_revision_id=$2::uuid,status='published' WHERE id=$1::uuid",
            plan_id,
            str(revision["id"]),
        )
    return {"revision": {"id": str(revision["id"]), "revision_number": revision_number, "definition_hash": revision["definition_hash"], "published_at": _iso(revision["published_at"])}}


async def _preview(ctx: TenantContext, plan_id: str, body: PreviewRequest) -> dict[str, Any]:
    provider = await _provider_snapshot(ctx)
    async with tenant_tx(ctx) as conn:
        revision = await conn.fetchrow(
            """
            SELECT revision.*,plan.name,plan.scope,plan.owner_agent_id
              FROM smart_plans plan JOIN smart_plan_revisions revision
                ON revision.id=plan.current_revision_id
             WHERE plan.id=$1::uuid AND plan.status='published'
            """,
            plan_id,
        )
        if revision is None:
            raise HTTPException(status_code=409, detail="Publish the Smart Plan before previewing enrollment.")
        _require_plan_read(revision, ctx)
        rows = await conn.fetch(
            _CONTACT_SELECT
            + " WHERE contact.id=ANY($1::uuid[]) AND contact.deleted_at IS NULL ORDER BY contact.id",
            body.contact_ids,
        )
        contacts = [await _contact_json(conn, ctx, row) for row in rows]
        active_enrollments = await conn.fetch(
            """
            SELECT contact_id
              FROM smart_plan_enrollments
             WHERE plan_id=$1::uuid
               AND contact_id=ANY($2::uuid[])
               AND status IN ('active','paused')
            """,
            plan_id,
            body.contact_ids,
        )
        active_contact_ids = {str(row["contact_id"]) for row in active_enrollments}
    found = {contact["id"] for contact in contacts}
    if found != set(body.contact_ids):
        raise HTTPException(status_code=404, detail="One or more contacts were not found.")
    definition = PlanDefinition.model_validate(_json(revision["definition"], {}))
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for contact in contacts:
        if contact["id"] in active_contact_ids:
            blockers.append(
                {
                    "contact_id": contact["id"],
                    "step_key": "enrollment",
                    "code": "already_enrolled",
                    "message": "Contact already has an active or paused enrollment in this Smart Plan",
                }
            )
        for step in definition.steps:
            if step.type == "task" and not contact.get("legacy_client_id"):
                blockers.append(
                    {
                        "contact_id": contact["id"],
                        "step_key": step.key,
                        "code": "missing_client_anchor",
                        "message": "Task steps require this contact to be linked to a CRM client",
                    }
                )
                continue
            channel: Optional[Channel] = None
            destination: Optional[str] = None
            provider_key: Optional[str] = None
            if step.type == "email":
                channel, destination, provider_key = Channel.EMAIL, contact.get("email"), "email"
            elif step.type == "sms":
                channel, destination, provider_key = Channel.SMS, contact.get("phone"), "sms"
            elif step.type == "approved_call":
                channel, destination, provider_key = Channel.VOICE, contact.get("phone"), "ai_call"
            if channel is None:
                continue
            if not destination:
                blockers.append({"contact_id": contact["id"], "step_key": step.key, "code": "missing_destination", "message": f"{channel.value} destination is missing"})
                continue
            if channel in {Channel.SMS, Channel.VOICE} and not contact.get("state_code"):
                blockers.append({"contact_id": contact["id"], "step_key": step.key, "code": "missing_state", "message": "state_code is required for calling-hours compliance"})
                continue
            if not provider["channels"].get(str(provider_key)):
                blockers.append({"contact_id": contact["id"], "step_key": step.key, "code": "provider_unavailable", "message": f"No verified {provider_key} delivery route is available"})
                continue
            decision = await guard_outreach(
                ctx,
                channel=channel,
                contact=str(destination),
                state_code=contact.get("state_code"),
                tz_name=contact.get("timezone"),
                log=False,
                voice_mode=VoiceMode.AI,
                recording_enabled=True,
            )
            blockers.extend(
                {"contact_id": contact["id"], "step_key": step.key, "code": "compliance_block", "message": message}
                for message in decision.blockers
            )
            warnings.extend(
                {"contact_id": contact["id"], "step_key": step.key, "message": message}
                for message in decision.warnings
            )
    contact_facts = [
        {"id": contact["id"], "updated_at": contact.get("updated_at"), "state_code": contact.get("state_code"), "timezone": contact.get("timezone")}
        for contact in contacts
    ]
    fingerprint = _hash(
        {
            "revision_hash": revision["definition_hash"],
            "contacts": contact_facts,
            "channels": provider["channels"],
            "start_at": body.start_at.isoformat(),
        }
    )
    token_payload = {
        "version": 1,
        "tenant_id": ctx.tenant_id,
        "plan_id": plan_id,
        "revision_id": str(revision["id"]),
        "contact_ids": sorted(body.contact_ids),
        "start_at": body.start_at.isoformat(),
        "fingerprint": fingerprint,
        "issued_at": int(datetime.now(timezone.utc).timestamp()),
    }
    return {
        "plan_id": plan_id,
        "plan_name": revision["name"],
        "revision_id": str(revision["id"]),
        "revision_number": revision["revision_number"],
        "contact_count": len(contacts),
        "blockers": blockers,
        "warnings": warnings,
        "can_enroll": not blockers,
        "schedule": [
            {"step_key": step.key, "type": step.type, "scheduled_for": (body.start_at + timedelta(minutes=sum(item.delay_minutes for item in definition.steps[: index + 1]))).isoformat()}
            for index, step in enumerate(definition.steps)
        ],
        "fingerprint": fingerprint,
        "preview_token": _sign_preview(token_payload),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=_PREVIEW_TTL_SECONDS)).isoformat(),
    }


@router.post("/plans/{plan_id}/preview")
async def preview_plan(plan_id: str, body: PreviewRequest, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    return await _preview(ctx, _uuid(plan_id, "plan_id"), body)


@router.post("/plans/{plan_id}/enroll", status_code=status.HTTP_201_CREATED)
async def enroll_plan(plan_id: str, body: EnrollmentCreate, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_feature(Feature.SMART_PLANS)
    plan_id = _uuid(plan_id, "plan_id")
    supplied = _verify_preview(body.preview_token)
    if supplied.get("tenant_id") != ctx.tenant_id or supplied.get("plan_id") != plan_id:
        raise HTTPException(status_code=422, detail="Preview does not belong to this tenant and plan.")
    if sorted(supplied.get("contact_ids") or []) != sorted(body.contact_ids) or supplied.get("start_at") != body.start_at.isoformat():
        raise HTTPException(status_code=409, detail="Enrollment selection changed; preview again.")
    current = await _preview(ctx, plan_id, PreviewRequest(contact_ids=body.contact_ids, start_at=body.start_at))
    if current["fingerprint"] != supplied.get("fingerprint"):
        raise HTTPException(status_code=409, detail="Contact, provider, or plan facts changed; preview again.")
    if current["blockers"]:
        raise HTTPException(status_code=409, detail={"message": "Enrollment is blocked.", "blockers": current["blockers"]})
    async with tenant_tx(ctx) as conn:
        revision = await conn.fetchrow("SELECT definition FROM smart_plan_revisions WHERE id=$1::uuid", current["revision_id"])
        definition = PlanDefinition.model_validate(_json(revision["definition"], {}))
        enrollment_rows: list[Any] = []
        run_rows: list[Any] = []
        for contact_id in body.contact_ids:
            enrollment = await conn.fetchrow(
                """
                INSERT INTO smart_plan_enrollments (
                    tenant_id,plan_id,revision_id,contact_id,status,next_run_at,preview_hash,created_by
                ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,'active',$5,$6,$7)
                RETURNING *
                """,
                ctx.tenant_id,
                plan_id,
                current["revision_id"],
                contact_id,
                body.start_at,
                current["fingerprint"],
                ctx.agent_id,
            )
            enrollment_rows.append(enrollment)
            elapsed = 0
            for index, step in enumerate(definition.steps):
                elapsed += step.delay_minutes
                run = await conn.fetchrow(
                    """
                    INSERT INTO smart_plan_step_runs (
                        tenant_id,enrollment_id,step_key,step_index,step_type,scheduled_for,state
                    ) VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,'scheduled') RETURNING *
                    """,
                    ctx.tenant_id,
                    str(enrollment["id"]),
                    step.key,
                    index,
                    step.type,
                    body.start_at + timedelta(minutes=elapsed),
                )
                run_rows.append(run)
    jobs: list[dict[str, Any]] = []
    for run in run_rows:
        job, _ = await enqueue_job(
            ctx,
            job_type="smart-plan:dispatch",
            payload={"step_run_id": str(run["id"])},
            idempotency_key=f"smart-plan:{run['id']}:0",
            created_by=ctx.agent_id,
            scheduled_at=run["scheduled_for"],
            priority=20,
            risk=ActionRisk.READ_ONLY,
        )
        jobs.append(job)
        async with tenant_tx(ctx) as conn:
            await conn.execute("UPDATE smart_plan_step_runs SET job_id=$2::uuid WHERE id=$1::uuid", str(run["id"]), job["id"])
    return {"created": len(enrollment_rows), "enrollment_ids": [str(row["id"]) for row in enrollment_rows], "scheduled_steps": len(run_rows), "job_ids": [job["id"] for job in jobs]}


async def _set_enrollment_state(ctx: TenantContext, enrollment_id: str, action: str) -> dict[str, Any]:
    enrollment_id = _uuid(enrollment_id, "enrollment_id")
    async with tenant_tx(ctx) as conn:
        enrollment = await conn.fetchrow("SELECT * FROM smart_plan_enrollments WHERE id=$1::uuid FOR UPDATE", enrollment_id)
        if enrollment is None:
            raise HTTPException(status_code=404, detail="Enrollment not found.")
        if not (ctx.is_platform_admin or ctx.is_broker_owner) and enrollment["created_by"] != ctx.agent_id:
            raise HTTPException(status_code=404, detail="Enrollment not found.")
        if action == "pause":
            if enrollment["status"] != "active":
                raise HTTPException(status_code=409, detail=f"Enrollment is {enrollment['status']}.")
            await conn.execute("UPDATE smart_plan_enrollments SET status='paused',paused_at=now() WHERE id=$1::uuid", enrollment_id)
            await conn.execute("UPDATE smart_plan_step_runs SET state='paused' WHERE enrollment_id=$1::uuid AND state='scheduled'", enrollment_id)
            await conn.execute("UPDATE automation_jobs SET state='cancelled',completed_at=now() WHERE id IN (SELECT job_id FROM smart_plan_step_runs WHERE enrollment_id=$1::uuid AND state='paused') AND state IN ('queued','failed')", enrollment_id)
        elif action == "cancel":
            if enrollment["status"] in {"completed", "cancelled"}:
                raise HTTPException(status_code=409, detail=f"Enrollment is {enrollment['status']}.")
            await conn.execute("UPDATE smart_plan_enrollments SET status='cancelled',cancelled_at=now(),next_run_at=NULL WHERE id=$1::uuid", enrollment_id)
            await conn.execute("UPDATE smart_plan_step_runs SET state='cancelled',finished_at=now() WHERE enrollment_id=$1::uuid AND state IN ('scheduled','paused')", enrollment_id)
            await conn.execute("UPDATE automation_jobs SET state='cancelled',completed_at=now() WHERE id IN (SELECT job_id FROM smart_plan_step_runs WHERE enrollment_id=$1::uuid) AND state IN ('queued','failed')", enrollment_id)
        else:
            if enrollment["status"] != "paused":
                raise HTTPException(status_code=409, detail=f"Enrollment is {enrollment['status']}.")
            next_resume = int(enrollment["resume_count"] or 0) + 1
            runs = await conn.fetch("SELECT * FROM smart_plan_step_runs WHERE enrollment_id=$1::uuid AND state='paused' ORDER BY step_index", enrollment_id)
            await conn.execute("UPDATE smart_plan_enrollments SET status='active',paused_at=NULL,resume_count=$2,next_run_at=now() WHERE id=$1::uuid", enrollment_id, next_resume)
            await conn.execute("UPDATE smart_plan_step_runs SET state='scheduled',scheduled_for=GREATEST(scheduled_for,now()) WHERE enrollment_id=$1::uuid AND state='paused'", enrollment_id)
    if action == "resume":
        for run in runs:
            scheduled_for = max(run["scheduled_for"], datetime.now(timezone.utc))
            job, _ = await enqueue_job(
                ctx,
                job_type="smart-plan:dispatch",
                payload={"step_run_id": str(run["id"])},
                idempotency_key=f"smart-plan:{run['id']}:{next_resume}",
                created_by=ctx.agent_id,
                scheduled_at=scheduled_for,
                priority=20,
                risk=ActionRisk.READ_ONLY,
            )
            async with tenant_tx(ctx) as conn:
                await conn.execute("UPDATE smart_plan_step_runs SET job_id=$2::uuid WHERE id=$1::uuid", str(run["id"]), job["id"])
    return {"enrollment_id": enrollment_id, "status": {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]}


@router.post("/plans/enrollments/{enrollment_id}/pause")
async def pause_enrollment(enrollment_id: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    return await _set_enrollment_state(ctx, enrollment_id, "pause")


@router.post("/plans/enrollments/{enrollment_id}/resume")
async def resume_enrollment(enrollment_id: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    return await _set_enrollment_state(ctx, enrollment_id, "resume")


@router.post("/plans/enrollments/{enrollment_id}/cancel")
async def cancel_enrollment(enrollment_id: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    return await _set_enrollment_state(ctx, enrollment_id, "cancel")


@router.get("/providers")
async def providers(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    snapshot = await _provider_snapshot(ctx)
    return {"providers": list(snapshot["providers"].values()), "channels": snapshot["channels"], "route": snapshot["route"], "credential_exposed": False}


@router.put("/providers/{provider}")
async def configure_provider(provider: str, body: ProviderSetupInput, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    provider = provider.lower()
    payload = _provider_payload(provider, body)
    stored = await store_provider_credential(
        provider,
        ProviderCredentialInput(
            account_label=body.account_label,
            token=_canonical(payload),
            scopes=["email"] if provider == "ses" else ["voice", "sms"] if provider in {"twilio", "acs"} else [],
        ),
        ctx,
    )
    return {**stored, "validation_status": "unverified", "next_step": "Validate this provider before using it for delivery."}


@router.post("/providers/{provider}/{account_label}/validate")
async def validate_provider(provider: str, account_label: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    provider = provider.lower()
    if provider not in {"twilio", "acs", "ses"}:
        raise HTTPException(status_code=422, detail="Google health is managed by OAuth refresh.")
    raw = await _load_provider_credential(ctx, provider, account_label)
    if not raw:
        raise HTTPException(status_code=404, detail="Active provider credential not found.")
    try:
        credentials = json.loads(raw)
        if not isinstance(credentials, dict):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Stored provider credential is invalid.")
    capabilities: dict[str, bool] = {}
    error: Optional[str] = None
    try:
        if provider == "twilio":
            from twilio.rest import Client

            def _check_twilio() -> None:
                client = (
                    Client(credentials["api_key"], credentials["api_secret"], credentials["account_sid"])
                    if credentials.get("api_key")
                    else Client(credentials["account_sid"], credentials["auth_token"])
                )
                client.api.accounts(credentials["account_sid"]).fetch()

            await asyncio.wait_for(asyncio.to_thread(_check_twilio), timeout=15.0)
            capabilities = {
                "voice": bool(credentials.get("from_number") and credentials.get("twiml_app_sid")),
                "sms": bool(credentials.get("sms_sender") and credentials.get("sms_sender_type")),
                "agent_call": bool(
                    credentials.get("auth_token")
                    and credentials.get("api_key")
                    and credentials.get("api_secret")
                    and credentials.get("twiml_app_sid")
                    and credentials.get("from_number")
                ),
            }
        elif provider == "smtp":
            import smtp_mailer

            # Prove the credential end to end: connect, negotiate TLS and
            # authenticate, then quit without sending. A tenant pasting an app
            # password should find out here, not on their first outreach.
            def _check_smtp() -> None:
                import smtplib
                import ssl as _ssl

                settings = smtp_mailer.resolve_settings(credentials)
                context = _ssl.create_default_context()
                if settings["port"] == smtp_mailer.IMPLICIT_TLS_PORT:
                    client = smtplib.SMTP_SSL(
                        settings["host"], settings["port"], timeout=10.0, context=context
                    )
                else:
                    client = smtplib.SMTP(settings["host"], settings["port"], timeout=10.0)
                with client:
                    if settings["port"] != smtp_mailer.IMPLICIT_TLS_PORT:
                        client.ehlo()
                        if not client.has_extn("starttls"):
                            raise smtp_mailer.SmtpConfigurationError(
                                "server does not offer STARTTLS"
                            )
                        client.starttls(context=context)
                        client.ehlo()
                    if settings["username"]:
                        client.login(settings["username"], settings["password"])

            await asyncio.wait_for(asyncio.to_thread(_check_smtp), timeout=20.0)
            capabilities = {"email": True}
        elif provider == "acs":
            if "endpoint=https://" not in str(credentials.get("connection_string") or "").lower():
                raise ProviderConfigurationError("ACS endpoint is invalid")
            # One ACS resource backs voice, SMS and email; each capability is
            # gated on the sender identity that channel actually requires.
            from_email = str(credentials.get("from_email") or "").strip()
            if from_email and "@" not in from_email:
                raise ProviderConfigurationError("ACS sender email is invalid")
            capabilities = {
                "voice": bool(credentials.get("from_number")),
                "sms": bool(credentials.get("sms_sender")),
                "email": bool(from_email),
            }
        else:
            import boto3
            from botocore.config import Config

            def _check_ses() -> None:
                client_options: dict[str, Any] = {
                    "region_name": credentials.get("region") or "us-east-2",
                    "config": Config(
                        connect_timeout=5,
                        read_timeout=10,
                        retries={"max_attempts": 1},
                    ),
                }
                if credentials.get("aws_access_key_id"):
                    client_options["aws_access_key_id"] = credentials["aws_access_key_id"]
                    client_options["aws_secret_access_key"] = credentials["aws_secret_access_key"]
                    if credentials.get("aws_session_token"):
                        client_options["aws_session_token"] = credentials["aws_session_token"]
                client = boto3.client("sesv2", **client_options)
                client.get_account()

            await asyncio.wait_for(asyncio.to_thread(_check_ses), timeout=15.0)
            capabilities = {"email": True}
    except Exception as exc:
        error = (str(exc).strip() or exc.__class__.__name__)[:500]
    validation_status = "invalid" if error else "valid"
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            UPDATE provider_credentials
               SET validation_status=$3,validation_error=$4,
                   validated_capabilities=$5::jsonb,last_validated_at=now(),updated_at=now()
             WHERE provider=$1 AND account_label=$2 AND disabled_at IS NULL
            """,
            provider,
            account_label,
            validation_status,
            error,
            _canonical(capabilities),
        )
    return {"provider": provider, "account_label": account_label, "validation_status": validation_status, "capabilities": capabilities, "error": error, "credential_exposed": False}


@router.delete("/providers/{provider}/{account_label}")
async def disconnect_provider(provider: str, account_label: str, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    return await disable_provider_credential(provider, account_label, ctx)


async def _smart_plan_dispatch(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
    step_run_id = str(payload.get("step_run_id") or "")
    tenant_id = str(reporter.job["tenant_id"])
    worker_ctx = TenantContext(agent_id="smart-plan-worker", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    async with tenant_tx(worker_ctx) as conn:
        run = await conn.fetchrow(
            """
            SELECT run.*,enrollment.status AS enrollment_status,enrollment.contact_id,
                   enrollment.created_by,revision.definition
              FROM smart_plan_step_runs run
              JOIN smart_plan_enrollments enrollment ON enrollment.id=run.enrollment_id
              JOIN smart_plan_revisions revision ON revision.id=enrollment.revision_id
             WHERE run.id=$1::uuid FOR UPDATE OF run
            """,
            step_run_id,
        )
        if run is None:
            raise RuntimeError("Smart Plan step run not found")
        if run["state"] in {"succeeded", "awaiting_approval", "cancelled", "skipped"}:
            return {"state": run["state"], "idempotent": True}
        if run["enrollment_status"] != "active":
            return {"state": run["enrollment_status"], "dispatched": False}
        await conn.execute("UPDATE smart_plan_step_runs SET state='running',started_at=COALESCE(started_at,now()),attempt_count=attempt_count+1 WHERE id=$1::uuid", step_run_id)
        contact_row = await conn.fetchrow(_CONTACT_SELECT + " WHERE contact.id=$1::uuid", str(run["contact_id"]))
        contact = await _contact_json(conn, worker_ctx, contact_row)
    definition = PlanDefinition.model_validate(_json(run["definition"], {}))
    step = next((item for item in definition.steps if item.key == run["step_key"]), None)
    if step is None:
        raise RuntimeError("Published Smart Plan step is missing")
    actor_ctx = TenantContext(agent_id=str(run["created_by"]), tenant_id=tenant_id, role=Role.AGENT)
    try:
        if step.type == "wait":
            result_state, command_id, task_id = "succeeded", None, None
        elif step.type == "task":
            async with tenant_tx(actor_ctx) as conn:
                task = await conn.fetchrow(
                    """
                    INSERT INTO client_tasks (tenant_id,client_id,title,details,due_at,status,priority,assignee_id,created_by)
                    VALUES ($1::uuid,$2::uuid,$3,$4,now(),'open',$5,$6,$6) RETURNING id
                    """,
                    tenant_id,
                    contact.get("legacy_client_id"),
                    step.title,
                    step.body or "Created by Smart Plan.",
                    step.priority,
                    str(run["created_by"]),
                )
            result_state, command_id, task_id = "succeeded", None, str(task["id"])
        else:
            target = {
                "contact_id": contact["id"],
                "client_id": contact.get("legacy_client_id"),
                "state_code": contact.get("state_code"),
                "timezone": contact.get("timezone"),
            }
            if step.type == "email":
                target["email"] = contact.get("email")
                command_type = CommandType.EMAIL
                draft = {"subject": step.subject, "body": step.body}
            elif step.type == "sms":
                target["phone"] = contact.get("phone")
                command_type = CommandType.SMS
                draft = {"body": step.body}
            else:
                target["phone"] = contact.get("phone")
                command_type = CommandType.CALL
                draft = {"script": step.body}
            staged = await create_command(
                CommandCreate(
                    command_type=command_type,
                    target=target,
                    draft=draft,
                    idempotency_key=f"smart-plan:{step_run_id}",
                    context={"source": "smart-plan", "step_run_id": step_run_id, "contact_id": contact["id"]},
                ),
                actor_ctx,
            )
            result_state, command_id, task_id = "awaiting_approval", str(staged["command"]["id"]), None
        async with tenant_tx(worker_ctx) as conn:
            await conn.execute(
                """
                UPDATE smart_plan_step_runs
                   SET state=$2,command_id=$3::uuid,task_id=$4::uuid,
                       finished_at=CASE WHEN $2='succeeded' THEN now() ELSE finished_at END,
                       blocker=NULL,last_error=NULL
                 WHERE id=$1::uuid
                """,
                step_run_id,
                result_state,
                command_id,
                task_id,
            )
            await conn.execute(
                """
                UPDATE smart_plan_enrollments
                   SET current_step_index=GREATEST(current_step_index,$2+1),
                       next_run_at=(SELECT min(scheduled_for) FROM smart_plan_step_runs
                                     WHERE enrollment_id=$1::uuid AND state='scheduled')
                 WHERE id=$1::uuid
                """,
                str(run["enrollment_id"]),
                int(run["step_index"]),
            )
        await reporter.progress(100, "Smart Plan step staged" if command_id else "Smart Plan step complete")
        return {"state": result_state, "command_id": command_id, "task_id": task_id, "requires_approval": bool(command_id)}
    except Exception as exc:
        async with tenant_tx(worker_ctx) as conn:
            await conn.execute("UPDATE smart_plan_step_runs SET state='blocked',blocker=$2,last_error=$2,finished_at=now() WHERE id=$1::uuid", step_run_id, (str(exc).strip() or exc.__class__.__name__)[:2_000])
            await conn.execute("UPDATE smart_plan_enrollments SET status='blocked' WHERE id=$1::uuid", str(run["enrollment_id"]))
        return {"state": "blocked", "error": (str(exc).strip() or exc.__class__.__name__)[:500]}


register_handler("smart-plan:dispatch", _smart_plan_dispatch)
