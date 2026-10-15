"""Approval-gated EMAIL, SMS, CALL, and CALENDAR command router."""

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

import pan_redaction
import record_access
import sealed_text
import semantic_language
import ws_hub
from agent_profile import load_agent_identity
from approval_service import create_approval, decide_approval, list_approvals, require_decider
from audit_ledger import AuditCategory, ledger
from automation_jobs import enqueue_job, register_handler
from command_providers import (
    ProviderConfigurationError,
    ProviderRejectedError,
    abort_twilio_call,
    create_google_calendar_event,
    place_custom_http_call,
    place_twilio_call,
    send_smtp_email,
    send_twilio_sms,
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
from billing import require_active_subscription

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
    SMS = "SMS"
    CALL = "CALL"
    CALENDAR = "CALENDAR"


class ParsedIntent(str, Enum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    CALL = "CALL"
    CALENDAR = "CALENDAR"
    CONTRACT = "CONTRACT"
    MAO_CALC = "MAO_CALC"


_RISK = {
    CommandType.EMAIL: ActionRisk.OUTREACH,
    CommandType.SMS: ActionRisk.OUTREACH,
    CommandType.CALL: ActionRisk.LIVE_CALL,
    CommandType.CALENDAR: ActionRisk.CALENDAR_WRITE,
}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


async def _run_sync_in_executor(
    fn: Any,
    *args: Any,
    timeout_seconds: Optional[float] = 5.0,
) -> Any:
    if timeout_seconds is None:
        return await asyncio.to_thread(fn, *args)
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout_seconds)


async def _require_webhook_secret(request: Request, env_name: str) -> str:
    """Header, body signature or signed callback URL; the old `?token=` static
    secret only for one release, with a deprecation log (HOOK-3, webhook_auth)."""
    import webhook_auth

    return await webhook_auth.verify_webhook(request, env_name)


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
            ParsedIntent.SMS,
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


_COMMAND_PROVIDERS = {"google", "smtp", "twilio"}
# Providers an agent may self-manage, scoped to their own account_label.
# store/disable here and validate in sales_api.py all share this list so a
# new self-service provider is added once, not independently at each site.
_AGENT_SELF_SERVICE_PROVIDERS = {"google", "smtp"}


def _require_own_credential_or_broker(
    ctx: TenantContext, provider: str, account_label: str, *, action: str
) -> None:
    if ctx.role is Role.AGENT:
        if provider not in _AGENT_SELF_SERVICE_PROVIDERS or account_label != ctx.agent_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Agents may {action} only their own Google OAuth credential or "
                    "their own SMTP mail identity."
                ),
            )
    else:
        require_role(ctx, Role.BROKER_OWNER)


_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# Calendar only. Mail never leaves through a Google account, so requesting
# gmail.send would be an unused grant over an agent's whole mailbox.
_GOOGLE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
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


async def revoke_google_token(token: str) -> str:
    """Revoke a Google OAuth grant at Google (offboarding, erasure).

    Revoking the refresh token ends the whole grant, so a copy of an access
    token taken earlier stops refreshing too. Google answers 400 invalid_token
    for a grant that is already gone — that is success for an idempotent
    revoke. Returns "revoked" or "already_invalid".
    """
    import recovery_mode

    recovery_mode.guard("revoke a Google account connection")
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_GOOGLE_REVOKE_URL, data={"token": token}) as response:
                if 200 <= response.status < 300:
                    return "revoked"
                data = await response.json(content_type=None)
                if response.status == 400 and isinstance(data, dict) and data.get("error") == "invalid_token":
                    return "already_invalid"
                reason = str((data or {}).get("error") if isinstance(data, dict) else "revoke_failed")[:120]
                raise GoogleOAuthError(f"Google refused the revoke: {reason}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GoogleOAuthError("Google OAuth revoke service was unavailable.") from exc


def _oauth_return_url(return_path: str, outcome: str) -> str:
    import config as _config

    base = _config.public_base_url()
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


# Terminal email senders, keyed by ORACLE_EMAIL_PROVIDER. The value is the send
# function plus the provider_credentials key its per-tenant credential is stored
# under, so both move together when a new provider is added.
_EMAIL_SENDERS = {
    "smtp": (send_smtp_email, "smtp"),
}


def _resolve_email_provider():
    """Pick the configured terminal email sender.

    Defaults to SMTP: agent mail goes out through each agent's own Google OAuth
    grant (the Gmail rungs above this one), and SMTP is the platform-level
    sender that does not depend on any single agent having consented."""
    name = os.getenv("ORACLE_EMAIL_PROVIDER", "smtp").strip().lower()
    try:
        return _EMAIL_SENDERS[name]
    except KeyError:
        raise ProviderConfigurationError(
            f"ORACLE_EMAIL_PROVIDER={name!r} is not supported; use "
            + " or ".join(repr(k) for k in sorted(_EMAIL_SENDERS))
        ) from None


async def _load_provider_credential(
    ctx: TenantContext,
    provider: str,
    *,
    account_label: Optional[str] = None,
    exact: bool = False,
) -> Any:
    """Decrypt a tenant credential only inside the executing worker.

    Preference order: the caller's own labelled row, then 'default', then
    whichever active row is newest — so a broker who saved a credential under
    a custom label (the account_label field on PUT /providers/{provider} is
    free text) is still found even when the caller asked for a specific
    agent's row that doesn't exist. Pass `exact=True` (validate/disable: the
    caller named one specific row) to require that exact account_label match
    rather than silently falling back to someone else's credential.
    """
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT token_ciphertext,expires_at FROM provider_credentials
             WHERE tenant_id=$1::uuid AND provider=$2 AND disabled_at IS NULL
               AND ($4 IS FALSE OR account_label=$3)
               -- Fall back across shared labels (a broker's free-text one
               -- included), but never onto a row labelled with ANOTHER user's
               -- identity: that is a colleague's own mailbox or Google account
               -- (review OUT-3: agent B's approved email went out through agent
               -- A's personal SMTP login). Agents' rows are labelled agent_id.
               AND ($3::text IS NULL OR account_label = $3 OR NOT EXISTS (
                     SELECT 1 FROM users u
                      WHERE u.tenant_id = provider_credentials.tenant_id
                        AND lower(u.agent_id) = lower(provider_credentials.account_label)))
             ORDER BY CASE
                        WHEN $3::text IS NOT NULL AND account_label=$3 THEN 0
                        WHEN account_label='default' THEN 1
                        ELSE 2
                      END,
                      updated_at DESC
             LIMIT 1
            """,
            ctx.tenant_id,
            provider,
            account_label,
            exact,
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
    if command_type is CommandType.SMS:
        phone = str(target.get("phone") or "").strip()
        if not _E164_RE.match(phone):
            raise ValueError("SMS target requires an E.164 phone number")
        if not any(target.get(key) for key in ("contact_id", "lead_id", "client_id")):
            raise ValueError("SMS target must reference a contact, lead, or client")
        for key in ("contact_id", "lead_id", "client_id"):
            if target.get(key):
                try:
                    uuid.UUID(str(target[key]))
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ValueError(f"SMS target {key} must be a UUID") from exc
        state_code = str(target.get("state_code") or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", state_code):
            raise ValueError("SMS target requires a two-letter state_code")
        message = str(draft.get("body") or "").strip()
        if not message or len(message) > 1_600:
            raise ValueError("SMS draft body must contain 1-1600 characters")
        return
    if command_type is CommandType.CALL:
        phone = str(target.get("phone") or "").strip()
        if not _E164_RE.match(phone):
            raise ValueError("CALL target requires an E.164 phone number")
        if not any(target.get(key) for key in ("contact_id", "lead_id", "client_id")):
            raise ValueError("CALL target must reference a contact, lead, or client")
        for key in ("contact_id", "lead_id", "client_id"):
            if target.get(key):
                try:
                    uuid.UUID(str(target[key]))
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ValueError(f"CALL target {key} must be a UUID") from exc
        state_code = str(target.get("state_code") or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", state_code):
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


_COMMAND_NOT_FOUND = "Command not found."


async def _require_target_editable(conn: Any, target: dict[str, Any]) -> None:
    """A draft goes only to a record the caller may contact (0142, CONTRACT
    C3.4): 404 when they cannot see the client/contact it names, 403 when it is
    shared with them to view only. The database refuses the same row
    (command_executions_linked_insert); this answers it as an HTTP status
    instead of a 500. A value that is not a UUID links nothing (the 0142
    trigger ignores it) and is left to _validate_command_payload."""
    for key, require in (("client_id", record_access.require_client_edit),
                         ("contact_id", record_access.require_contact_edit)):
        value = target.get(key)
        if not value:
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        await require(conn, str(value))


async def _require_command_edit(ctx: TenantContext, command_id: str) -> None:
    """404 when the draft is invisible to the caller (its client or contact
    is), 403 when that record is shared with them to view only (0142)."""
    async with tenant_tx(ctx) as conn:
        await record_access.require_linked_edit(conn, "command_executions", command_id, _COMMAND_NOT_FOUND)


async def _requester_may_contact(tenant_id: str, command: Any) -> bool:
    """Re-check, at execution, that the person who asked for this send may
    still contact the record it is about (CONTRACT C3.4, 0142).

    Approval happens hours or days before a scheduled send; in between the
    client can be reassigned to a colleague or a share revoked. The worker runs
    as platform admin, which sees everything, so the check is made as the
    requester: the approval's requested_by (the agent the AI, a mission or a
    Smart Plan acted for), in their own role. A requester who is not a member
    (a system pipeline such as speed-to-lead) is checked as an agent with that
    id, which is exactly what it could stage: pool records only."""
    client_id, contact_id = command.get("client_id"), command.get("contact_id")
    if client_id is None and contact_id is None:
        return True
    platform = TenantContext(agent_id="command-worker", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    async with tenant_tx(platform) as conn:
        requested_by = None
        if command.get("approval_id") is not None:
            requested_by = await conn.fetchval(
                "SELECT requested_by FROM action_approvals WHERE id=$1::uuid", command["approval_id"]
            )
        requested_by = str(requested_by or command.get("created_by") or "")
        member = await record_access.find_member(conn, tenant_id, requested_by)
    role = Role.AGENT
    if member is not None:
        try:
            role = Role(member["role"])
        except ValueError:
            role = Role.AGENT
    actor = TenantContext(
        agent_id=str(member["agent_id"]) if member else requested_by,
        tenant_id=tenant_id,
        role=role,
    )
    async with tenant_tx(actor) as conn:
        return bool(await conn.fetchval(
            "SELECT app_linked_access($1::uuid, $2::uuid, true)", client_id, contact_id,
        ))


# "text" is an ordinary English noun as often as it is a verb — "email her the
# text of the counteroffer", "call him about their last text". Left in place
# those readings win the SMS pattern below (it is tested before CALL and EMAIL)
# and turn an email or a call into a billable outbound SMS under the wrong
# compliance gate. Determiner-led noun phrases are stripped before intent
# matching; "send the client a text" and "the text message" are not determiner-
# led references to existing content, so they still route to SMS.
# The modifier slot accepts any word rather than a fixed adjective list: real
# commands say "the inspection text", "the counteroffer text", "her closing
# text" — an enumerated whitelist can never cover the nouns a listing throws at
# it, and each miss silently re-routes an email into a billable SMS. A second
# determiner is excluded because it starts a NEW noun phrase: without that guard
# "send the client a text" reads as one span and stops routing to SMS.
_DETERMINER = r"(?:a|an|the|this|that|these|those|his|her|their|its|my|your|our)"
_TEXT_AS_NOUN_RE = re.compile(
    r"\b(?:the|this|that|these|those|his|her|their|its|my|your|our)"
    rf"(?:\s+(?!{_DETERMINER}\b)[a-z][a-z-]*){{0,2}}"
    r"\s+texts?\b(?!\s+messages?\b)"
    r"|\btexts?\s+of\b"
)

# The action verb an agent actually typed is the strongest possible signal, and
# it sits at the front: "email the seller the inspection text and my notes" is
# unambiguously an email no matter what nouns follow. Anchoring on it settles
# the SMS-before-EMAIL ordering below before the noun heuristics have to.
#
# Spanish (L05-04, owner 2026-10-14: English and Spanish at launch). Matched
# on `semantic_language.fold`ed text — lower case, accents removed — so
# "Envíale un correo" and "enviale un correo" are one command. Each Spanish
# phrase names its channel as explicitly as its English twin: "mándale un
# texto" is SMS, "llámala" is CALL, "escríbele un correo" is EMAIL. A bare
# "mensaje" names no channel (it can be a text, an email or a WhatsApp), so,
# like the English "message her", it is not a channel verb.
_ES_EMAIL_VERB = r"(?:envia|manda|escribe)(?:le|les)?\s+(?:un\s+|el\s+)?(?:correo|e-?mail|mail)"
_ES_SMS_VERB = (r"(?:envia|manda)(?:le|les)?\s+(?:un\s+|el\s+)?(?:sms|texto|mensaje\s+de\s+texto)"
                r"|textea(?:le|les)?")
_ES_CALL_VERB = r"llam(?:a|ar)(?:le|la|lo|les|los|las)?|telefonea(?:r)?(?:le|la|lo)?"
_LEADING_VERB_INTENT = (
    (rf"e-?mail|{_ES_EMAIL_VERB}", "EMAIL"),
    (rf"sms|text|{_ES_SMS_VERB}", "SMS"),
    (rf"call|phone|dial|{_ES_CALL_VERB}", "CALL"),
)
_LEADING_VERB_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|pls\s+|por\s+favor\s+|puedes\s+|podrias\s+)*"
    rf"(?P<verb>e-?mail|sms|text|call|phone|dial|{_ES_EMAIL_VERB}|{_ES_SMS_VERB}|{_ES_CALL_VERB})\b"
)
# "el texto de la contraoferta" is the Spanish noun, as "the text of" is.
_TEXTO_AS_NOUN_RE = re.compile(
    r"\b(?:el|los|este|estos|ese|esos|aquel|su|sus|mi|mis|tu|tus|nuestro|nuestros)\s+textos?\b"
    r"|\btextos?\s+del?\b"
)


def _parse_intent(raw_text: str) -> ParsedIntent:
    # fold = lower case with accents removed: English is unchanged by it.
    normalized = " ".join(semantic_language.fold(raw_text).split())
    text = _TEXTO_AS_NOUN_RE.sub(" ", _TEXT_AS_NOUN_RE.sub(" ", normalized))

    # These outrank the comms channels and keep their existing precedence:
    # "call to schedule a meeting with Bob" is CALENDAR, not CALL.
    #
    # Matched against the UNSTRIPPED text on purpose. The noun-stripper exists
    # only to stop the noun "text" being read as the SMS verb, and none of these
    # keywords can ever be that noun — but the stripper swallows whatever sits
    # between a determiner and "text", so "send the assignment text to Maria"
    # became "send to maria" and 422'd a command that used to route to CONTRACT.
    for intent, pattern in (
        (ParsedIntent.MAO_CALC, r"\b(?:mao|max(?:imum)? allowable offer|offer ceiling"
                                r"|oferta maxima(?: permitida)?)\b"),
        # "de acuerdo" is "OK", not an agreement.
        (ParsedIntent.CONTRACT, r"\b(?:contract|agreement|assignment|contrato|cesion|(?<!de )acuerdo)\b"),
        (ParsedIntent.CALENDAR, r"\b(?:calendar|schedule|book|meeting|appointment"
                                r"|calendario|reunion|cita|agendar|agenda(?:le|la|lo)?\s+(?:una?|la|el)"
                                r"|(?:programa|reserva)(?:r)?\s+(?:una\s+)?(?:llamada|visita|reunion|cita))\b"),
    ):
        if re.search(pattern, normalized):
            return intent

    # Among the three comms channels, the verb the agent led with decides —
    # ordering alone cannot, because "text" is a noun as often as a verb and
    # SMS has to be tested first for "text her about our call tomorrow".
    leading = _LEADING_VERB_RE.match(normalized)
    if leading:
        verb = leading.group("verb")
        for pattern, name in _LEADING_VERB_INTENT:
            if re.fullmatch(pattern, verb):
                return ParsedIntent[name]

    channels = (
        # SMS stays before CALL: misrouting a text into the LIVE_CALL risk class
        # is the more expensive error of the two.
        (ParsedIntent.SMS, r"\b(?:sms|text message|text|mensaje de texto|texto)\b"),
        (ParsedIntent.CALL, r"\b(?:call|phone|dial|llamale|llamala|llamalo|llama|llamar|telefonea|telefonear)\b"),
        (ParsedIntent.EMAIL, r"\b(?:email|e-mail|mail|correo)\b"),
    )
    for intent, pattern in channels:
        if re.search(pattern, text):
            return intent
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Command must request EMAIL, SMS, CALL, CALENDAR, CONTRACT, or MAO_CALC.",
    )


def _name_hint(raw_text: str, intent: ParsedIntent) -> str:
    verb = {
        ParsedIntent.EMAIL: r"(?:email|e-mail|mail)",
        ParsedIntent.SMS: r"(?:sms|text(?:\s+message)?)",
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
    if match:
        return " ".join(match.group(1).split())
    return _name_hint_es(raw_text, intent)


_ES_NAME_VERB = {
    ParsedIntent.EMAIL: _ES_EMAIL_VERB + r"(?:\s+electronico)?",
    ParsedIntent.SMS: _ES_SMS_VERB,
    ParsedIntent.CALL: _ES_CALL_VERB,
    ParsedIntent.CALENDAR: r"(?:agenda|programa|organiza)(?:r)?\s+(?:una\s+)?(?:reunion|cita|llamada|visita)\s+con",
    ParsedIntent.CONTRACT: r"(?:contrato|acuerdo)(?:\s+de\s+[a-z]+)?\s+para",
    ParsedIntent.MAO_CALC: r"(?:oferta maxima(?:\s+permitida)?)(?:\s+(?:para|de))?",
}


def _name_hint_es(raw_text: str, intent: ParsedIntent) -> str:
    """The Spanish twin of `_name_hint`: "llama a José Pérez sobre la oferta"
    names José Pérez. Matched on folded text, quoted from the original (the
    fold keeps positions), so accents in the name survive."""
    original = semantic_language.nfc(raw_text)
    folded = semantic_language.fold(original)
    match = re.search(
        rf"\b(?:{_ES_NAME_VERB[intent]})\s+(?:a\s+|al\s+|para\s+)?"
        r"([^\W\d_](?:[^\W\d_]|[' -]){0,79}?)"
        r"(?=\s+(?:y|sobre|acerca|para|por|a|al|en|con|que|hoy|manana)\b|[,.;?!]|$)",
        folded,
    )
    if not match:
        return ""
    return " ".join(original[match.start(1):match.end(1)].split())


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


def _assignment_requested(raw_text: str) -> bool:
    """An assignment contract was asked for: "assign…" or the Spanish
    "cesión" (L05-05; "contrato de cesión" used to fall to seller_purchase)."""
    return "assign" in raw_text.lower() or "cesion" in semantic_language.fold(raw_text)


def _fallback_draft(
    intent: ParsedIntent,
    raw_text: str,
    *,
    profile: dict[str, Any],
    client: Optional[dict[str, Any]],
    property_context: Optional[dict[str, Any]],
) -> dict[str, Any]:
    # The draft is written in the language the agent wrote the command in
    # (L05-05): "Envíale un correo a Sarah" drafts Spanish. Only the product's
    # own words change; the client's name, the address, the agent's
    # signature and the request itself are used as stored.
    lang = semantic_language.reply_language(raw_text)

    def say(key: str, **values: Any) -> str:
        return semantic_language.t(key, lang, **values)

    name = str((client or {}).get("full_name") or "").strip()
    first_name = name.split()[0] if name else ""
    signature = str(profile.get("signature") or "").strip()
    price = _money_hint(raw_text)
    property_address = str((property_context or {}).get("address") or "").strip()
    subject_context = property_address or say("draft.plans")

    if intent is ParsedIntent.EMAIL:
        proposal = say("draft.email_proposal", amount=f"${Decimal(price):,.2f}") if price else ""
        greeting = say("draft.greeting", first_name=first_name) if first_name else say("draft.greeting_bare")
        body = f"{greeting}\n\n{say('draft.email_line', about=subject_context, proposal=proposal)}"
        if signature:
            body = f"{body}\n\n{signature}"
        return {"subject": say("draft.email_subject", about=subject_context), "body": body}
    if intent is ParsedIntent.SMS:
        body = (say("draft.sms", first_name=first_name, about=subject_context) if first_name
                else say("draft.sms_bare", about=subject_context))
        if signature:
            body = f"{body} {signature}"
        return {"body": body[:1_600]}
    if intent is ParsedIntent.CALL:
        agent = profile.get("name") or say("draft.call_agent")
        return {
            "opening": (say("draft.call_opening", first_name=first_name, agent=agent) if first_name
                        else say("draft.call_opening_bare", agent=agent)),
            "objective": raw_text,
            "property_address": property_address or None,
            "agent_phone": profile.get("phone_number") or None,
        }
    if intent is ParsedIntent.CALENDAR:
        return {
            "event": {
                "summary": (say("draft.event_summary", name=name) if name
                            else say("draft.event_summary_bare")),
                "start": None,
                "end": None,
                "description": raw_text,
            }
        }
    if intent is ParsedIntent.CONTRACT:
        assignment = _assignment_requested(raw_text)
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
    if intent in {ParsedIntent.SMS, ParsedIntent.CALL}:
        if not target["phone"]:
            missing_fields.append("client.phone")
        if intent is ParsedIntent.CALL:
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
        assignment_requested = _assignment_requested(body.raw_text)
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

    if intent in {
        ParsedIntent.EMAIL,
        ParsedIntent.SMS,
        ParsedIntent.CALL,
        ParsedIntent.CALENDAR,
    } and not missing_fields:
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


@router.post("/execute", dependencies=[Depends(require_active_subscription)])
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


#: The explicit command verbs /classify accepts, English and Spanish (L05-04).
#: Matched at the START of the folded text (lower case, no accents).
_EXPLICIT_VERBS: tuple[tuple[CommandType, str], ...] = (
    (CommandType.EMAIL, r"^(?:email|e-mail|send (?:an )?email"
                        r"|(?:envia|manda|escribe)(?:le|les)? (?:un |el )?(?:correo|e-?mail|mail)"
                        r"|correo(?: electronico)?)\b"),
    (CommandType.SMS, r"^(?:sms|text|send (?:a )?text"
                      r"|(?:envia|manda)(?:le|les)? (?:un |el )?(?:sms|texto|mensaje de texto)"
                      r"|textea(?:le|les)?)\b"),
    (CommandType.CALL, r"^(?:call|phone|dial|llam(?:a|ar)(?:le|la|lo|les|los|las)?"
                       r"|telefonea(?:r)?(?:le|la|lo)?)\b"),
    (CommandType.CALENDAR, r"^(?:calendar|schedule|book|set up (?:a )?meeting"
                           r"|calendario|agendar|agenda(?:le|la|lo)? (?:una?|la|el)"
                           r"|(?:programa|organiza|reserva)(?:r)? (?:una )?(?:reunion|cita|llamada|visita))\b"),
)


def _classify_explicit(raw: str) -> CommandType:
    """The channel a command names with its leading verb, or 422."""
    text = " ".join(semantic_language.fold(raw.strip()).split())
    for command_type, pattern in _EXPLICIT_VERBS:
        if re.match(pattern, text):
            return command_type
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Command must explicitly request EMAIL, SMS, CALL, or CALENDAR.",
    )


@router.post("/classify")
async def classify_command(
    body: ClassificationRequest,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    command_type = _classify_explicit(body.text)
    return {
        "command_type": command_type.value,
        "risk_class": _RISK[command_type].value,
        "execution": "editable_approval_draft",
        "classification_basis": "explicit_command_verb",
    }


async def stage_command(
    ctx: TenantContext,
    *,
    command_type: CommandType,
    target: dict[str, Any],
    draft: dict[str, Any],
    context: dict[str, Any],
    idempotency_key: str,
    created_by: str,
    scheduled_at: Optional[datetime] = None,
    approval_expires_minutes: int = 1_440,
) -> dict[str, Any]:
    """Put an outreach command in `awaiting_approval` and return it.

    Extracted from ``create_command`` so the AI tool surface stages an outreach
    request through the same path a human does: one row in command_executions,
    one approval, and execution only via ``approve_command`` — which enqueues
    ``command:execute``, where ``guard_outreach`` runs before any provider call.

    The AI tools previously called ``create_approval`` directly. That produced a
    pending approval with no command behind it, so approving it did nothing at
    all: ``decide_approval`` only records the decision, and every executing
    caller in this codebase enqueues the work itself afterwards. The receipt
    still said "an admin must approve before the call is placed".
    """
    require_feature(Feature.AUTOMATION)
    # A card number is removed BEFORE the draft is hashed into its approval
    # (C4.2: the approved content is what is sent), so the text a person
    # approves, the stored draft and the message sent all lack it (SAQ A).
    draft = pan_redaction.scrub_obj(draft, "outreach_draft", tenant_id=ctx.tenant_id)
    _validate_command_payload(command_type, target, draft)
    if command_type is CommandType.EMAIL:
        # No postal address, no client email (CAN-SPAM; owner 2026-10-14):
        # refuse now rather than approve a draft the worker must hold.
        import commercial_email

        try:
            await commercial_email.require_postal_address(ctx.tenant_id)
        except commercial_email.PostalAddressRequired as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    enforce_public_property_data(context)
    async with tenant_tx(ctx) as conn:
        existing = await conn.fetchrow(
            """
            SELECT * FROM command_executions
            WHERE tenant_id=$1::uuid AND idempotency_key=$2
            """,
            ctx.tenant_id,
            idempotency_key,
        )
        if not existing:
            await _require_target_editable(conn, target)
    if existing:
        return {"command": _command_dict(existing), "created": False}

    command_id = str(uuid.uuid4())
    execution = _execution_payload(
        command_id=command_id,
        command_type=command_type,
        target=target,
        draft=draft,
        context=context,
        tenant_id=ctx.tenant_id,
    )
    approval = await create_approval(
        ctx,
        action_type=f"command:{command_type.value}",
        risk=_RISK[command_type],
        target_type="command",
        target_id=command_id,
        draft_payload=execution,
        expires_in_minutes=approval_expires_minutes,
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
            command_type.value,
            f"explicit_{command_type.value.lower()}",
            _RISK[command_type].value,
            json.dumps(target),
            json.dumps({"content": draft, "context": context}),
            str(approval["id"]),
            idempotency_key,
            scheduled_at,
            created_by,
        )
    return {"command": _command_dict(row), "approval": approval, "created": True}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_command(
    body: CommandCreate,
    ctx: TenantContext = Depends(require_context),
):
    """The human path into the same staging function the AI tools use."""
    return await stage_command(
        ctx,
        command_type=body.command_type,
        target=body.target,
        draft=body.draft,
        context=body.context,
        idempotency_key=body.idempotency_key,
        created_by=ctx.agent_id,
        scheduled_at=body.scheduled_at,
        approval_expires_minutes=body.approval_expires_minutes,
    )


@router.get("")
async def commands(
    state_filter: Optional[str] = Query(default=None, alias="state"),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    # RLS decides which drafts the caller sees (0142: those about records they
    # can see). Ordered by tenant_id first so idx_command_executions_tenant_created
    # serves the order under the 0141 tenant-wall range and the LIMIT stops the
    # scan; for one brokerage it is exactly newest first. Ordered by created_at
    # alone, every visible row was sorted, and the 0142 policies' estimated cost
    # crossed jit_above_cost: 1.7-2.4 s for an agent in a 21,000-draft brokerage
    # (local dev DB), 22-54 ms this way. JIT off as in list_approvals: a
    # selective state filter makes the planner expect a long scan, and the
    # policies are costed per row.
    async with tenant_tx(ctx) as conn:
        await conn.execute("SET LOCAL jit = off")
        if state_filter:
            rows = await conn.fetch(
                """
                SELECT * FROM command_executions
                WHERE state=$1 ORDER BY tenant_id, created_at DESC LIMIT $2
                """,
                state_filter,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM command_executions ORDER BY tenant_id, created_at DESC LIMIT $1",
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
                 WHERE provider=ANY($2::text[]) AND account_label=$1
                 ORDER BY provider,updated_at DESC
                """,
                ctx.agent_id,
                # Everything an agent may manage themselves, so a saved SMTP
                # identity shows as connected (it used to list Google only).
                sorted(_AGENT_SELF_SERVICE_PROVIDERS),
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


_OAUTH_BINDING_COOKIE = "neoh_oauth_binding"
_OAUTH_BINDING_PATH = "/api/commands/providers/google/oauth"


def _oauth_browser_binding(state_hash: str) -> str:
    """HMAC of the state under the app secret: proof that the browser finishing
    the flow is the one that started it (review AUTH-9)."""
    from auth import SECRET_KEY

    return hmac.new(SECRET_KEY.encode(), f"google-oauth:{state_hash}".encode(), hashlib.sha256).hexdigest()


@router.post("/providers/google/oauth/start")
async def start_google_oauth(
    body: GoogleOAuthStart,
    response: Response,
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
    # Bind the flow to this browser. The state alone was the whole credential,
    # so a user in one brokerage could start a flow and send the genuine Google
    # consent URL to someone else; their consent stored THEIR Google tokens in
    # the starter's tenant. SameSite=Lax still rides Google's top-level redirect.
    import config as _config

    response.set_cookie(
        _OAUTH_BINDING_COOKIE,
        _oauth_browser_binding(state_hash),
        max_age=int(_OAUTH_STATE_TTL.total_seconds()),
        httponly=True,
        secure=not _config.IS_DEV,
        samesite="lax",
        path=_OAUTH_BINDING_PATH,
    )
    return {
        "provider": "google",
        "authorization_url": f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}",
        "expires_at": expires_at.isoformat(),
    }


@router.get("/providers/google/oauth/callback", include_in_schema=False)
async def finish_google_oauth(
    request: Request,
    state_token: str = Query(alias="state", min_length=20, max_length=200),
    code: Optional[str] = Query(default=None, min_length=1, max_length=4_000),
    provider_error: Optional[str] = Query(default=None, alias="error", max_length=200),
):
    """Consume a Google callback without a browser JWT. The random state proves
    the flow; the binding cookie proves this browser started it."""
    state_hash = hashlib.sha256(state_token.encode("utf-8")).hexdigest()
    binding = request.cookies.get(_OAUTH_BINDING_COOKIE, "")
    if not binding or not hmac.compare_digest(binding, _oauth_browser_binding(state_hash)):
        raise HTTPException(
            status_code=400,
            detail="Finish connecting Google in the same browser you started from.",
        )
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
                refresh_ciphertext,scopes,expires_at,last_validated_at,created_by,
                validation_status,validation_error,validated_capabilities
            ) VALUES ($1::uuid,'google',$2,$3,$4,$5::text[],$6,now(),$7,
                      'valid',NULL,'{"email":true,"calendar":true}'::jsonb)
            ON CONFLICT (tenant_id,provider,account_label) DO UPDATE SET
                token_ciphertext=EXCLUDED.token_ciphertext,
                refresh_ciphertext=COALESCE(
                    EXCLUDED.refresh_ciphertext,provider_credentials.refresh_ciphertext
                ),
                scopes=EXCLUDED.scopes,expires_at=EXCLUDED.expires_at,
                last_validated_at=now(),validation_status='valid',validation_error=NULL,
                validated_capabilities=EXCLUDED.validated_capabilities,
                disabled_at=NULL,updated_at=now()
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
    _require_own_credential_or_broker(ctx, provider, body.account_label, action="manage")
    key = _provider_key(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        encrypted = await encrypt_pii(conn, body.token, key)
        refresh = await encrypt_pii(conn, body.refresh_token, key) if body.refresh_token else None
        row = await conn.fetchrow(
            """
            INSERT INTO provider_credentials (
                tenant_id,provider,account_label,token_ciphertext,
                refresh_ciphertext,scopes,expires_at,last_validated_at,created_by,
                validation_status,validation_error,validated_capabilities
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6::text[],$7,NULL,$8,
                      'unverified',NULL,'{}'::jsonb)
            ON CONFLICT (tenant_id,provider,account_label) DO UPDATE SET
                token_ciphertext=EXCLUDED.token_ciphertext,
                refresh_ciphertext=EXCLUDED.refresh_ciphertext,
                scopes=EXCLUDED.scopes,expires_at=EXCLUDED.expires_at,
                last_validated_at=NULL,validation_status='unverified',
                validation_error=NULL,validated_capabilities='{}'::jsonb,
                disabled_at=NULL,updated_at=now()
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
        # A freshly stored credential is deliberately unverified, so this column
        # is NULL until the first validation run — never assume a datetime here.
        "last_validated_at": (
            row["last_validated_at"].isoformat() if row["last_validated_at"] else None
        ),
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
    _require_own_credential_or_broker(ctx, provider, account_label, action="disable")
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
    # The edited draft is what gets approved and sent: no card number in it.
    body.draft = pan_redaction.scrub_obj(body.draft, "outreach_draft",
                                         tenant_id=ctx.tenant_id, target_id=command_id)
    async with tenant_tx(ctx) as conn:
        # Before the FOR UPDATE: a row the caller may only view is filtered
        # out of a locking read, which would answer 404 instead of 403.
        await record_access.require_linked_edit(conn, "command_executions", command_id, _COMMAND_NOT_FOUND)
        row = await conn.fetchrow(
            "SELECT * FROM command_executions WHERE id=$1::uuid FOR UPDATE", command_id
        )
        if not row:
            raise HTTPException(status_code=404, detail=_COMMAND_NOT_FOUND)
        if row["state"] not in {"draft", "awaiting_approval"}:
            raise HTTPException(status_code=409, detail="Command can no longer be edited.")
        # An edit replaces the approval with one requested by the editor, so
        # only someone who may decide the current one may edit (0142): before,
        # any agent who could see a colleague's draft could edit it into their
        # own request and approve it.
        current = await conn.fetchrow(
            "SELECT requested_by, risk_class, action_type FROM action_approvals WHERE id=$1::uuid",
            row["approval_id"],
        ) if row["approval_id"] is not None else None
        if current is not None:
            require_decider(ctx, current)
        elif not (ctx.is_platform_admin or ctx.is_broker_owner):
            raise HTTPException(status_code=403, detail="Only a broker owner can change this command.")
        command_type = CommandType(row["command_type"])
        try:
            _validate_command_payload(command_type, body.target, body.draft)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await _require_target_editable(conn, body.target)
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


async def release_command(
    ctx: TenantContext, row: Any, *, reason: str = "",
) -> dict[str, Any]:
    """Approve a staged command and put it on the queue.

    Extracted from approve_command's body, unchanged, so that a mission
    releasing its own approval and a person clicking Approve take the SAME
    path: one decision record, one job, one state transition. The alternative
    — a second release path for missions — is how two systems end up
    disagreeing about what was authorised, and how a send escapes the audit
    trail the first path writes.

    `row` is a command_executions row already checked to be awaiting approval.
    """
    command_id = str(row["id"])
    approval = await decide_approval(
        ctx, str(row["approval_id"]), decision="approved", reason=reason
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
        import outreach_lifecycle

        await outreach_lifecycle.advance_first_response(
            conn, tenant_id=ctx.tenant_id, command_id=command_id,
            stage=outreach_lifecycle.Stage.APPROVED,
        )
    return {"command": _command_dict(updated), "approval": approval, "job": job}


@router.post("/{command_id}/approve", dependencies=[Depends(require_active_subscription)])
async def approve_command(
    command_id: str,
    body: ApprovalDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.AUTOMATION)
    await _require_command_edit(ctx, command_id)
    row = await _get_command(ctx, command_id)
    if not row:
        raise HTTPException(status_code=404, detail=_COMMAND_NOT_FOUND)
    if row["state"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"Command is {row['state']}.")
    return await release_command(ctx, row, reason=body.reason)


@router.post("/{command_id}/reject")
async def reject_command(
    command_id: str,
    body: ApprovalDecision,
    ctx: TenantContext = Depends(require_context),
):
    await _require_command_edit(ctx, command_id)
    row = await _get_command(ctx, command_id)
    if not row:
        raise HTTPException(status_code=404, detail=_COMMAND_NOT_FOUND)
    approval = await decide_approval(
        ctx, str(row["approval_id"]), decision="rejected", reason=body.reason
    )
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchrow(
            "UPDATE command_executions SET state='cancelled' WHERE id=$1::uuid RETURNING *",
            command_id,
        )
        import outreach_lifecycle

        # A rejected draft never reached anyone. Without this the first-response
        # ledger kept it 'staged' forever.
        await outreach_lifecycle.advance_first_response(
            conn, tenant_id=ctx.tenant_id, command_id=command_id,
            stage=outreach_lifecycle.Stage.CANCELLED,
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
        # 0140: 404 for a call on a record the caller cannot see, 403 when it
        # is shared view-only (FOR SHARE alone would read both as missing).
        await record_access.require_linked_edit(
            conn, "live_call_sessions", call_session_id, "Call session not found.")
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
                tenant_id, call_session_id, event_type, transcript_excerpt, transcript_ciphertext,
                counter_offer, arv, rehab, mao, threshold, payload,
                model_version, created_by
            ) VALUES (
                $1::uuid,$2::uuid,'counter_offer',NULL,
                pgp_sym_encrypt(NULLIF($3::text,''),$11::text,'s2k-mode=1'),$4,$5,$6,$7,$8,$9::jsonb,
                'objective-negotiation-2026.07',$10
            ) RETURNING id, created_at
            """,
            ctx.tenant_id,
            call_session_id,
            # Never a card number a caller read out (PCI DSS SAQ A).
            pan_redaction.scrub(body.transcript_excerpt, "call_transcript_excerpt",
                                tenant_id=ctx.tenant_id, target_id=call_session_id),
            guidance["counter_offer"],
            body.arv,
            body.rehab,
            guidance["mao"],
            guidance["threshold"],
            json.dumps(guidance),
            ctx.agent_id,
            # Sealed at rest (0152, A01): what the caller said.
            sealed_text.key_for(ctx.tenant_id),
        )
    frame = {
        "type": "NEGOTIATION_TELEMETRY",
        "version": 1,
        "call_session_id": call_session_id,
        "event_id": event["id"],
        **guidance,
    }
    # The counter offer, ARV and MAO belong to the call's client/contact: only
    # members who may read that record receive them (C3.6, 0145).
    await ws_hub.broadcast_record(ctx.tenant_id, frame, client_id=call["client_id"],
                                  contact_id=call["contact_id"], actor=ctx.agent_id)
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
        await record_access.require_linked_edit(
            conn, "live_call_sessions", call_session_id, "Call session not found.")
        call = await conn.fetchrow(
            """
            UPDATE live_call_sessions
               SET consent_recorded=$2,consent_basis=$3,
                   transcript_status=CASE WHEN $2 THEN 'active' ELSE 'complete' END,
                   ended_at=CASE WHEN $2 THEN ended_at ELSE COALESCE(ended_at,now()) END
             WHERE id=$1::uuid
            RETURNING id, client_id, contact_id
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
    await ws_hub.broadcast_record(ctx.tenant_id, frame, client_id=call["client_id"],
                                  contact_id=call["contact_id"], actor=ctx.agent_id)
    return frame


@router.post("/webhooks/acs", include_in_schema=False)
async def acs_webhook(request: Request):
    await _require_webhook_secret(request, "ORACLE_ACS_WEBHOOK_SECRET")
    body = await request.json()
    events = body if isinstance(body, list) else [body]
    for event in events:
        event_type = event.get("eventType") or event.get("type", "")
        data = event.get("data", {})
        logger.info("acs webhook: event_type=%s id=%s", event_type, event.get("id", "")[:20])

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
            logger.warning("acs webhook: CreateCallFailed cid=%s reason=%s", cid, reason)
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
    is_authentic = await _run_sync_in_executor(
        verify_acs_websocket_jwt,
        authorization,
        timeout_seconds=5.0,
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
        # Never the caller's number: it is personal data (CONTRACT C5.4, audit A06).
        logging.getLogger("oracle.acs_webhook").error(
            "ACS configuration error — incoming call rejected: %s", exc
        )
    except Exception:
        import logging
        logging.getLogger("oracle.acs_webhook").exception("Incoming call handler failed")


def _update_call_session(call_connection_id: str, status: str) -> None:
    import asyncio as _asyncio
    _asyncio.ensure_future(_update_call_session_async(call_connection_id, status))


async def _update_call_session_async(call_connection_id: str, status: str,
                                     outcome: Optional[str] = None) -> None:
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
                   -- The first final callback wins; duplicates do not move it.
                   ended_at=CASE WHEN $2 IN ('completed','failed')
                                  THEN COALESCE(ended_at, now()) ELSE ended_at END,
                   outcome=COALESCE(outcome, $3)
             WHERE provider_call_id=$1
            """,
            call_connection_id,
            status,
            outcome,
        )


#: Provider call statuses, in the words a client timeline should use. Only a
#: status the carrier itself reported ever reaches the timeline; "answered" is
#: written when the carrier says the call was picked up, never before.
_CALL_OUTCOME_SUMMARY: dict[str, str] = {
    "completed": "Call answered and ended",
    "busy": "Call not connected — line busy",
    "no-answer": "Call not answered",
    "timeout": "Call not answered",
    "failed": "Call failed at the carrier",
    "canceled": "Call canceled before it connected",
    "cancel": "Call canceled before it connected",
}


async def _record_call_answered(provider_call_id: str) -> None:
    import outreach_lifecycle

    platform_ctx = TenantContext(
        agent_id="call-status-webhook",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    try:
        async with tenant_tx(platform_ctx) as conn:
            await outreach_lifecycle.record_call_answered(conn, provider_call_id=provider_call_id)
    except Exception:  # noqa: BLE001 — the carrier status is recorded regardless
        logger.warning("call answer not applied to its first response: %s", provider_call_id,
                       exc_info=True)


async def record_call_outcome(provider_call_id: str, call_status: str,
                              duration_seconds: Optional[int] = None) -> None:
    """Carrier-confirmed call status → live_call_sessions + the client's timeline.

    Plivo's outbound status callback only logged and cleaned up, so an approved
    Neoh call left "Call placed" as the last thing anyone could see — whether
    it was answered was known to the carrier and to nobody here.
    """
    status_value = (call_status or "").strip().lower()
    if not provider_call_id or not status_value:
        return
    import outreach_lifecycle

    if (
        outreach_lifecycle.call_status_stage(status_value, duration_seconds)
        is outreach_lifecycle.Stage.DELIVERED
    ):
        # An answer is the call's delivery (outreach_lifecycle). It is the
        # only point a speed-to-lead call meters lead_engaged; idempotent, so
        # 'answered' followed by 'completed' still counts once.
        await _record_call_answered(provider_call_id)
    if status_value in {"in-progress", "answered"}:
        await _update_call_session_async(provider_call_id, "in-progress")
        return
    summary = _CALL_OUTCOME_SUMMARY.get(status_value)
    if summary is None:
        return
    await _update_call_session_async(provider_call_id, "completed", outcome=status_value)
    if status_value == "completed" and duration_seconds is not None and duration_seconds <= 0:
        # Plivo reports a never-answered call as completed with zero billed time.
        summary = "Call not answered"
    elif status_value == "completed" and duration_seconds:
        minutes, seconds = divmod(int(duration_seconds), 60)
        summary = f"{summary} ({minutes}:{seconds:02d})"
    platform_ctx = TenantContext(
        agent_id="call-status-webhook",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    try:
        async with tenant_tx(platform_ctx) as conn:
            await conn.execute(
                """
                INSERT INTO client_activities (tenant_id, client_id, kind, summary, meta, actor)
                SELECT s.tenant_id, s.client_id, 'message', $2,
                       jsonb_build_object('direction', 'outbound', 'channel', 'call',
                                          'command_id', s.command_id::text,
                                          'provider_call_id', $1::text,
                                          'carrier_status', $3::text,
                                          'duration_seconds', $4::int),
                       'carrier'
                  FROM live_call_sessions s
                 WHERE s.provider_call_id = $1 AND s.client_id IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM client_activities a
                        WHERE a.client_id = s.client_id
                          AND a.meta->>'provider_call_id' = $1
                          AND a.meta ? 'carrier_status')
                """,
                provider_call_id, summary, status_value, duration_seconds,
            )
    except Exception:  # noqa: BLE001 — the status is recorded above; the timeline row is extra
        logger.warning("call outcome not added to the timeline: %s", provider_call_id, exc_info=True)


#: Who wrote a note the AI took on a call — never the agent's own id: the agent
#: did not say these words, the callee did, and the AI summarised them.
AI_CALL_NOTE_AUTHOR = "neoh-ai-call"
AI_CALL_NOTE_CONSENT_BASIS = "verbal_consent_on_ai_call"
_MAX_CALL_NOTES = 25
_MAX_CALL_NOTE_CHARS = 300


def _call_platform_ctx(agent_id: str) -> TenantContext:
    return TenantContext(
        agent_id=agent_id,
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )


async def record_call_note_consent(provider_call_id: str, granted: bool) -> bool:
    """The callee's answer to "may I take a few notes for your agent?".

    Outbound calls keep nothing by default (consent_recorded=false,
    'awaiting_explicit_live_transcription_consent'). A yes said to the AI on
    the call, after the recorded-line disclosure, is that explicit consent;
    a no is recorded too, so nobody later mistakes silence for agreement.
    Returns whether the call's session row was found — no row, no notes.
    """
    if not provider_call_id:
        return False
    async with tenant_tx(_call_platform_ctx("ai-call-consent")) as conn:
        row = await conn.fetchrow(
            """
            UPDATE live_call_sessions
               SET consent_recorded=$2,
                   consent_basis=CASE WHEN $2 THEN $3 ELSE 'declined_on_ai_call' END,
                   transcript_status=CASE WHEN $2 THEN 'active' ELSE 'complete' END
             WHERE provider_call_id=$1
            RETURNING id
            """,
            provider_call_id, bool(granted), AI_CALL_NOTE_CONSENT_BASIS,
        )
    return row is not None


def format_call_notes(notes: list[str]) -> str:
    lines = "\n".join(f"- {n}" for n in notes)
    return f"Notes from Neoh's call (the caller agreed to notes):\n{lines}"


async def deliver_call_notes(provider_call_id: str, notes: list[str],
                             conversation_id: str = "") -> Optional[str]:
    """Put the notes the AI took on a call where the agent works: a note on
    the client plus a timeline entry, and a live CALL_NOTES frame.

    Only for a call whose callee said yes (consent_recorded); idempotent per
    call. Returns the note id, or None when nothing was written.

    The note lands at once in the mode the calling agent chose
    (call_note_settings, owner decision D-2026-10-12-03): 'review' saves it
    *proposed* for the agent to approve, edit or discard; 'direct' saves it
    approved. The live frame carries customer content, so it goes only to the
    agent who requested the call and the client's assigned agent, and only if
    they may still read the client (CONTRACT C3.6, ws_hub.broadcast_record).
    It used to go to every session in the brokerage.
    """
    import call_note_settings
    import client_note_crypto

    clean = [str(n).strip()[:_MAX_CALL_NOTE_CHARS] for n in notes if str(n).strip()][:_MAX_CALL_NOTES]
    if not provider_call_id or not clean:
        return None
    # A card number the callee read out never becomes a note, a timeline
    # entry or a CALL_NOTES frame (PCI DSS SAQ A). seal_body checks again.
    redactions = [pan_redaction.redact(n) for n in clean]
    clean = [r.text for r in redactions]
    body = format_call_notes(clean)
    async with tenant_tx(_call_platform_ctx(AI_CALL_NOTE_AUTHOR)) as conn:
        session = await conn.fetchrow(
            """SELECT s.id, s.tenant_id, s.client_id, s.contact_id, s.lead_id, s.command_id, s.consent_recorded,
                      s.created_by, c.created_by AS command_created_by
                 FROM live_call_sessions s
                 LEFT JOIN command_executions c ON c.id = s.command_id
                WHERE s.provider_call_id=$1 FOR UPDATE OF s""",
            provider_call_id,
        )
        pan_redaction.report(
            "ai_call_note", sum(r.cards for r in redactions), sum(r.codes for r in redactions),
            tenant_id=session["tenant_id"] if session else None,
            target_id=session["client_id"] if session else None)
        if session is None or not session["consent_recorded"]:
            logger.warning("call notes dropped — no consented session: %s", provider_call_id)
            return None
        await conn.execute(
            "UPDATE live_call_sessions SET transcript_status='complete' WHERE id=$1", session["id"])
        client_id = session["client_id"]
        requester = str(session["command_created_by"] or session["created_by"] or "").strip()
        if client_id is None and session["lead_id"] is not None:
            # A lead-only call (chk_call_anchor allows it; leads have no notes
            # table). A lead that is a client's own house follows that client
            # (0143), so its notes do too (L09-06, T09.17).
            client_id = await conn.fetchval(
                "SELECT seller_client_id FROM leads WHERE id=$1", session["lead_id"])
        if client_id is None:
            # A prospect with no client record: there is nowhere the agent can
            # review a note, so nothing the callee said is kept — and the agent
            # is told so, once, instead of the notes vanishing into a log line.
            await _tell_requester_notes_not_kept(conn, session, requester, provider_call_id, len(clean))
            return None
        if await conn.fetchval(
            """SELECT 1 FROM client_activities
                WHERE client_id=$1 AND meta->>'provider_call_id'=$2 AND meta->>'source'='ai_call_notes'""",
            client_id, provider_call_id,
        ):
            return None
        assignee = await conn.fetchval(
            "SELECT assignee_id FROM clients WHERE id=$1", client_id)
        mode = await call_note_settings.read_mode(conn, session["tenant_id"], requester)
        review_state = "proposed" if mode == "review" else "approved"
        # What the customer said is sealed at rest under the brokerage's key,
        # never a plaintext body (0132, audit A01; the database refuses one).
        sealed = await client_note_crypto.seal_body(conn, session["tenant_id"], body,
                                                    surface="ai_call_note")
        note = await conn.fetchrow(
            """INSERT INTO client_notes (tenant_id, client_id, body, body_ciphertext, author_id,
                                         pinned, review_state)
               VALUES ($1, $2, '', $3, $4, false, $5) RETURNING id""",
            session["tenant_id"], client_id, sealed, AI_CALL_NOTE_AUTHOR, review_state,
        )
        summary = f"Call notes from Neoh ({len(clean)})"
        if review_state == "proposed":
            summary += " — waiting for your review"
        await conn.execute(
            """INSERT INTO client_activities (tenant_id, client_id, kind, summary, meta, actor)
               VALUES ($1, $2, 'note', $3, $4::jsonb, $5)""",
            session["tenant_id"], client_id, summary,
            json.dumps({
                "source": "ai_call_notes", "note_id": str(note["id"]),
                "provider_call_id": provider_call_id,
                "command_id": str(session["command_id"]) if session["command_id"] else None,
                "consent_basis": AI_CALL_NOTE_CONSENT_BASIS,
                "conversation_id": conversation_id or None,
                "review_state": review_state,
            }),
            AI_CALL_NOTE_AUTHOR,
        )
    frame = {
        "type": "CALL_NOTES", "version": 1,
        "client_id": str(client_id),
        "command_id": str(session["command_id"]) if session["command_id"] else None,
        "note_id": str(note["id"]), "notes": clean, "review_state": review_state,
    }
    # Among the requester and the assignee, only those who may still read the
    # client (a revoked share, a reassignment) — C3.4/C3.6, 0145.
    try:
        await ws_hub.broadcast_record(
            str(session["tenant_id"]), frame, client_id=client_id,
            contact_id=session["contact_id"], only=call_note_recipients(requester, assignee),
            mirror=False)  # private to its recipients, as broadcast_user was
    except Exception:  # noqa: BLE001 — the note is saved; the live frame is extra
        logger.warning("CALL_NOTES frame not sent: %s", provider_call_id, exc_info=True)
    return str(note["id"])


async def _tell_requester_notes_not_kept(conn: Any, session: Any, requester: str,
                                         provider_call_id: str, count: int) -> None:
    """A task for the agent who asked for the call: notes were taken but not
    kept, because the call was about a lead with no client record. Carries no
    note content. Idempotent per call."""
    marker = f"Call {provider_call_id}"
    if not requester or await conn.fetchval(
        """SELECT 1 FROM client_tasks
            WHERE tenant_id=$1 AND created_by=$2 AND details LIKE $3""",
        session["tenant_id"], AI_CALL_NOTE_AUTHOR, f"%{marker}%",
    ):
        return
    await conn.execute(
        """INSERT INTO client_tasks (tenant_id, client_id, title, details, status, priority,
                                     assignee_id, created_by)
           VALUES ($1, NULL, $2, $3, 'open', 'normal', $4, $5)""",
        session["tenant_id"],
        "Call notes were not kept: this lead has no client record",
        (f"Neoh took {count} note(s) with the callee's consent on a call about a lead "
         f"(lead {session['lead_id']}), but the lead is not linked to a client, so there "
         f"is nowhere to keep them for your review and nothing was saved. Add the person "
         f"as a client before the next call to keep its notes. {marker}."),
        requester, AI_CALL_NOTE_AUTHOR,
    )
    logger.info("call notes not kept — lead with no client; requester told: %s", provider_call_id)


def call_note_recipients(requester: Optional[str], assignee: Optional[str]) -> list[str]:
    """Who may receive a call's note text live: the agent who requested the
    call and the client's assigned agent, once each (agent ids compare
    case-insensitively), never the AI author or an empty id."""
    out: list[str] = []
    for agent in (requester, assignee):
        agent = str(agent or "").strip()
        if agent and agent != AI_CALL_NOTE_AUTHOR and agent.lower() not in {a.lower() for a in out}:
            out.append(agent)
    return out


#: What an approved send looks like in interaction_logs. CALENDAR is absent on
#: purpose: a calendar event is not a touch on a thread, it is an appointment,
#: and it is recorded as an outcome below rather than as an interaction.
_SEND_INTERACTION_TYPE: dict[str, str] = {
    "EMAIL": "email",
    "SMS": "sms",
    "CALL": "call_transcript",
}

#: Timeline wording per approved send — what the provider acknowledged, only.
_SEND_ACTIVITY_SUMMARY: dict[str, str] = {
    "EMAIL": "Email sent (approved in Neoh)",
    "SMS": "Text accepted by the carrier (approved in Neoh)",
    "CALL": "Call placed (approved in Neoh)",
}


def _event_start(draft: dict[str, Any]) -> Optional[datetime]:
    """The scheduled start of a calendar draft, or None if it cannot be read."""
    event = draft.get("event") if isinstance(draft, dict) else None
    start = event.get("start") if isinstance(event, dict) else None
    raw = start.get("dateTime") if isinstance(start, dict) else start
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _record_send_bookkeeping(
    ctx: TenantContext,
    *,
    command_id: str,
    command_type: CommandType,
    target: dict[str, Any],
    draft: dict[str, Any],
    provider_result: Any,
    actor: Optional[str] = None,
) -> None:
    """Make an approved send visible to the layers that reason about it.

    Three writes, each in its own SAVEPOINT, none able to fail the caller:

    1. An interaction_logs row. Until now an approved message that a provider
       accepted left NO row here — crm.send_message writes one for manual
       sends, but this worker never did. So a thread could hold a client's
       reply and no send for it to be a reply TO, and intent_states saw the
       brokerage's outreach as silence. chk_interaction_anchor needs a client
       or lead; a command that targets only a contact_id writes nothing and
       says so, because inventing an anchor would be worse than the gap.
    2. lead_response_events.disposition → 'accepted' (0133; 'sent' before),
       via outreach_lifecycle — and 'delivered' if a receipt already arrived.
    3. For a calendar command, an appointment_booked outcome — an actual
       future appointment with a provider reference behind it, which is what
       the vision means by "booked" and what `showings` (a past-tense record)
       cannot represent.
    """
    import outcome_memory
    import outreach_lifecycle

    client_id = target.get("client_id") or None
    lead_id = target.get("lead_id") or None
    reference = getattr(provider_result, "reference", None)
    interaction_type = _SEND_INTERACTION_TYPE.get(command_type.value)

    try:
        async with tenant_tx(ctx) as conn:
            if interaction_type and (client_id or lead_id):
                try:
                    async with conn.transaction():
                        # 0154 (A01): the draft subject is stored only sealed; the
                        # payload is metadata, split the same way as every writer's.
                        subject = draft.get("subject") if isinstance(draft, dict) else None
                        stored, sealed_content = sealed_text.split_json({
                            "command_id": command_id,
                            "provider": getattr(provider_result, "provider", None),
                        }, sealed_text.LOG_CONTENT)
                        await conn.execute(
                            """
                            INSERT INTO interaction_logs
                                (tenant_id, client_id, lead_id, actor_role, interaction_type,
                                 direction, subject, external_message_id, payload, thread_id,
                                 subject_ciphertext, content_ciphertext)
                            SELECT $1::uuid, $2::uuid, $3::uuid, 'agent', $4,
                                   'outbound', NULL, $6, $7::jsonb,
                                   COALESCE(
                                       (SELECT thread_id FROM interaction_logs
                                         WHERE ($2::uuid IS NOT NULL AND client_id = $2::uuid
                                                OR $2::uuid IS NULL AND lead_id = $3::uuid)
                                           AND interaction_type = $4
                                           AND thread_id IS NOT NULL
                                         ORDER BY created_at DESC LIMIT 1),
                                       gen_random_uuid()),
                                   pgp_sym_encrypt(NULLIF($5::text, ''), $8::text, 's2k-mode=1'),
                                   pgp_sym_encrypt($9::text, $8::text, 's2k-mode=1')
                            -- interaction_logs has no dedupe key. A retried
                            -- worker that reaches this block twice must not
                            -- write two sends for one provider reference.
                            WHERE $6::text IS NULL OR NOT EXISTS (
                                SELECT 1 FROM interaction_logs
                                 WHERE external_message_id = $6 AND direction = 'outbound')
                            """,
                            ctx.tenant_id, client_id, lead_id, interaction_type,
                            subject,
                            reference,
                            json.dumps(stored),
                            sealed_text.key_if_needed(ctx.tenant_id, subject, sealed_content),
                            sealed_content,
                        )
                except Exception:  # noqa: BLE001 — best-effort by contract
                    logger.warning("send interaction not recorded for %s", command_id, exc_info=True)
            elif interaction_type:
                logger.warning(
                    "command %s reached a contact with no client or lead anchor; "
                    "no interaction row written (chk_interaction_anchor)", command_id,
                )

            # Accepted, in outreach_lifecycle's words: the provider took it.
            # Not delivered — that needs a carrier receipt or an answer, and is
            # the only stage that meters lead_engaged. A receipt that beat
            # this write is picked up by reconcile_command_delivery. Both run
            # in their own SAVEPOINT; neither can fail the worker.
            await outreach_lifecycle.advance_first_response(
                conn, tenant_id=ctx.tenant_id, command_id=command_id,
                stage=outreach_lifecycle.Stage.ACCEPTED,
            )
            await outreach_lifecycle.reconcile_command_delivery(
                conn, tenant_id=ctx.tenant_id, command_id=command_id,
            )

            # 4. The client's timeline. ClientTimeline reads client_activities
            #    only; until now an approved call, an approved email and a
            #    Twilio text reached the provider and left the relationship
            #    timeline silent (only the Telnyx path wrote a row, inside
            #    record_outbound_message — skipped here so it is not doubled).
            #    The wording states what the provider acknowledged, nothing
            #    more: "accepted" is not "delivered", "placed" is not "answered".
            activity = _SEND_ACTIVITY_SUMMARY.get(command_type.value)
            provider_name = str(getattr(provider_result, "provider", "") or "")
            if (
                client_id and activity
                and not (command_type is CommandType.SMS and provider_name == "telnyx")
            ):
                try:
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO client_activities
                                (tenant_id, client_id, kind, summary, meta, actor)
                            SELECT $1::uuid, $2::uuid, 'message', $3, $4::jsonb, $5
                             WHERE NOT EXISTS (
                                 SELECT 1 FROM client_activities
                                  WHERE client_id = $2::uuid AND meta->>'command_id' = $6)
                            """,
                            ctx.tenant_id, client_id, activity,
                            json.dumps({
                                "direction": "outbound",
                                "channel": command_type.value.lower(),
                                "command_id": command_id,
                                "provider_reference": reference,
                                "approved": True,
                            }),
                            actor or ctx.agent_id,
                            command_id,
                        )
                except Exception:  # noqa: BLE001 — best-effort by contract
                    logger.warning("timeline activity not recorded for %s", command_id, exc_info=True)

            if command_type is CommandType.CALENDAR and (client_id or lead_id):
                await outcome_memory.record_outcome(
                    ctx,
                    outcome_kind="appointment_booked",
                    subject_type="client" if client_id else "lead",
                    subject_id=str(client_id or lead_id),
                    client_id=str(client_id) if client_id else None,
                    source_table="command_executions",
                    source_id=command_id,
                    occurred_at=_event_start(draft) or datetime.now(timezone.utc),
                    detail={"provider_reference": reference},
                    conn=conn,
                )
    except Exception:  # noqa: BLE001 — the send already happened; log and move on
        logger.warning("send bookkeeping skipped for %s", command_id, exc_info=True)


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
                        tenant_id,command_id,contact_id,client_id,lead_id,consent_recorded,
                        consent_basis,transcript_status,provider_call_id,created_by
                    ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,false,
                              'awaiting_explicit_live_transcription_consent',
                              'pending',$6,$7)
                    ON CONFLICT (command_id) WHERE command_id IS NOT NULL DO NOTHING
                    """,
                    tenant_id,
                    command_id,
                    target.get("contact_id"),
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

    # `ctx` is the job runner ("command-worker"), not a person. Everything
    # keyed per agent — the business number a text goes out from, the verified
    # caller ID a call shows — belongs to the agent who staged the command.
    # Looking those up as "command-worker" found nobody's route, so every
    # approved call failed "Connect and verify your business number" and every
    # Telnyx text "connect and finish setting up text messages", for agents
    # who had done exactly that (found by the killer-demo run, 2026-10-04).
    # The email branch already resolved its sender this way.
    owner_ctx = TenantContext(
        agent_id=str(command["created_by"] or "command-worker"),
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )

    await reporter.progress(15, "compliance check")
    submission_started = False
    try:
        if not await _requester_may_contact(tenant_id, command):
            raise RuntimeError(
                "Not sent: the person who asked for this can no longer contact this "
                "client or contact (reassigned, or access removed since it was approved). "
                "Stage it again if it should still go out."
            )
        if command_type is CommandType.EMAIL:
            decision = await guard_outreach(
                ctx,
                channel=Channel.EMAIL,
                contact=str(target["email"]),
                state_code=target.get("state_code"),
            )
            if not decision.allowed:
                raise RuntimeError("Outreach blocked: " + "; ".join(decision.blockers))
            # Commercial email basics (CAN-SPAM, RFC 8058): the approved body
            # goes out unchanged, followed by a fixed footer the draft cannot
            # edit — the brokerage's postal address (when set) and an
            # unsubscribe link — plus the one-click List-Unsubscribe headers.
            # Built before submission so a failure here is a definite "not sent".
            import commercial_email

            commercial = await commercial_email.prepare(
                tenant_id, recipient=str(target["email"]), body=str(draft.get("body") or "")
            )
            await reporter.progress(45, "submitting approved email")
            submission_started = True
            # One sender, chosen by configuration. SMTP is the default, so mail
            # leaves through a server the operator controls rather than any
            # third party's API.
            sender, credential_key = _resolve_email_provider()
            # ctx.agent_id is "command-worker" — the job runner, not the human
            # who drafted this. Their own SMTP identity and Reply-To only
            # resolve against the agent who actually wrote the draft.
            drafting_ctx = TenantContext(
                agent_id=str(command["created_by"] or "command-worker"),
                tenant_id=tenant_id,
                role=Role.PLATFORM_ADMIN,
            )
            # An agent's own SMTP identity (account_label=agent_id) wins over
            # the tenant-wide "default" one falls back to inside the query.
            raw, identity = await asyncio.gather(
                _load_provider_credential(
                    drafting_ctx, credential_key, account_label=drafting_ctx.agent_id
                ),
                load_agent_identity(drafting_ctx),
            )
            credentials = None
            if raw:
                malformed = (
                    f"Encrypted {credential_key.upper()} credential must be a JSON object"
                )
                try:
                    credentials = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    raise ProviderConfigurationError(malformed) from exc
                if not isinstance(credentials, dict):
                    raise ProviderConfigurationError(malformed)
            reply_to = str(identity.get("public_email") or "").strip() or None
            provider_result = await sender(
                {**draft, "target": target, "body": commercial.body},
                credentials=credentials,
                reply_to=reply_to,
                list_unsubscribe_url=commercial.unsubscribe_url,
            )
        elif command_type is CommandType.SMS:
            decision = await guard_outreach(
                ctx,
                channel=Channel.SMS,
                contact=str(target["phone"]),
                state_code=str(target.get("state_code") or ""),
                tz_name=target.get("timezone"),
            )
            if not decision.allowed:
                raise RuntimeError("SMS blocked: " + "; ".join(decision.blockers))
            from messaging_provider import messaging_provider_name

            sms_provider = messaging_provider_name()
            if sms_provider == "telnyx":
                # Same-business-number messaging: the sender MUST be the
                # agent's own READY hosted number, never a client-supplied
                # from_number, and never a silent fallback to legacy Twilio
                # SMS — retries on a fallback provider could double-send.
                from messaging_data import get_messaging_route, get_public_business_number

                messaging_route = await get_messaging_route(owner_ctx)
                business_number = await get_public_business_number(owner_ctx)
                if (
                    messaging_route is None
                    or messaging_route.get("hosted_order_status") != "active"
                    or business_number is None
                ):
                    raise ProviderConfigurationError(
                        "Text message blocked: connect and finish setting up "
                        "text messages for your business number before sending."
                    )
                telnyx_raw = await _load_provider_credential(ctx, "telnyx")
                telnyx_credentials = None
                if telnyx_raw:
                    try:
                        telnyx_credentials = json.loads(telnyx_raw)
                    except (TypeError, ValueError):
                        pass
                await reporter.progress(45, "submitting approved text message")
                submission_started = True
                from messaging_provider import get_messaging_provider

                sender_number = str(business_number["voice_caller_id_e164"])
                adapter = get_messaging_provider("telnyx")
                provider_result = await adapter.send_message(
                    to=str(target["phone"]),
                    from_=sender_number,
                    text=str(draft.get("body") or ""),
                    credentials=telnyx_credentials,
                )
                from messaging_data import record_outbound_message

                # Telnyx has the message. A failure to write our log row must
                # not turn a sent text into an error (and lose its id): the
                # command still records provider_reference below.
                try:
                    await record_outbound_message(
                        tenant_id=ctx.tenant_id,
                        agent_id=owner_ctx.agent_id,
                        contact_id=str(target.get("contact_id")) if target.get("contact_id") else None,
                        client_id=str(target.get("client_id")) if target.get("client_id") else None,
                        provider="telnyx",
                        provider_message_id=provider_result.reference,
                        from_e164=sender_number,
                        to_e164=str(target["phone"]),
                        body=str(draft.get("body") or ""),
                    )
                except Exception:  # noqa: BLE001 - logged; the send itself succeeded
                    logger.exception("SMS %s sent but its log row failed to write", provider_result.reference)
            else:
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
                provider_result = await send_twilio_sms(
                    {**draft, "target": target}, credentials=twilio_credentials
                )
        elif command_type is CommandType.CALL:
            decision = await guard_outreach(
                ctx,
                channel=Channel.VOICE,
                contact=str(target["phone"]),
                state_code=str(target.get("state_code") or ""),
                tz_name=target.get("timezone"),
            )
            if not decision.allowed:
                raise RuntimeError("Call blocked: " + "; ".join(decision.blockers))
            # Neoh places the call, but the client must see the agent's own
            # verified business number — that is the entire point of the
            # "use my existing business number" flow
            # (telephony_routes.voice_caller_id_e164). get_verified_caller_id
            # returns a number only when Twilio has actually confirmed it
            # (outbound_verification_status == 'verified'); anything else —
            # no route connected, pending, failed — must FAIL CLOSED, before
            # any provider submission is attempted (submission_started stays
            # False here so this is recorded as a clean 'failed' command, not
            # 'reconciliation_required'). A caller ID is never fabricated,
            # never "close enough", and never silently substituted with a
            # platform/env Twilio number: an unverified agent cannot place
            # calls under this path at all.
            from inbound_voice import get_telephony_route, get_verified_caller_id

            verified_caller_id = await get_verified_caller_id(owner_ctx)
            if not verified_caller_id:
                raise RuntimeError(
                    "Call blocked: Connect and verify your business number "
                    "before placing calls."
                )
            call_route = await get_telephony_route(owner_ctx)
            call_provider = str((call_route or {}).get("provider") or "twilio")
            if call_provider == "plivo":
                # Plivo is a distinct carrier, never a silent fallback for a
                # failed Twilio submission and vice versa — mixing providers
                # mid-flow risks placing the same approved call twice. The
                # route's own recorded provider decides the rail; there is no
                # try-Plivo-then-Twilio chain here.
                await reporter.progress(45, "placing approved call")
                submission_started = True
                plivo_raw = await _load_provider_credential(ctx, "plivo")
                plivo_credentials = None
                if plivo_raw:
                    try:
                        plivo_credentials = json.loads(plivo_raw)
                    except (TypeError, ValueError):
                        pass
                base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
                if not base:
                    raise ProviderConfigurationError(
                        "ORACLE_PUBLIC_BASE_URL is not set — Plivo answer_url is unavailable"
                    )
                from plivo_call_handler import (
                    PLIVO_CORRELATION_PARAM,
                    ensure_plivo_call_state_available,
                    initialize_outbound_plivo_call_state,
                    record_outbound_plivo_reference,
                )
                from telephony_api import (
                    PLIVO_OUTBOUND_ANSWER_PATH,
                    PLIVO_OUTBOUND_STATUS_PATH,
                )
                from voice_provider import get_voice_provider

                adapter = get_voice_provider("plivo")
                await ensure_plivo_call_state_available()
                account_id = str(
                    (plivo_credentials or {}).get("auth_id")
                    or os.getenv("PLIVO_AUTH_ID", "")
                )
                # Neoh's OWN reference for this call, carried in the callback
                # URLs and stored BEFORE the call exists. Plivo's create
                # response came back with no request id on both real staging
                # calls (2026-10-04, 2026-10-06: fields api_id + message only),
                # so a call keyed on that id had no state and its answer webhook
                # refused it as "unmanaged". The URL is covered by the V3
                # signature, so the reference cannot be forged onto a webhook.
                correlation = str(uuid.uuid4())
                await initialize_outbound_plivo_call_state(
                    correlation,
                    str(target.get("phone") or ""),
                    tenant_id=ctx.tenant_id,
                    account_id=account_id,
                    briefing=str(draft.get("reason") or ""),
                )
                ref_query = f"?{PLIVO_CORRELATION_PARAM}={correlation}"
                provider_result = await adapter.place_call(
                    to_number=str(target.get("phone") or ""),
                    from_number=verified_caller_id,
                    # Imported, not re-typed: these URLs were once written as
                    # /api/commands/… — a path with no route — so every
                    # outbound Plivo call fetched a 404 for its instructions.
                    answer_url=f"{base}{PLIVO_OUTBOUND_ANSWER_PATH}{ref_query}",
                    status_callback_url=f"{base}{PLIVO_OUTBOUND_STATUS_PATH}{ref_query}",
                    credentials=plivo_credentials,
                    correlation_ref=correlation,
                )
                try:
                    await record_outbound_plivo_reference(correlation, provider_result.reference)
                except Exception:
                    await adapter.abort_call(
                        provider_result.reference, credentials=plivo_credentials
                    )
                    raise
            else:
                twilio_raw = await _load_provider_credential(ctx, "twilio")
                twilio_credentials = None
                if twilio_raw:
                    try:
                        twilio_credentials = json.loads(twilio_raw)
                    except (TypeError, ValueError):
                        pass
                twilio_credentials = {
                    **(twilio_credentials or {}),
                    "from_number": verified_caller_id,
                }
                try:
                    from twilio_call_handler import (
                        ensure_twilio_call_state_available,
                        initialize_twilio_call_state,
                    )

                    await ensure_twilio_call_state_available()
                    # From here a call may exist at Twilio even if no answer
                    # comes back: a timeout or dropped response must become
                    # reconciliation_required, never a clean "failed" that the
                    # job retries into a second call (resilience drill 2026-10-02).
                    submission_started = True
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
                    if not os.getenv("ORACLE_CUSTOM_CALL_API_URL", "").strip():
                        raise
                    # A custom call URL is configured as the fallback; let its own
                    # ProviderConfigurationError (if any) propagate instead of
                    # Twilio's, since it is the error that actually applies.
                    submission_started = True
                    provider_result = await place_custom_http_call(
                        {**draft, "target": target}
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
            # This write is the ONLY record that the command failed. If it is
            # lost the row stays state='executing' forever, carrying no
            # last_error, and — in the uncertain branch below — this function
            # still returns a success-shaped dict. Swallowing it silently left
            # an operator with a permanently stuck command and no trace of
            # either failure. Log both: the write that failed, and the original
            # provider error it was trying to record.
            logger.exception(
                "command %s failed and its failure could not be recorded; "
                "the row remains 'executing'. Original error: %s",
                command_id,
                exc,
            )
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
                            tenant_id,command_id,contact_id,client_id,lead_id,consent_recorded,
                            consent_basis,transcript_status,provider_call_id,created_by
                        ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,false,
                                  'awaiting_explicit_live_transcription_consent',
                                  'pending',$6,$7)
                        ON CONFLICT (command_id) WHERE command_id IS NOT NULL DO NOTHING
                        """,
                        tenant_id,
                        command_id,
                        target.get("contact_id"),
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
    # The acknowledgement above is the one write that must not be lost. What
    # follows is bookkeeping that makes the send visible to the intelligence
    # layer, and it is deliberately OUTSIDE the retry loop and best-effort: a
    # failure here is logged, never raised, and can neither re-run the
    # acknowledgement nor turn a delivered message into a failed job.
    await _record_send_bookkeeping(
        ctx,
        command_id=command_id,
        command_type=command_type,
        target=target,
        draft=draft,
        provider_result=provider_result,
        actor=str(reporter.job.get("created_by") or "") or None,
    )
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
    # The number Twilio dialled, which is what the TTS allowlist is keyed on —
    # not the caller's, so a test number cannot be spoofed into the feature by
    # whoever is calling.
    to_number = str(form.get("To") or "")
    greeting_speech = await _spoken(greeting, to_number=to_number)
    if twilio_qwen_enabled(state):
        stream_url = twilio_media_websocket_url()
        bridge_token = create_twilio_bridge_token(call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {greeting_speech}
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
    {greeting_speech}
    <Gather {gather_attribs}>
        <Say voice="Polly.Joanna">I'm listening.</Say>
    </Gather>
    <Say voice="Polly.Joanna">I didn't catch that. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def _spoken(text: str, *, to_number: str) -> str:
    """One line of TwiML speech: ElevenLabs when permitted, Polly otherwise.

    Returns a `<Play>` element for a call on the TTS test allowlist and a
    `<Say>` for everything else — including every failure path inside
    voice_tts, which returns None rather than raising. The caller has to hear
    something: the first line of every call is the FCC-required AI disclosure,
    and silence there is both a broken call and a compliance failure.
    """
    import voice_tts

    url = await voice_tts.play_url(text, to_number=to_number)
    if url:
        return f'<Play>{_xml_escape(url)}</Play>'
    return f'<Say voice="Polly.Joanna">{_xml_escape(text)}</Say>'


@router.get("/webhooks/twilio/tts/{token}.mp3", include_in_schema=False)
async def twilio_tts_audio(token: str):
    """Serve a rendered line to Twilio.

    Unauthenticated because Twilio fetches it directly, which is safe here: the
    token is a SHA-256 prefix of the audio itself, so it is unguessable and
    reveals nothing but a line the caller is about to hear anyway. Entries
    expire minutes after the call leg that created them.
    """
    import voice_tts

    audio = await voice_tts.cache.get(token)
    if audio is None:
        raise HTTPException(status_code=404, detail="Audio expired.")
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


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
        # Awaited, not fire-and-forget: if the write fails the exception
        # becomes a 503 and Twilio retries, instead of the outcome vanishing.
        if call_status in {"in-progress", "answered"}:
            await _update_call_session_async(call_sid, "in-progress")
        elif call_status in {"completed", "busy", "failed", "no-answer", "canceled"}:
            await _update_call_session_async(call_sid, "completed", outcome=call_status)
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
    is_authentic = await _run_sync_in_executor(
        verify_twilio_websocket_signature,
        signature,
        timeout_seconds=5.0,
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


@router.websocket("/media/plivo")
async def plivo_qwen_media(websocket: WebSocket):
    """Authenticate and bridge Plivo's 8 kHz mu-law stream to Qwen Omni.

    Plivo's PlivoXML <Stream> element carries only a bare WSS URL (no nested
    parameters, unlike Twilio's <Stream><Parameter>), so the bridge_token
    voice_provider.PlivoVoiceProvider.speak_and_stream_markup mints travels
    as a query parameter on the URL itself instead of inside the start
    frame. The socket must be accepted before the start frame (carrying
    Plivo's call id) can be read, so full validation completes just after
    accept rather than before it — the token is still call-id-bound and
    single-use-short-lived, so a stolen token cannot be replayed against a
    different call.
    """
    from qwen_omni_realtime import (
        PlivoQwenRealtimeBridge,
        QwenCallLimitReached,
        QwenRealtimeError,
    )
    from plivo_call_handler import authorize_plivo_media, mark_plivo_streaming

    bridge_token = websocket.query_params.get("bridge_token", "")
    if not bridge_token:
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
    call_uuid = str(start.get("callId") or "")
    if not await authorize_plivo_media(call_uuid, bridge_token):
        await websocket.close(code=4403)
        return

    from elevenlabs_realtime import (
        ElevenLabsCallLimitReached,
        ElevenLabsRealtimeError,
        PlivoElevenLabsBridge,
        realtime_provider,
    )

    use_elevenlabs = realtime_provider() == "elevenlabs"
    try:
        bridge = (PlivoElevenLabsBridge(websocket, call_uuid, start_event) if use_elevenlabs
                  else PlivoQwenRealtimeBridge(websocket, call_uuid, start_event))
    except (QwenRealtimeError, ElevenLabsRealtimeError):
        await websocket.close(code=4400)
        return
    await mark_plivo_streaming(call_uuid)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        logger.info("Plivo media socket disconnected: uuid=%s", call_uuid)
    except (QwenCallLimitReached, ElevenLabsCallLimitReached) as exc:
        # A spoken opt-out (L09-05) is a limit subclass: same hang-up.
        logger.info("Realtime call ended (%s): uuid=%s",
                    "callee opted out" if "opt" in type(exc).__name__.lower() else "turn limit", call_uuid)
        from voice_provider import get_voice_provider

        await get_voice_provider("plivo").abort_call(call_uuid)
    except (QwenRealtimeError, ElevenLabsRealtimeError):
        logger.exception("Plivo realtime bridge failed (%s): uuid=%s",
                         "elevenlabs" if use_elevenlabs else "qwen", call_uuid)
        await websocket.close(code=1011)
    except Exception:
        logger.exception("Unexpected Plivo/Qwen bridge failure: uuid=%s", call_uuid)
        await websocket.close(code=1011)


@router.post("/webhooks/twilio/speech", include_in_schema=False)
async def twilio_speech(request: Request):
    """Handle speech recognized by Twilio Gather. Calls the AI, returns new TwiML."""
    form = await request.form()
    _validate_twilio_webhook_signature(request, form, "/api/commands/webhooks/twilio/speech")
    speech_text = (form.get("SpeechResult") or "").strip()
    call_sid = form.get("CallSid", "")

    # The transcript itself is deliberately NOT logged. Call transcripts have
    # their own retention policy (ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS, see
    # platform_policy.PUBLIC_PROPERTY_DATA_POLICY); printing 100 characters of
    # what a caller said to stdout put that content in the container log stream
    # instead, where it lives under the log group's retention and nobody's
    # transcript policy. The length is enough to debug a silent leg.
    logger.info(
        "twilio speech webhook: call_sid=%s speech_chars=%d",
        call_sid, len(speech_text),
    )

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

    # The reply is the line worth hearing in a voice demo — the greeting is
    # fixed text, this is the conversation. Synthesis happens after
    # _generate_voice_reply has already spent its budget, so voice_tts keeps a
    # short timeout of its own and falls back to Say rather than extending the
    # wait a caller is already sitting through.
    reply_speech = await _spoken(ai_reply, to_number=str(form.get("To") or ""))

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {reply_speech}
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
    await _require_webhook_secret(request, "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET")
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

    logger.info(
        "custom-call webhook: event=%s status=%s call_id=%s", event, status, call_id
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
