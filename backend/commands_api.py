"""Approval-gated EMAIL, CALL, and CALENDAR command router."""

from __future__ import annotations

import asyncio
import base64
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request
import urllib.parse
from xml.sax.saxutils import escape as _xml_escape
import uuid
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

import aiohttp
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.responses import RedirectResponse, Response

import ws_hub
from agent_profile import load_agent_identity
from approval_service import create_approval, decide_approval, list_approvals
from audit_ledger import AuditCategory, ledger
from automation_jobs import enqueue_job, register_handler
from command_providers import (
    ProviderConfigurationError,
    ProviderRejectedError,
    abort_twilio_call,
    create_google_calendar_event,
    place_acs_call,
    place_custom_http_call,
    place_twilio_call,
    send_gmail,
    send_ses_email,
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

if TYPE_CHECKING:
    from agent_mind import MindService

logger = logging.getLogger("oracle.commands")

router = APIRouter(prefix="/api/commands", tags=["commands"])
_mind_service: Optional["MindService"] = None


def _provider_submission_is_uncertain(
    submission_started: bool,
    exc: Exception,
) -> bool:
    """Return true only when a provider might have accepted the side effect."""
    return submission_started and not isinstance(
        exc,
        (ProviderConfigurationError, ProviderRejectedError),
    )


def configure_command_mind_service(service: "MindService") -> None:
    """Bind the process-wide MindService after server startup wiring."""
    global _mind_service
    _mind_service = service


class CommandType(str, Enum):
    EMAIL = "EMAIL"
    CALL = "CALL"
    CALENDAR = "CALENDAR"


class ParsedIntent(str, Enum):
    EMAIL = "EMAIL"
    CALL = "CALL"
    CALENDAR = "CALENDAR"
    CONTRACT = "CONTRACT"
    MAO_CALC = "MAO_CALC"


_RISK = {
    CommandType.EMAIL: ActionRisk.OUTREACH,
    CommandType.CALL: ActionRisk.LIVE_CALL,
    CommandType.CALENDAR: ActionRisk.CALENDAR_WRITE,
}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _require_webhook_secret(request: Request, env_name: str) -> None:
    expected = os.getenv(env_name, "").strip()
    supplied = request.query_params.get("token", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured.")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid webhook credential.")


def _twilio_webhook_url(request: Request, suffix: str) -> str:
    public_base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    if public_base:
        return f"{public_base}{suffix}"

    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    if not host:
        raise HTTPException(
            status_code=400,
            detail="Unable to determine webhook URL for Twilio signature validation.",
        )
    return f"{scheme}://{host}{suffix}"


def _twilio_tokens() -> list[str]:
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    return [token] if token else []


def _normalize_twilio_form(form: Any) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in form.multi_items():
        if not isinstance(key, str):
            key = str(key)
        if isinstance(value, str):
            normalized[key] = value
    return normalized


def _validate_twilio_webhook_signature(
    request: Request,
    form: Any,
    suffix: str,
) -> None:
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Twilio-Signature header.")

    tokens = _twilio_tokens()
    if not tokens:
        raise HTTPException(
            status_code=503,
            detail="Twilio webhook cannot be validated because no auth token is configured.",
        )

    params = _normalize_twilio_form(form)
    webhook_url = _twilio_webhook_url(request, suffix)

    try:
        from twilio.request_validator import RequestValidator
    except Exception:
        logger.exception("twilio request validator import failed")
        raise HTTPException(
            status_code=503,
            detail="Twilio request validation is unavailable.",
        )

    for token in tokens:
        if RequestValidator(token).validate(webhook_url, params, signature):
            return

    logger.warning(
        "twilio webhook signature validation failed path=%s",
        suffix,
    )
    raise HTTPException(status_code=400, detail="Invalid Twilio signature.")


class ClassificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=2, max_length=4_000)


class CommandParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    raw_text: str = Field(min_length=2, max_length=4_000)
    client_id: Optional[str] = None
    property_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=240)

    @field_validator("client_id", "property_id")
    @classmethod
    def validate_optional_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        uuid.UUID(value)
        return value


class CommandExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    command_id: Optional[str] = None
    intent: Optional[ParsedIntent] = None
    target: Optional[dict[str, Any]] = None
    draft_payload: Optional[dict[str, Any]] = None
    context: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=240)
    reason: str = Field(min_length=8, max_length=500)

    @field_validator("command_id")
    @classmethod
    def validate_command_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        uuid.UUID(value)
        return value

    @model_validator(mode="after")
    def validate_execute_reference(self) -> "CommandExecuteRequest":
        if self.command_id:
            return self
        if self.intent is ParsedIntent.CONTRACT:
            if not self.draft_payload or not self.idempotency_key:
                raise ValueError("contract execution requires draft_payload and idempotency_key")
            return self
        if not self.intent or self.intent not in {
            ParsedIntent.EMAIL,
            ParsedIntent.CALL,
            ParsedIntent.CALENDAR,
        }:
            raise ValueError("command_id is required for this action")
        if not self.target or not self.draft_payload or not self.idempotency_key:
            raise ValueError("new execution requires target, draft_payload, and idempotency_key")
        return self


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


class GoogleOAuthStart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    return_path: str = Field(default="/", min_length=1, max_length=500)

    @field_validator("return_path")
    @classmethod
    def validate_return_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("return_path must be a local application path")
        return value


_COMMAND_PROVIDERS = {"google", "acs", "ses", "twilio"}
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
)
_OAUTH_STATE_TTL = timedelta(minutes=10)


class GoogleOAuthError(RuntimeError):
    """Safe provider-linking error that never contains client credentials."""


def _google_oauth_settings() -> tuple[str, str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if not redirect_uri:
        public_base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
        if public_base:
            redirect_uri = f"{public_base}/api/commands/providers/google/oauth/callback"
    if not all((client_id, client_secret, redirect_uri)):
        raise HTTPException(
            status_code=503,
            detail="Google OAuth client configuration is incomplete.",
        )
    if not redirect_uri.startswith("https://") and not (
        os.getenv("ORACLE_ENV", "").lower() in {"dev", "development", "local", "test"}
        and redirect_uri.startswith("http://")
    ):
        raise HTTPException(status_code=503, detail="Google OAuth redirect URI must use HTTPS.")
    return client_id, client_secret, redirect_uri


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def _exchange_google_oauth_code(
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_GOOGLE_TOKEN_URL, data=payload) as response:
                data = await response.json(content_type=None)
                if response.status < 200 or response.status >= 300:
                    reason = str(data.get("error") or "token_exchange_failed")[:120]
                    raise GoogleOAuthError(f"Google rejected the authorization code: {reason}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GoogleOAuthError("Google OAuth token service was unavailable.") from exc
    if not isinstance(data, dict) or not str(data.get("access_token") or ""):
        raise GoogleOAuthError("Google OAuth response did not include an access token.")
    return data


async def _refresh_google_oauth_token(
    *, refresh_token: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            ) as response:
                data = await response.json(content_type=None)
                if response.status < 200 or response.status >= 300:
                    reason = str(data.get("error") or "refresh_failed")[:120]
                    raise GoogleOAuthError(f"Google rejected the refresh token: {reason}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GoogleOAuthError("Google OAuth token service was unavailable.") from exc
    if not isinstance(data, dict) or not str(data.get("access_token") or ""):
        raise GoogleOAuthError("Google OAuth refresh did not include an access token.")
    return data


def _oauth_return_url(return_path: str, outcome: str) -> str:
    base = os.getenv("ORACLE_BASE_URL", "http://localhost:5173").rstrip("/")
    parsed = urllib.parse.urlsplit(return_path)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("google", outcome))
    return base + urllib.parse.urlunsplit(
        ("", "", parsed.path or "/", urllib.parse.urlencode(query), parsed.fragment)
    )


async def _load_google_access_token(ctx: TenantContext, account_label: str) -> Optional[str]:
    """Load an agent token, refreshing it before expiry without exposing either token."""
    key = _provider_key(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT id,token_ciphertext,refresh_ciphertext,expires_at
              FROM provider_credentials
             WHERE tenant_id=$1::uuid AND provider='google'
               AND account_label=$2 AND disabled_at IS NULL
             ORDER BY updated_at DESC LIMIT 1
            """,
            ctx.tenant_id,
            account_label,
        )
        if row is None:
            return None
        if not row["expires_at"] or row["expires_at"] > datetime.now(timezone.utc) + timedelta(
            seconds=60
        ):
            return await decrypt_pii(conn, row["token_ciphertext"], key)
        if not row["refresh_ciphertext"]:
            return None
        refresh_token = await decrypt_pii(conn, row["refresh_ciphertext"], key)

    client_id, client_secret, _redirect_uri = _google_oauth_settings()
    token_data = await _refresh_google_oauth_token(
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )
    access_token = str(token_data["access_token"])
    if len(access_token) > 20_000:
        raise GoogleOAuthError("Google returned an invalid access token.")
    try:
        expires_in = max(1, min(int(token_data.get("expires_in") or 3_600), 86_400))
    except (TypeError, ValueError):
        expires_in = 3_600
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    async with tenant_tx(ctx) as conn:
        encrypted = await encrypt_pii(conn, access_token, key)
        await conn.execute(
            """
            UPDATE provider_credentials
               SET token_ciphertext=$2,expires_at=$3,last_validated_at=now(),updated_at=now()
             WHERE id=$1::uuid AND disabled_at IS NULL
            """,
            row["id"],
            encrypted,
            expires_at,
        )
    return access_token


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


def _parse_intent(raw_text: str) -> ParsedIntent:
    text = " ".join(raw_text.lower().split())
    patterns = (
        (ParsedIntent.MAO_CALC, r"\b(?:mao|max(?:imum)? allowable offer|offer ceiling)\b"),
        (ParsedIntent.CONTRACT, r"\b(?:contract|agreement|assignment)\b"),
        (ParsedIntent.CALENDAR, r"\b(?:calendar|schedule|book|meeting|appointment)\b"),
        (ParsedIntent.CALL, r"\b(?:call|phone|dial)\b"),
        (ParsedIntent.EMAIL, r"\b(?:email|e-mail|mail)\b"),
    )
    for intent, pattern in patterns:
        if re.search(pattern, text):
            return intent
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Command must request EMAIL, CALL, CALENDAR, CONTRACT, or MAO_CALC.",
    )


def _name_hint(raw_text: str, intent: ParsedIntent) -> str:
    verb = {
        ParsedIntent.EMAIL: r"(?:email|e-mail|mail)",
        ParsedIntent.CALL: r"(?:call|phone|dial)",
        ParsedIntent.CALENDAR: r"(?:schedule(?:\s+a)?(?:\s+call|\s+meeting)?\s+with|book(?:\s+a)?(?:\s+meeting)?\s+with)",
        ParsedIntent.CONTRACT: r"(?:contract(?:\s+for)?|agreement(?:\s+for)?)",
        ParsedIntent.MAO_CALC: r"(?:mao(?:\s+for)?|offer ceiling(?:\s+for)?)",
    }[intent]
    match = re.search(
        rf"\b{verb}\s+([A-Za-z][A-Za-z' -]{{0,79}}?)(?=\s+(?:and|about|regarding|for|to|at|on)\b|[,.]|$)",
        raw_text,
        re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match else ""


def _money_hint(raw_text: str) -> Optional[str]:
    match = re.search(
        r"(?:\$\s*|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
        raw_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return f"{value.quantize(Decimal('0.01'))}"


def _state_hint(raw_text: str) -> str:
    aliases = {
        "DE": ("DE", "Delaware"),
        "MD": ("MD", "Maryland"),
        "PA": ("PA", "Pennsylvania"),
    }
    for code, values in aliases.items():
        if any(re.search(rf"\b{re.escape(value)}\b", raw_text, re.IGNORECASE) for value in values):
            return code
    return ""


def _fallback_draft(
    intent: ParsedIntent,
    raw_text: str,
    *,
    profile: dict[str, Any],
    client: Optional[dict[str, Any]],
    property_context: Optional[dict[str, Any]],
) -> dict[str, Any]:
    name = str((client or {}).get("full_name") or "there").strip()
    first_name = name.split()[0] if name else "there"
    signature = str(profile.get("signature") or "").strip()
    price = _money_hint(raw_text)
    property_address = str((property_context or {}).get("address") or "").strip()
    subject_context = property_address or "your real estate plans"

    if intent is ParsedIntent.EMAIL:
        proposal = f" I would like to discuss a proposed amount of ${Decimal(price):,.2f}." if price else ""
        body = f"Hi {first_name},\n\nI’m following up regarding {subject_context}.{proposal}"
        if signature:
            body = f"{body}\n\n{signature}"
        return {"subject": f"Following up about {subject_context}", "body": body}
    if intent is ParsedIntent.CALL:
        return {
            "opening": f"Hi {first_name}, this is {profile.get('name') or 'your real estate agent'}.",
            "objective": raw_text,
            "property_address": property_address or None,
            "agent_phone": profile.get("phone_number") or None,
        }
    if intent is ParsedIntent.CALENDAR:
        return {
            "event": {
                "summary": f"Follow-up with {name}",
                "start": None,
                "end": None,
                "description": raw_text,
            }
        }
    if intent is ParsedIntent.CONTRACT:
        assignment = "assign" in raw_text.lower()
        return {
            "document_type": "assignment" if assignment else "seller_purchase",
            "doc_id": "assignment-standard" if assignment else "seller-purchase-standard",
            "state": (
                str(((property_context or {}).get("payload") or {}).get("state_code") or "").upper()
                or _state_hint(raw_text)
            ),
            "client_id": (client or {}).get("id"),
            "property_id": (property_context or {}).get("id"),
            "instructions": raw_text,
        }
    return {"formula": "ARV * 0.70 - Rehab", "property": property_context or {}}


async def _resolve_command_context(
    body: CommandParseRequest,
    intent: ParsedIntent,
    ctx: TenantContext,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], str]:
    hint = _name_hint(body.raw_text, intent)
    async with tenant_tx(ctx) as conn:
        client = None
        if body.client_id:
            client = await conn.fetchrow(
                """
                SELECT id,full_name,email,phone,client_type,stage
                  FROM clients
                 WHERE id=$1::uuid AND archived_at IS NULL
                """,
                body.client_id,
            )
            if client is None:
                raise HTTPException(status_code=404, detail="Client not found.")
        elif hint:
            matches = await conn.fetch(
                """
                SELECT id,full_name,email,phone,client_type,stage
                  FROM clients
                 WHERE archived_at IS NULL AND full_name ILIKE $1
                 ORDER BY CASE WHEN lower(full_name)=lower($2) THEN 0 ELSE 1 END,
                          full_name
                 LIMIT 3
                """,
                f"%{hint}%",
                hint,
            )
            if len(matches) > 1 and str(matches[0]["full_name"]).lower() != hint.lower():
                raise HTTPException(
                    status_code=409,
                    detail="More than one client matches that name; select a client first.",
                )
            client = matches[0] if matches else None

        property_row = None
        if body.property_id:
            property_row = await conn.fetchrow(
                """
                SELECT id,address,parcel_id,state,asking_price,payload
                  FROM leads
                 WHERE id=$1::uuid
                """,
                body.property_id,
            )
            if property_row is None:
                raise HTTPException(status_code=404, detail="Property not found.")

    client_data = _decode(dict(client)) if client else None
    property_data = _decode(dict(property_row)) if property_row else None
    if client_data and client_data.get("id") is not None:
        client_data["id"] = str(client_data["id"])
    if property_data and property_data.get("id") is not None:
        property_data["id"] = str(property_data["id"])
    if property_data and isinstance(property_data.get("payload"), str):
        property_data["payload"] = _decode(property_data["payload"])
    return client_data, property_data, hint


def _mao_payload(property_context: Optional[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    payload = dict((property_context or {}).get("payload") or {})
    arv_value = payload.get("arv") or payload.get("after_repair_value")
    rehab_value = payload.get("rehab") or payload.get("rehab_estimate")
    missing = [
        name
        for name, value in (("arv", arv_value), ("rehab", rehab_value))
        if value in (None, "")
    ]
    if missing:
        return {
            "formula": "ARV * 0.70 - Rehab",
            "arv": arv_value,
            "rehab": rehab_value,
            "mao": None,
        }, missing
    try:
        arv = Decimal(str(arv_value))
        rehab = Decimal(str(rehab_value))
    except InvalidOperation:
        return {"formula": "ARV * 0.70 - Rehab", "mao": None}, ["arv", "rehab"]
    mao = (arv * Decimal("0.70") - rehab).quantize(Decimal("0.01"))
    return {
        "formula": "ARV * 0.70 - Rehab",
        "arv": f"{arv.quantize(Decimal('0.01'))}",
        "rehab": f"{rehab.quantize(Decimal('0.01'))}",
        "mao": f"{mao}",
    }, []


@router.post("/parse")
async def parse_personal_command(
    body: CommandParseRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Classify and stage an editable, tenant-scoped Personal AI proposal."""
    require_feature(Feature.AUTOMATION)
    enforce_public_property_data(body.model_dump())
    intent = _parse_intent(body.raw_text)
    client, property_context, name_hint = await _resolve_command_context(body, intent, ctx)
    profile = await load_agent_identity(ctx)

    missing_fields: list[str] = []
    if intent is ParsedIntent.MAO_CALC:
        draft_payload, missing_fields = _mao_payload(property_context)
        confidence = 1.0 if not missing_fields else 0.55
    else:
        generated = None
        if _mind_service is not None:
            generated = await _mind_service.generate_command_draft(
                raw_text=body.raw_text,
                intent=intent.value,
                profile=profile,
                client=client,
                property_context=property_context,
            )
        draft_payload = (
            generated.get("draft_payload")
            if isinstance(generated, dict) and isinstance(generated.get("draft_payload"), dict)
            else _fallback_draft(
                intent,
                body.raw_text,
                profile=profile,
                client=client,
                property_context=property_context,
            )
        )
        try:
            confidence = max(0.0, min(float((generated or {}).get("confidence", 0.82)), 1.0))
        except (TypeError, ValueError):
            confidence = 0.82

    target = {
        "client_id": str(client["id"]) if client else body.client_id,
        "property_id": str(property_context["id"]) if property_context else body.property_id,
        "name": str(client["full_name"]) if client else name_hint,
        "email": str(client.get("email") or "") if client else "",
        "phone": str(client.get("phone") or "") if client else "",
    }
    command_id = None
    approval_id = None

    if intent is ParsedIntent.EMAIL and not target["email"]:
        missing_fields.append("client.email")
    if intent is ParsedIntent.CALL:
        if not target["phone"]:
            missing_fields.append("client.phone")
        draft_payload.setdefault("script", draft_payload.get("opening") or body.raw_text)
        target["state_code"] = str(
            (property_context or {}).get("state")
            or ((property_context or {}).get("payload") or {}).get("state_code")
            or ""
        ).upper()
        if len(target["state_code"]) != 2:
            missing_fields.append("property.state_code")
    if intent is ParsedIntent.CALENDAR:
        event = draft_payload.get("event") or {}
        if not event.get("start") or not event.get("end"):
            missing_fields.extend(["event.start", "event.end"])
    if intent is ParsedIntent.CONTRACT:
        assignment_requested = "assign" in body.raw_text.lower()
        draft_payload.setdefault(
            "document_type",
            "assignment" if assignment_requested else "seller_purchase",
        )
        draft_payload.setdefault(
            "doc_id",
            "assignment-standard" if assignment_requested else "seller-purchase-standard",
        )
        if not draft_payload.get("client_id"):
            draft_payload["client_id"] = target["client_id"]
        if not draft_payload.get("property_id"):
            draft_payload["property_id"] = target["property_id"]
        if not draft_payload.get("state"):
            draft_payload["state"] = str(
                (property_context or {}).get("state")
                or ((property_context or {}).get("payload") or {}).get("state_code")
                or _state_hint(body.raw_text)
            ).upper()
        if not draft_payload.get("client_id"):
            missing_fields.append("client_id")
        if not re.fullmatch(r"[A-Za-z]{2}", str(draft_payload.get("state") or "")):
            missing_fields.append("state")
        if not draft_payload.get("doc_id"):
            missing_fields.append("doc_id")

    if intent in {ParsedIntent.EMAIL, ParsedIntent.CALL, ParsedIntent.CALENDAR} and not missing_fields:
        create_body = CommandCreate(
            command_type=CommandType(intent.value),
            target=target,
            draft=draft_payload,
            idempotency_key=body.idempotency_key or f"parse:{uuid.uuid4()}",
            context={
                "client_id": target["client_id"],
                "property_id": target["property_id"],
                "profile_tone": profile["communication_tone"],
            },
        )
        staged = await create_command(create_body, ctx)
        command_id = str(staged["command"]["id"])
        approval = staged.get("approval") or {}
        approval_id = str(approval.get("id")) if approval.get("id") else None

    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="personal_ai_command_parsed",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=command_id or target.get("client_id"),
        metadata={
            "intent": intent.value,
            "requires_approval": intent is not ParsedIntent.MAO_CALC,
            "staged": bool(command_id),
            "missing_fields": sorted(set(missing_fields)),
        },
    )
    return {
        "intent": intent.value,
        "target_client_id": target.get("client_id"),
        "target_property_id": target.get("property_id"),
        "extracted_name": target.get("name") or "",
        "target": target,
        "draft_payload": draft_payload,
        "confidence": confidence,
        "requires_approval": intent is not ParsedIntent.MAO_CALC,
        "command_id": command_id,
        "approval_id": approval_id,
        "idempotency_key": body.idempotency_key,
        "missing_fields": sorted(set(missing_fields)),
    }


@router.post("/execute")
async def execute_personal_command(
    body: CommandExecuteRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Explicit HITL authorization wrapper over the durable command engine."""
    require_feature(Feature.AUTOMATION)
    if body.intent is ParsedIntent.CONTRACT and body.command_id is None:
        from media_api import ContractSynthesisRequest, synthesize_contract

        payload = dict(body.draft_payload or {})
        client_id = payload.get("client_id") or (body.target or {}).get("client_id")
        try:
            synthesis_request = ContractSynthesisRequest(
                client_id=client_id,
                doc_id=payload.get("doc_id"),
                state=payload.get("state"),
                financial_override=payload.get("financial_override") or {},
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Contract draft is missing a valid client_id, doc_id, or state.",
            ) from exc
        return await synthesize_contract(synthesis_request, 3600, ctx)

    command_id = body.command_id
    if command_id is None:
        staged = await create_command(
            CommandCreate(
                command_type=CommandType(body.intent.value),
                target=body.target or {},
                draft=body.draft_payload or {},
                idempotency_key=body.idempotency_key or f"execute:{uuid.uuid4()}",
                context=body.context,
            ),
            ctx,
        )
        command_id = str(staged["command"]["id"])

    row = await _get_command(ctx, command_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Command not found.")
    current_target = dict(_decode(row["target"]) or {})
    stored_draft = dict(_decode(row["draft"]) or {})
    current_draft = dict(stored_draft.get("content") or {})
    next_target = body.target if body.target is not None else current_target
    next_draft = body.draft_payload if body.draft_payload is not None else current_draft
    if next_target != current_target or next_draft != current_draft:
        await edit_command(
            command_id,
            CommandEdit(
                target=next_target,
                draft=next_draft,
                context=body.context or dict(stored_draft.get("context") or {}),
            ),
            ctx,
        )
    return await approve_command(command_id, ApprovalDecision(reason=body.reason), ctx)


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


@router.post("/providers/google/oauth/start")
async def start_google_oauth(
    body: GoogleOAuthStart,
    ctx: TenantContext = Depends(require_context),
):
    """Create a tenant-bound, single-use Google OAuth + PKCE authorization."""
    require_feature(Feature.AUTOMATION)
    client_id, _client_secret, redirect_uri = _google_oauth_settings()
    state_token = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state_token.encode("ascii")).hexdigest()
    code_verifier = secrets.token_urlsafe(64)
    challenge = _pkce_challenge(code_verifier)
    expires_at = datetime.now(timezone.utc) + _OAUTH_STATE_TTL
    key = _provider_key(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        verifier_ciphertext = await encrypt_pii(conn, code_verifier, key)
        await conn.execute(
            """
            INSERT INTO oauth_authorization_states (
                tenant_id,provider,state_hash,account_label,
                code_verifier_ciphertext,redirect_uri,return_path,scopes,
                expires_at,created_by
            ) VALUES ($1::uuid,'google',$2,$3,$4,$5,$6,$7::text[],$8,$9)
            """,
            ctx.tenant_id,
            state_hash,
            ctx.agent_id,
            verifier_ciphertext,
            redirect_uri,
            body.return_path,
            list(_GOOGLE_SCOPES),
            expires_at,
            ctx.agent_id,
        )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state_token,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {
        "provider": "google",
        "authorization_url": f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}",
        "expires_at": expires_at.isoformat(),
    }


@router.get("/providers/google/oauth/callback", include_in_schema=False)
async def finish_google_oauth(
    state_token: str = Query(alias="state", min_length=20, max_length=200),
    code: Optional[str] = Query(default=None, min_length=1, max_length=4_000),
    provider_error: Optional[str] = Query(default=None, alias="error", max_length=200),
):
    """Consume a Google callback without a browser JWT; the random state is the credential."""
    state_hash = hashlib.sha256(state_token.encode("utf-8")).hexdigest()
    platform_ctx = TenantContext(
        agent_id="google-oauth-callback",
        tenant_id=os.getenv(
            "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        ),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE oauth_authorization_states
               SET consumed_at=now()
             WHERE provider='google' AND state_hash=$1
               AND consumed_at IS NULL AND expires_at > now()
            RETURNING id,tenant_id,account_label,code_verifier_ciphertext,
                      redirect_uri,return_path,scopes,created_by
            """,
            state_hash,
        )
        if row is None:
            raise HTTPException(status_code=400, detail="OAuth state is invalid, expired, or already used.")
        tenant_id = str(row["tenant_id"])
        code_verifier = await decrypt_pii(
            conn,
            row["code_verifier_ciphertext"],
            _provider_key(tenant_id),
        )

    if provider_error or not code:
        return RedirectResponse(
            _oauth_return_url(row["return_path"], "denied"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        client_id, client_secret, configured_redirect = _google_oauth_settings()
        if configured_redirect != row["redirect_uri"]:
            raise GoogleOAuthError("OAuth redirect configuration changed; start again.")
        token_data = await _exchange_google_oauth_code(
            code=code,
            code_verifier=code_verifier,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=row["redirect_uri"],
        )
    except (GoogleOAuthError, HTTPException):
        return RedirectResponse(
            _oauth_return_url(row["return_path"], "error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    access_token = str(token_data["access_token"])
    refresh_token = str(token_data.get("refresh_token") or "") or None
    if len(access_token) > 20_000 or (refresh_token and len(refresh_token) > 20_000):
        raise HTTPException(status_code=502, detail="Google returned an invalid credential payload.")
    try:
        expires_in = max(1, min(int(token_data.get("expires_in") or 3_600), 86_400))
    except (TypeError, ValueError):
        expires_in = 3_600
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    returned_scopes = str(token_data.get("scope") or "").split()
    scopes = returned_scopes or list(row["scopes"] or _GOOGLE_SCOPES)
    callback_ctx = TenantContext(
        agent_id=str(row["created_by"]), tenant_id=tenant_id, role=Role.AGENT
    )
    key = _provider_key(tenant_id)
    async with tenant_tx(callback_ctx) as conn:
        encrypted_access = await encrypt_pii(conn, access_token, key)
        encrypted_refresh = (
            await encrypt_pii(conn, refresh_token, key) if refresh_token else None
        )
        credential = await conn.fetchrow(
            """
            INSERT INTO provider_credentials (
                tenant_id,provider,account_label,token_ciphertext,
                refresh_ciphertext,scopes,expires_at,last_validated_at,created_by
            ) VALUES ($1::uuid,'google',$2,$3,$4,$5::text[],$6,now(),$7)
            ON CONFLICT (tenant_id,provider,account_label) DO UPDATE SET
                token_ciphertext=EXCLUDED.token_ciphertext,
                refresh_ciphertext=COALESCE(
                    EXCLUDED.refresh_ciphertext,provider_credentials.refresh_ciphertext
                ),
                scopes=EXCLUDED.scopes,expires_at=EXCLUDED.expires_at,
                last_validated_at=now(),disabled_at=NULL,updated_at=now()
            RETURNING id
            """,
            tenant_id,
            str(row["account_label"]),
            encrypted_access,
            encrypted_refresh,
            scopes,
            expires_at,
            str(row["created_by"]),
        )
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="google_oauth_connected",
        tenant_id=tenant_id,
        user_id=str(row["created_by"]),
        target_id=str(credential["id"]),
        metadata={"scopes": scopes, "credential_exposed": False},
    )
    return RedirectResponse(
        _oauth_return_url(row["return_path"], "connected"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


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


@router.post("/webhooks/acs", include_in_schema=False)
async def acs_webhook(request: Request):
    _require_webhook_secret(request, "ORACLE_ACS_WEBHOOK_SECRET")
    body = await request.json()
    events = body if isinstance(body, list) else [body]
    for event in events:
        event_type = event.get("eventType") or event.get("type", "")
        data = event.get("data", {})
        print(f"[ACS-WEBHOOK] event_type={event_type} id={event.get('id','')[:20]}", flush=True)

        if event_type == "Microsoft.EventGrid.SubscriptionValidationEvent":
            validation_code = data.get("validationCode", "")
            return {"validationResponse": validation_code}

        if event_type == "Microsoft.Communication.IncomingCall":
            from_number = data.get("from", {}).get("phoneNumber", {}).get("value", "")
            context = data.get("incomingCallContext", "")
            if context:
                import asyncio as _asyncio
                _asyncio.ensure_future(_handle_incoming_acs_call(context, from_number))

        elif event_type == "Microsoft.Communication.CallDisconnected":
            cid = data.get("callConnectionId", "")
            if cid:
                _update_call_session(cid, "completed")
                from acs_call_handler import cleanup_call
                await cleanup_call(cid)

        elif event_type == "Microsoft.Communication.PlayCompleted":
            cid = data.get("callConnectionId", "")
            ctx = data.get("operationContext", "")
            if cid:
                from acs_call_handler import handle_play_completed
                import asyncio as _asyncio
                _asyncio.ensure_future(handle_play_completed(cid, ctx))

        elif event_type == "Microsoft.Communication.RecognizeCompleted":
            cid = data.get("callConnectionId", "")
            speech = data.get("speechResult", {}).get("speech", "")
            if cid and speech:
                from acs_call_handler import handle_speech_recognized
                import asyncio as _asyncio
                _asyncio.ensure_future(handle_speech_recognized(cid, speech))
            elif cid:
                from acs_call_handler import handle_no_input
                import asyncio as _asyncio
                _asyncio.ensure_future(handle_no_input(cid))

        elif event_type == "Microsoft.Communication.RecognizeFailed":
            cid = data.get("callConnectionId", "")
            if cid:
                from acs_call_handler import handle_no_input
                import asyncio as _asyncio
                _asyncio.ensure_future(handle_no_input(cid))

        elif event_type == "Microsoft.Communication.CreateCallFailed":
            cid = data.get("callConnectionId", "")
            reason = data.get("resultInformation", {}).get("message", "unknown")
            print(f"[ACS-WEBHOOK] CreateCallFailed cid={cid} reason={reason}", flush=True)
            if cid:
                _update_call_session(cid, "failed")
                from acs_call_handler import cleanup_call
                await cleanup_call(cid)

        elif event_type == "Microsoft.Communication.CallConnected":
            cid = data.get("callConnectionId", "")
            if cid:
                _update_call_session(cid, "in-progress")
                from acs_call_handler import start_outbound_conversation
                to_number = data.get("to", {}).get("phoneNumber", {}).get("value", "")
                import asyncio as _asyncio
                _asyncio.ensure_future(start_outbound_conversation(cid, to_number))

    return {"accepted": True}


@router.websocket("/media/acs")
async def acs_qwen_media(websocket: WebSocket):
    """Authenticated bidirectional ACS PCM stream bridged to Qwen Omni."""
    from qwen_omni_realtime import (
        QwenCallLimitReached,
        QwenOmniRealtimeBridge,
        QwenRealtimeError,
        verify_acs_websocket_jwt,
    )

    authorization = websocket.headers.get("authorization", "")
    is_authentic = await asyncio.to_thread(
        verify_acs_websocket_jwt,
        authorization,
    )
    if not is_authentic:
        await websocket.close(code=4403)
        return

    call_connection_id = websocket.headers.get("x-ms-call-connection-id", "").strip()
    if not call_connection_id:
        await websocket.close(code=4400)
        return
    from acs_call_handler import authorize_qwen_media_call

    if not await authorize_qwen_media_call(call_connection_id):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    bridge = QwenOmniRealtimeBridge(websocket, call_connection_id)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        logger.info("ACS media socket disconnected: cid=%s", call_connection_id)
    except QwenCallLimitReached:
        logger.info("Qwen realtime turn limit reached: cid=%s", call_connection_id)
        from acs_call_handler import end_qwen_call

        await end_qwen_call(call_connection_id)
    except QwenRealtimeError:
        logger.exception("Qwen realtime bridge failed: cid=%s", call_connection_id)
        from acs_call_handler import fallback_from_qwen_media

        await fallback_from_qwen_media(call_connection_id)
    except Exception:
        logger.exception(
            "Unexpected ACS/Qwen media bridge failure: cid=%s",
            call_connection_id,
        )
        from acs_call_handler import fallback_from_qwen_media

        await fallback_from_qwen_media(call_connection_id)


async def _handle_incoming_acs_call(incoming_call_context: str, from_number: str) -> None:
    from acs_call_handler import answer_incoming_call
    from command_providers import ProviderConfigurationError

    try:
        await answer_incoming_call(incoming_call_context, from_number)
    except ProviderConfigurationError as exc:
        import logging
        logging.getLogger("oracle.acs_webhook").error(
            "ACS configuration error — call rejected for %s: %s", from_number, exc
        )
    except Exception:
        import logging
        logging.getLogger("oracle.acs_webhook").exception(
            "Incoming call handler failed for %s", from_number
        )


def _update_call_session(call_connection_id: str, status: str) -> None:
    import asyncio as _asyncio
    _asyncio.ensure_future(_update_call_session_async(call_connection_id, status))


async def _update_call_session_async(call_connection_id: str, status: str) -> None:
    platform_ctx = TenantContext(
        agent_id="acs-webhook",
        tenant_id=os.getenv(
            "ORACLE_PLATFORM_TENANT_ID",
            "00000000-0000-0000-0000-000000000000",
        ),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        await conn.execute(
            """
            UPDATE live_call_sessions
               SET started_at=CASE WHEN $2='in-progress'
                                    THEN COALESCE(started_at,now()) ELSE started_at END,
                   ended_at=CASE WHEN $2='completed'
                                  THEN now() ELSE ended_at END
             WHERE provider_call_id=$1
            """,
            call_connection_id,
            status,
        )


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
            try:
                google_raw = await _load_provider_credential(ctx, "google")
                provider_result = await send_gmail({**draft, "target": target}, credentials=google_raw)
            except Exception:
                try:
                    provider_result = await send_gmail({**draft, "target": target})
                except Exception:
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
            submission_started = True
            twilio_raw = await _load_provider_credential(ctx, "twilio")
            twilio_credentials = None
            if twilio_raw:
                try:
                    twilio_credentials = json.loads(twilio_raw)
                except (TypeError, ValueError):
                    pass
            try:
                from twilio_call_handler import (
                    ensure_twilio_call_state_available,
                    initialize_twilio_call_state,
                )

                await ensure_twilio_call_state_available()
                provider_result = await place_twilio_call(
                    {**draft, "target": target}, credentials=twilio_credentials
                )
                account_sid = str(
                    (twilio_credentials or {}).get("account_sid")
                    or os.getenv("TWILIO_ACCOUNT_SID", "")
                )
                try:
                    await initialize_twilio_call_state(
                        provider_result.reference,
                        str(target.get("phone") or ""),
                        tenant_id=ctx.tenant_id,
                        account_sid=account_sid,
                    )
                except Exception:
                    await abort_twilio_call(
                        provider_result.reference,
                        credentials=twilio_credentials,
                    )
                    raise
            except ProviderConfigurationError:
                provider_result = None
                try:
                    if os.getenv("ORACLE_CUSTOM_CALL_API_URL", "").strip():
                        provider_result = await place_custom_http_call(
                            {**draft, "target": target}
                        )
                except ProviderConfigurationError:
                    provider_result = None
                if provider_result is None:
                    acs_raw = await _load_provider_credential(ctx, "acs")
                    acs_credentials = None
                    if acs_raw:
                        try:
                            acs_credentials = json.loads(acs_raw)
                        except (TypeError, ValueError) as exc:
                            raise ProviderConfigurationError(
                                "Encrypted ACS credential must be a JSON object"
                            ) from exc
                        if not isinstance(acs_credentials, dict):
                            raise ProviderConfigurationError(
                                "Encrypted ACS credential must be a JSON object"
                            )
                    from acs_call_handler import (
                        abort_unmanaged_call,
                        ensure_call_state_available,
                        initialize_call_state,
                    )

                    # A call without durable callback state is unmanaged. Verify
                    # Redis before dialing, then disconnect if the post-dial
                    # state write still loses a race with an outage.
                    await ensure_call_state_available()
                    provider_result = await place_acs_call(
                        {**draft, "target": target}, credentials=acs_credentials
                    )
                    try:
                        await initialize_call_state(
                            provider_result.reference,
                            str(target.get("phone") or ""),
                            tenant_id=ctx.tenant_id,
                            credentials=acs_credentials,
                        )
                    except Exception:
                        await abort_unmanaged_call(
                            provider_result.reference,
                            tenant_id=ctx.tenant_id,
                            credentials=acs_credentials,
                        )
                        raise
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
            google_token = await _load_google_access_token(
                ctx,
                account_label=str(reporter.job.get("created_by") or ""),
            )
            submission_started = True
            provider_result = await create_google_calendar_event(
                event_draft, access_token=google_token
            )
    except Exception as exc:
        uncertain = _provider_submission_is_uncertain(submission_started, exc)
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


# ── Twilio webhook (TwiML) ─────────────────────────────────────────────────────

@router.post("/webhooks/twilio", include_in_schema=False)
async def twilio_webhook(request: Request):
    """Return signed, state-bound TwiML for an approved outbound Twilio call."""
    from outreach_compliance import AI_VOICE_DISCLOSURE
    from twilio_call_handler import (
        create_twilio_bridge_token,
        load_twilio_call_state,
        twilio_media_websocket_url,
        twilio_qwen_enabled,
    )

    form = await request.form()
    _validate_twilio_webhook_signature(request, form, "/api/commands/webhooks/twilio")
    call_sid = str(form.get("CallSid") or "")
    state = await load_twilio_call_state(
        call_sid,
        wait_for_initialization=True,
    )
    if state is None:
        logger.error("Rejecting unmanaged Twilio call: sid=%s", call_sid)
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">This call cannot be connected safely. Goodbye.</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    greeting = (
        AI_VOICE_DISCLOSURE
        + " I'm NEOH, your real estate AI assistant. How can I help you today?"
    )
    if twilio_qwen_enabled(state):
        stream_url = twilio_media_websocket_url()
        bridge_token = create_twilio_bridge_token(call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{_xml_escape(greeting)}</Say>
    <Connect>
        <Stream url="{_xml_escape(stream_url)}">
            <Parameter name="bridge_token" value="{_xml_escape(bridge_token)}"/>
        </Stream>
    </Connect>
    <Say voice="Polly.Joanna">The realtime assistant is unavailable. Goodbye.</Say>
</Response>"""
        logger.info("Twilio Qwen Media Stream authorized in TwiML: sid=%s", call_sid)
        return Response(content=twiml, media_type="application/xml")

    speech_url = f"{os.getenv('ORACLE_PUBLIC_BASE_URL', '').rstrip('/')}/api/commands/webhooks/twilio/speech"
    gather_attribs = f'input="speech" action="{speech_url}" method="POST" timeout="5" speechTimeout="auto"'

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{_xml_escape(greeting)}</Say>
    <Gather {gather_attribs}>
        <Say voice="Polly.Joanna">I'm listening.</Say>
    </Gather>
    <Say voice="Polly.Joanna">I didn't catch that. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post("/webhooks/twilio/status", include_in_schema=False)
async def twilio_status_webhook(request: Request):
    """Consume signed Twilio call lifecycle events and release Redis state."""
    from twilio_call_handler import cleanup_twilio_call

    form = await request.form()
    _validate_twilio_webhook_signature(
        request,
        form,
        "/api/commands/webhooks/twilio/status",
    )
    call_sid = str(form.get("CallSid") or "")
    call_status = str(form.get("CallStatus") or "").strip().lower()
    if call_sid:
        if call_status in {"in-progress", "answered"}:
            _update_call_session(call_sid, "in-progress")
        elif call_status in {"completed", "busy", "failed", "no-answer", "canceled"}:
            _update_call_session(call_sid, "completed")
            await cleanup_twilio_call(call_sid)
    logger.info(
        "Twilio call status received: sid=%s status=%s",
        call_sid,
        call_status or "unknown",
    )
    return Response(status_code=204)


@router.websocket("/media/twilio")
async def twilio_qwen_media(websocket: WebSocket):
    """Authenticate and bridge Twilio's 8 kHz mu-law stream to Qwen Omni."""
    from qwen_omni_realtime import (
        QwenCallLimitReached,
        QwenRealtimeError,
        TwilioQwenRealtimeBridge,
    )
    from twilio_call_handler import (
        authorize_twilio_media,
        mark_twilio_streaming,
        verify_twilio_websocket_signature,
    )

    signature = websocket.headers.get("x-twilio-signature", "")
    is_authentic = await asyncio.to_thread(
        verify_twilio_websocket_signature,
        signature,
    )
    if not is_authentic:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    start_event: Optional[dict[str, Any]] = None
    try:
        for _ in range(2):
            raw_message = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=5.0,
            )
            if len(raw_message) > 64 * 1024:
                await websocket.close(code=4400)
                return
            event = json.loads(raw_message)
            if isinstance(event, dict) and event.get("event") == "start":
                start_event = event
                break
    except WebSocketDisconnect:
        return
    except (asyncio.TimeoutError, TypeError, ValueError):
        await websocket.close(code=4400)
        return

    if start_event is None:
        await websocket.close(code=4400)
        return
    start = start_event.get("start")
    if not isinstance(start, dict):
        await websocket.close(code=4400)
        return
    call_sid = str(start.get("callSid") or "")
    account_sid = str(start.get("accountSid") or "")
    parameters = start.get("customParameters")
    bridge_token = (
        str(parameters.get("bridge_token") or "")
        if isinstance(parameters, dict)
        else ""
    )
    if not await authorize_twilio_media(call_sid, account_sid, bridge_token):
        await websocket.close(code=4403)
        return

    try:
        bridge = TwilioQwenRealtimeBridge(
            websocket,
            call_sid,
            start_event,
        )
    except QwenRealtimeError:
        await websocket.close(code=4400)
        return
    await mark_twilio_streaming(call_sid)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        logger.info("Twilio media socket disconnected: sid=%s", call_sid)
    except QwenCallLimitReached:
        logger.info("Qwen realtime turn limit reached: sid=%s", call_sid)
        await abort_twilio_call(call_sid)
    except QwenRealtimeError:
        logger.exception("Twilio/Qwen realtime bridge failed: sid=%s", call_sid)
        await websocket.close(code=1011)
    except Exception:
        logger.exception("Unexpected Twilio/Qwen bridge failure: sid=%s", call_sid)
        await websocket.close(code=1011)


@router.post("/webhooks/twilio/speech", include_in_schema=False)
async def twilio_speech(request: Request):
    """Handle speech recognized by Twilio Gather. Calls the AI, returns new TwiML."""
    form = await request.form()
    _validate_twilio_webhook_signature(request, form, "/api/commands/webhooks/twilio/speech")
    speech_text = (form.get("SpeechResult") or "").strip()
    call_sid = form.get("CallSid", "")

    print(f"[TWILIO-SPEECH] CallSid={call_sid} speech={speech_text[:100]}", flush=True)

    if not speech_text:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Sorry, I didn't hear you. Goodbye.</Say>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    ai_reply = "I'm sorry, I'm having trouble processing that. Could you try again?"
    try:
        from ai_chat_agent import _generate_voice_reply
        ai_reply = await _generate_voice_reply(call_sid, speech_text)
    except Exception:
        import logging
        logging.getLogger("oracle.twilio").exception("Twilio AI reply failed")

    speech_url = f"{os.getenv('ORACLE_PUBLIC_BASE_URL', '').rstrip('/')}/api/commands/webhooks/twilio/speech"
    gather_attribs = f'input="speech" action="{speech_url}" method="POST" timeout="5" speechTimeout="auto"'

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{_xml_escape(ai_reply)}</Say>
    <Gather {gather_attribs}>
        <Say voice="Polly.Joanna"></Say>
    </Gather>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post("/webhooks/custom-call", include_in_schema=False)
async def custom_call_webhook(request: Request):
    """Generic webhook for custom internet telephony providers.

    Expected payload keys:
      event / type: call status or "speech"
      call_id / callId / id
      speech / speech_text / transcript
      Responses are returned inline. Caller-selected outbound URLs are rejected.
    """
    _require_webhook_secret(request, "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET")
    payload = await request.json()
    if not isinstance(payload, dict):
        return {"accepted": False, "error": "invalid payload"}

    event = str(payload.get("event") or payload.get("type") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    call_id = str(
        payload.get("call_id") or payload.get("callId") or payload.get("id") or ""
    ).strip()
    from_number = str(payload.get("from_number") or "")
    speech = str(
        payload.get("speech")
        or payload.get("speech_text")
        or payload.get("transcript")
        or ""
    ).strip()
    if payload.get("reply_url"):
        raise HTTPException(status_code=422, detail="reply_url is not supported.")

    print(
        f"[CUSTOM-CALL-WEBHOOK] event={event} status={status} call_id={call_id}",
        flush=True,
    )

    if event in {
        "call.connected",
        "connected",
        "answered",
        "inbound.connected",
        "outbound.connected",
    } or status in {"connected", "answered"}:
        if call_id:
            _update_call_session(call_id, "in-progress")

    if event in {
        "call.completed",
        "completed",
        "ended",
        "disconnected",
        "hangup",
        "failed",
    } or status in {"completed", "ended", "disconnected", "failed"}:
        if call_id:
            _update_call_session(call_id, "completed")

    if not speech:
        return {"accepted": True}

    try:
        from ai_chat_agent import _generate_voice_reply

        ai_reply = await _generate_voice_reply(from_number, speech)
    except Exception:
        logger.exception("Custom call AI reply failed for %s", call_id)
        ai_reply = "I'm sorry, I couldn't process that. Could you try again?"

    reply_payload = {
        "call_id": call_id,
        "event": "reply",
        "text": ai_reply,
        "tts": True,
    }
    return reply_payload
