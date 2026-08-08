"""Authenticated telephony configuration and signed Twilio inbound webhooks."""

from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from html import escape as xml_escape
from typing import Any, Literal, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from inbound_voice import (
    InboundVoiceError,
    finalize_inbound_voice_call,
    get_inbound_call,
    list_inbound_calls,
    list_telephony_routes,
    normalize_e164,
    prepare_inbound_call,
    record_forward_attempt,
    resolve_forward_target,
    resolve_inbound_call_route,
    resolve_inbound_route,
    update_inbound_call_status,
    upsert_telephony_route,
)
from commands_api import _load_provider_credential
from contacts_api import _CONTACT_SELECT, _contact_json
from db.connection import tenant_tx
from outreach_compliance import Channel, VoiceMode, guard_outreach
from platform_policy import Feature, require_feature
from tenancy import Role, TenantContext, require_context
from twilio_call_handler import (
    TwilioCallStateUnavailable,
    cleanup_twilio_call,
    create_twilio_bridge_token,
    initialize_inbound_twilio_call_state,
    twilio_media_websocket_url,
    twilio_qwen_enabled,
)

logger = logging.getLogger("oracle.telephony")

router = APIRouter(prefix="/api/telephony", tags=["Telephony"])

_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
_SIP_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_TERMINAL_CALL_STATUSES = {
    "completed",
    "busy",
    "failed",
    "no-answer",
    "canceled",
    "declined",
}


class TelephonyRouteUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    inbound_did: str
    twilio_account_sid: str = Field(min_length=34, max_length=34)
    intake_mode: Literal["buyer", "seller", "auto"] = "auto"
    forwarding_mode: Literal["none", "carrier_conditional", "sip"] = "none"
    forwarding_source_e164: Optional[str] = None
    sip_domain: Optional[str] = Field(default=None, max_length=253)
    voice_caller_id_e164: Optional[str] = None
    voice_caller_id_verified: bool = False
    sms_sender_e164: Optional[str] = None
    sms_sender_type: Optional[
        Literal[
            "twilio_registered",
            "ported",
            "toll_free_verified",
        ]
    ] = None
    active: bool = True

    # Live hand-off: the agent's own reachable phone. This is the transfer
    # TARGET, not forwarding_source_e164 (the legacy number pointing INTO the
    # Neoh DID).
    agent_forward_e164: Optional[str] = None
    forward_on_request: bool = True
    forward_when_ai_unavailable: bool = True
    forward_timeout_seconds: int = Field(default=25, ge=5, le=120)

    @field_validator(
        "inbound_did",
        "forwarding_source_e164",
        "voice_caller_id_e164",
        "sms_sender_e164",
        "agent_forward_e164",
    )
    @classmethod
    def validate_phone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return normalize_e164(value)

    @field_validator("twilio_account_sid")
    @classmethod
    def validate_account_sid(cls, value: str) -> str:
        if not _ACCOUNT_SID_RE.fullmatch(value):
            raise ValueError("must be a Twilio Account SID")
        return value

    @field_validator("sip_domain")
    @classmethod
    def validate_sip_domain(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        candidate = value.rstrip(".").lower()
        if not _SIP_DOMAIN_RE.fullmatch(candidate) or ".." in candidate:
            raise ValueError("must be a hostname without credentials or a URI path")
        return candidate

    @model_validator(mode="after")
    def validate_channel_rules(self) -> "TelephonyRouteUpsert":
        if self.forwarding_mode == "carrier_conditional" and not self.forwarding_source_e164:
            raise ValueError("carrier conditional forwarding requires forwarding_source_e164")
        if self.forwarding_mode == "sip" and not self.sip_domain:
            raise ValueError("SIP forwarding requires sip_domain")
        if self.voice_caller_id_verified and not self.voice_caller_id_e164:
            raise ValueError("verified voice caller ID requires voice_caller_id_e164")
        if bool(self.sms_sender_e164) != bool(self.sms_sender_type):
            raise ValueError("SMS sender and registered sender type must be configured together")
        if not self.agent_forward_e164:
            # Turning a hand-off on without a destination is only an error when
            # the caller actually asked for it; a client that predates the
            # feature and omits the fields entirely just gets it disabled.
            explicit = self.model_fields_set & {
                "forward_on_request",
                "forward_when_ai_unavailable",
            }
            if any(getattr(self, name) for name in explicit):
                raise ValueError(
                    "call hand-off requires agent_forward_e164 — the number the AI should transfer to"
                )
            self.forward_on_request = False
            self.forward_when_ai_unavailable = False
        if self.agent_forward_e164 and self.agent_forward_e164 == self.inbound_did:
            raise ValueError(
                "agent_forward_e164 cannot be the inbound DID — the transfer would loop back"
            )
        return self


class AgentCallPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str

    @field_validator("contact_id")
    @classmethod
    def validate_contact_id(cls, value: str) -> str:
        return str(uuid.UUID(value))


def _public_base_url(request: Request) -> str:
    configured = os.getenv("ORACLE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        parsed = urlsplit(configured)
        is_development = os.getenv("ORACLE_ENV", "").lower() in {
            "dev",
            "development",
            "local",
            "test",
        }
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (not is_development and parsed.scheme != "https")
        ):
            raise HTTPException(
                status_code=503,
                detail="Public telephony URL is not configured safely.",
            )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    if os.getenv("ORACLE_ENV", "").lower() not in {
        "dev",
        "development",
        "local",
        "test",
    }:
        raise HTTPException(
            status_code=503,
            detail="ORACLE_PUBLIC_BASE_URL is required for telephony webhooks.",
        )
    host = request.headers.get("host") or request.url.netloc
    scheme = request.url.scheme or "http"
    if not host:
        raise HTTPException(status_code=400, detail="Unable to determine webhook URL.")
    return f"{scheme}://{host}"


def _canonical_webhook_url(request: Request, suffix: str) -> str:
    return f"{_public_base_url(request)}{suffix}"


async def transfer_webhook_url(call_sid: str, *, reason: str) -> str:
    """Absolute transfer URL for a live call, or "" when hand-off is not possible.

    Used by the realtime media bridge, which has no HTTP Request to derive a host
    from, so ORACLE_PUBLIC_BASE_URL must be configured for hand-off to work.
    """
    base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        logger.error(
            "ORACLE_PUBLIC_BASE_URL is not set — live agent hand-off is unavailable"
        )
        return ""
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.error("ORACLE_PUBLIC_BASE_URL is malformed — hand-off is unavailable")
        return ""
    target = await resolve_forward_target(call_sid, reason=reason)
    if target is None:
        return ""
    return (
        f"{base}/api/telephony/webhooks/twilio/inbound/"
        f"{target['endpoint_key']}/transfer?reason={quote(reason, safe='')}"
    )


def _normalize_twilio_form(form: Any) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in form.multi_items():
        if isinstance(value, str):
            normalized[str(key)] = value
    return normalized


def _twilio_auth_tokens() -> list[str]:
    current = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    previous = os.getenv("TWILIO_AUTH_TOKEN_PREVIOUS", "").strip()
    return list(dict.fromkeys(token for token in (current, previous) if token))


def validate_twilio_signature(
    request: Request,
    form: Any,
    suffix: str,
    *,
    tokens: Optional[list[str]] = None,
) -> None:
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Twilio-Signature header.")
    tokens = list(dict.fromkeys(token for token in (tokens or _twilio_auth_tokens()) if token))
    if not tokens:
        raise HTTPException(
            status_code=503,
            detail="Twilio webhook validation is not configured.",
        )
    try:
        from twilio.request_validator import RequestValidator
    except Exception as exc:
        logger.exception("Twilio request validator import failed")
        raise HTTPException(
            status_code=503,
            detail="Twilio webhook validation is unavailable.",
        ) from exc

    canonical_url = _canonical_webhook_url(request, suffix)
    params = _normalize_twilio_form(form)
    if any(
        RequestValidator(token).validate(canonical_url, params, signature)
        for token in tokens
    ):
        return
    logger.warning("Rejected invalid Twilio webhook signature path=%s", suffix)
    raise HTTPException(status_code=400, detail="Invalid Twilio signature.")


def _agent_identity(ctx: TenantContext) -> str:
    digest = hashlib.sha256(f"{ctx.tenant_id}:{ctx.agent_id}".encode("utf-8")).hexdigest()[:32]
    return f"oracle_agent_{digest}"


async def _twilio_credentials(ctx: TenantContext) -> dict[str, str]:
    credentials: dict[str, Any] = {}
    raw = await _load_provider_credential(ctx, "twilio")
    if raw:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Stored Twilio credential is invalid.") from exc
        if not isinstance(decoded, dict):
            raise HTTPException(status_code=503, detail="Stored Twilio credential is invalid.")
        credentials.update(decoded)
    for key, env_name in (
        ("account_sid", "TWILIO_ACCOUNT_SID"),
        ("auth_token", "TWILIO_AUTH_TOKEN"),
        ("api_key", "TWILIO_API_KEY"),
        ("api_secret", "TWILIO_API_SECRET"),
        ("twiml_app_sid", "TWILIO_TWIML_APP_SID"),
    ):
        credentials.setdefault(key, os.getenv(env_name, ""))
    return {key: str(value or "").strip() for key, value in credentials.items()}


async def _route_twilio_tokens(route: dict[str, Any]) -> list[str]:
    """Return signing tokens only for the tenant/account owning this route."""
    tenant_ctx = TenantContext(
        agent_id=str(route["agent_id"]),
        tenant_id=str(route["tenant_id"]),
        role=Role.AGENT,
    )
    credentials = await _twilio_credentials(tenant_ctx)
    if credentials.get("account_sid") != str(route["twilio_account_sid"]):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Twilio route credential does not match the configured account.",
        )
    return list(
        dict.fromkeys(
            token
            for token in (credentials.get("auth_token", ""), *_twilio_auth_tokens())
            if token
        )
    )


async def _agent_intent_platform(intent_id: str) -> Optional[dict[str, Any]]:
    platform_ctx = TenantContext(
        agent_id="twilio-agent-webhook",
        tenant_id=os.getenv(
            "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        ),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_call_intents WHERE id=$1::uuid",
            intent_id,
        )
    return dict(row) if row else None


def _safe_hangup(message: str = "This call cannot be connected safely. Goodbye.") -> Response:
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{xml_escape(message)}</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _dial_agent(
    forward_e164: str,
    *,
    caller_id: str,
    timeout_seconds: int,
    say: str,
    action_url: str = "",
) -> Response:
    """Bridge the live caller to the agent's own phone."""
    action = (
        f' action="{xml_escape(action_url, quote=True)}" method="POST"'
        if action_url
        else ""
    )
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{xml_escape(say)}</Say>
    <Dial callerId="{xml_escape(caller_id, quote=True)}" timeout="{int(timeout_seconds)}"{action}>
        <Number>{xml_escape(forward_e164)}</Number>
    </Dial>
    <Say voice="Polly.Joanna">Your agent is not available right now. They have your details and will call you back. Goodbye.</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _route_json(row: dict[str, Any], request: Request) -> dict[str, Any]:
    endpoint_key = str(row["endpoint_key"])
    base = _public_base_url(request)
    webhook_path = f"/api/telephony/webhooks/twilio/inbound/{endpoint_key}"
    status_path = f"/api/telephony/webhooks/twilio/status/{endpoint_key}"
    return {
        **row,
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "endpoint_key": endpoint_key,
        "voice_webhook_url": f"{base}{webhook_path}",
        "status_callback_url": f"{base}{status_path}",
        "voice_caller_id_policy": (
            "Verified caller ID can be used for outbound voice only."
        ),
        "sms_sender_policy": (
            "SMS requires a registered Twilio, ported, or toll-free verified sender."
        ),
        "call_handoff_policy": (
            "The AI transfers a live caller to agent_forward_e164 when the caller "
            "asks for a person, or when the assistant is unavailable. Leave the "
            "number blank to disable hand-off entirely."
        ),
    }


@router.get("/routes")
async def routes(
    request: Request,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    rows = await list_telephony_routes(ctx)
    return {"routes": [_route_json(row, request) for row in rows]}


@router.put("/routes/me")
async def configure_route(
    body: TelephonyRouteUpsert,
    request: Request,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    try:
        row = await upsert_telephony_route(ctx, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(
                status_code=409,
                detail="That inbound number is already assigned to an active Neoh route.",
            ) from exc
        raise
    return _route_json(row, request)


@router.get("/calls")
async def calls(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    return {"calls": await list_inbound_calls(ctx, limit=limit)}


@router.get("/calls/{call_id}")
async def call_detail(
    call_id: str,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    item = await get_inbound_call(ctx, call_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Inbound call not found.")
    return item


@router.get("/agent/token")
async def agent_voice_token(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Mint a short-lived Twilio Voice SDK token for the authenticated agent."""
    require_feature(Feature.POWER_DIALER)
    credentials = await _twilio_credentials(ctx)
    required = ("account_sid", "api_key", "api_secret", "auth_token", "twiml_app_sid")
    if any(not credentials.get(key) for key in required):
        raise HTTPException(
            status_code=503,
            detail="Browser calling requires Twilio Account SID, API key pair, auth token, and TwiML App SID.",
        )
    if not _ACCOUNT_SID_RE.fullmatch(credentials["account_sid"]):
        raise HTTPException(status_code=503, detail="Twilio Account SID is invalid.")
    if not re.fullmatch(r"^SK[0-9A-Fa-f]{32}$", credentials["api_key"]):
        raise HTTPException(status_code=503, detail="Twilio API key SID is invalid.")
    if not re.fullmatch(r"^AP[0-9A-Fa-f]{32}$", credentials["twiml_app_sid"]):
        raise HTTPException(status_code=503, detail="Twilio TwiML App SID is invalid.")
    async with tenant_tx(ctx) as conn:
        route = await conn.fetchrow(
            """
            SELECT id FROM telephony_routes
             WHERE agent_id=$1 AND active=true
               AND voice_caller_id_verified=true
               AND voice_caller_id_e164 IS NOT NULL
             LIMIT 1
            """,
            ctx.agent_id,
        )
    if route is None:
        raise HTTPException(status_code=409, detail="A verified outbound voice route is required.")
    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant

        token = AccessToken(
            credentials["account_sid"],
            credentials["api_key"],
            credentials["api_secret"],
            identity=_agent_identity(ctx),
            ttl=600,
        )
        token.add_grant(
            VoiceGrant(
                outgoing_application_sid=credentials["twiml_app_sid"],
                incoming_allow=False,
            )
        )
        jwt_token = token.to_jwt()
        if isinstance(jwt_token, bytes):
            jwt_token = jwt_token.decode("ascii")
    except Exception as exc:
        logger.exception("Unable to mint Twilio browser token")
        raise HTTPException(status_code=503, detail="Browser calling token is unavailable.") from exc
    return {
        "token": jwt_token,
        "identity": _agent_identity(ctx),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "recording_enabled": False,
    }


@router.post("/agent/calls/prepare", status_code=status.HTTP_201_CREATED)
async def prepare_agent_call(
    body: AgentCallPrepare,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Bind a canonical contact to a five-minute, single-use browser-call intent."""
    require_feature(Feature.POWER_DIALER)
    async with tenant_tx(ctx) as conn:
        contact_row = await conn.fetchrow(
            _CONTACT_SELECT
            + " WHERE contact.id=$1::uuid AND contact.deleted_at IS NULL",
            body.contact_id,
        )
        if contact_row is None:
            raise HTTPException(status_code=404, detail="Contact not found.")
        contact = await _contact_json(conn, ctx, contact_row)
        route = await conn.fetchrow(
            """
            SELECT id,voice_caller_id_e164,voice_caller_id_verified
              FROM telephony_routes
             WHERE agent_id=$1 AND active=true LIMIT 1
            """,
            ctx.agent_id,
        )
    if route is None or not route["voice_caller_id_verified"] or not route["voice_caller_id_e164"]:
        raise HTTPException(status_code=409, detail="A verified outbound voice route is required.")
    if not contact.get("phone"):
        raise HTTPException(status_code=409, detail="Contact has no callable phone number.")
    decision = await guard_outreach(
        ctx,
        channel=Channel.VOICE,
        contact=str(contact["phone"]),
        state_code=contact.get("state_code"),
        tz_name=contact.get("timezone"),
        log=False,
        voice_mode=VoiceMode.AGENT,
        recording_enabled=False,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
            detail={
                "message": "Agent call is blocked by compliance.",
                "blockers": list(decision.blockers),
                "warnings": list(decision.warnings),
            },
        )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    async with tenant_tx(ctx) as conn:
        intent = await conn.fetchrow(
            """
            INSERT INTO agent_call_intents (
                tenant_id,agent_id,contact_id,state,expires_at
            ) VALUES ($1::uuid,$2,$3::uuid,'prepared',$4) RETURNING id,expires_at,created_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            body.contact_id,
            expires_at,
        )
    return {
        "intent_id": str(intent["id"]),
        "contact_id": body.contact_id,
        "expires_at": intent["expires_at"].isoformat(),
        "warnings": list(decision.warnings),
        "required_disclosures": list(decision.required_disclosures),
        "recording_enabled": False,
    }


@router.get("/agent/calls")
async def agent_calls(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_feature(Feature.POWER_DIALER)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,contact_id,state,provider_call_sid,failure_reason,expires_at,
                   created_at,authorized_at,completed_at,updated_at
              FROM agent_call_intents
             WHERE agent_id=$1 ORDER BY created_at DESC LIMIT $2
            """,
            ctx.agent_id,
            limit,
        )
    return {
        "calls": [
            {
                **dict(row),
                "id": str(row["id"]),
                "contact_id": str(row["contact_id"]),
                **{
                    key: row[key].isoformat() if row[key] else None
                    for key in ("expires_at", "created_at", "authorized_at", "completed_at", "updated_at")
                },
            }
            for row in rows
        ]
    }


@router.post("/webhooks/twilio/agent", include_in_schema=False)
async def twilio_agent_outbound(request: Request) -> Response:
    """Signed TwiML App webhook; consumes an intent and resolves its contact server-side."""
    form = await request.form()
    intent_id = str(form.get("intent_id") or "")
    try:
        uuid.UUID(intent_id)
    except (TypeError, ValueError, AttributeError):
        return _safe_hangup()
    intent = await _agent_intent_platform(intent_id)
    if intent is None:
        return _safe_hangup()
    tenant_ctx = TenantContext(
        agent_id=str(intent["agent_id"]),
        tenant_id=str(intent["tenant_id"]),
        role=Role.AGENT,
    )
    credentials = await _twilio_credentials(tenant_ctx)
    validate_twilio_signature(
        request,
        form,
        "/api/telephony/webhooks/twilio/agent",
        tokens=[credentials.get("auth_token", ""), *_twilio_auth_tokens()],
    )
    call_sid = str(form.get("CallSid") or "")
    async with tenant_tx(tenant_ctx) as conn:
        locked = await conn.fetchrow(
            """
            SELECT * FROM agent_call_intents
             WHERE id=$1::uuid AND agent_id=$2 FOR UPDATE
            """,
            intent_id,
            tenant_ctx.agent_id,
        )
        if (
            locked is None
            or locked["state"] != "prepared"
            or locked["expires_at"] <= datetime.now(timezone.utc)
        ):
            if locked and locked["state"] == "prepared":
                await conn.execute(
                    "UPDATE agent_call_intents SET state='expired',completed_at=now() WHERE id=$1::uuid",
                    intent_id,
                )
            return _safe_hangup()
        contact_row = await conn.fetchrow(
            _CONTACT_SELECT + " WHERE contact.id=$1::uuid AND contact.deleted_at IS NULL",
            str(locked["contact_id"]),
        )
        route = await conn.fetchrow(
            """
            SELECT voice_caller_id_e164,voice_caller_id_verified
              FROM telephony_routes
             WHERE agent_id=$1 AND active=true LIMIT 1
            """,
            tenant_ctx.agent_id,
        )
        contact = await _contact_json(conn, tenant_ctx, contact_row) if contact_row else None
    if (
        not contact
        or not contact.get("phone")
        or not route
        or not route["voice_caller_id_verified"]
        or not route["voice_caller_id_e164"]
    ):
        return _safe_hangup()
    decision = await guard_outreach(
        tenant_ctx,
        channel=Channel.VOICE,
        contact=str(contact["phone"]),
        state_code=contact.get("state_code"),
        tz_name=contact.get("timezone"),
        log=True,
        voice_mode=VoiceMode.AGENT,
        recording_enabled=False,
    )
    if not decision.allowed:
        async with tenant_tx(tenant_ctx) as conn:
            await conn.execute(
                "UPDATE agent_call_intents SET state='failed',failure_reason=$2,completed_at=now() WHERE id=$1::uuid",
                intent_id,
                "; ".join(decision.blockers)[:2_000],
            )
        return _safe_hangup("This call cannot be connected because contact permission or calling-hour requirements are not met.")
    async with tenant_tx(tenant_ctx) as conn:
        updated = await conn.fetchrow(
            """
            UPDATE agent_call_intents
               SET state='authorized',provider_call_sid=$2,authorized_at=now()
             WHERE id=$1::uuid AND state='prepared' RETURNING id
            """,
            intent_id,
            call_sid if re.fullmatch(r"^CA[0-9A-Fa-f]{32}$", call_sid) else None,
        )
    if updated is None:
        return _safe_hangup()
    callback = (
        f"{_public_base_url(request)}/api/telephony/webhooks/twilio/agent/status"
        f"?intent_id={intent_id}"
    )
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{xml_escape(str(route['voice_caller_id_e164']), quote=True)}"
          answerOnBridge="true" record="do-not-record">
        <Number statusCallback="{xml_escape(callback, quote=True)}"
                statusCallbackMethod="POST"
                statusCallbackEvent="initiated ringing answered completed">{xml_escape(str(contact['phone']))}</Number>
    </Dial>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post("/webhooks/twilio/agent/status", include_in_schema=False)
async def twilio_agent_status(request: Request) -> Response:
    intent_id = str(request.query_params.get("intent_id") or "")
    try:
        uuid.UUID(intent_id)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Call intent not found.")
    intent = await _agent_intent_platform(intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="Call intent not found.")
    tenant_ctx = TenantContext(agent_id=str(intent["agent_id"]), tenant_id=str(intent["tenant_id"]), role=Role.AGENT)
    credentials = await _twilio_credentials(tenant_ctx)
    form = await request.form()
    validate_twilio_signature(
        request,
        form,
        f"/api/telephony/webhooks/twilio/agent/status?intent_id={intent_id}",
        tokens=[credentials.get("auth_token", ""), *_twilio_auth_tokens()],
    )
    call_status = str(form.get("CallStatus") or "").strip().lower()
    mapped = {
        "initiated": "authorized",
        "ringing": "ringing",
        "answered": "in_progress",
        "in-progress": "in_progress",
        "completed": "completed",
        "busy": "failed",
        "failed": "failed",
        "no-answer": "failed",
        "canceled": "cancelled",
    }.get(call_status)
    if mapped:
        terminal = mapped in {"completed", "failed", "cancelled"}
        async with tenant_tx(tenant_ctx) as conn:
            await conn.execute(
                """
                UPDATE agent_call_intents
                   SET state=$2,
                       failure_reason=CASE WHEN $2='failed' THEN $3 ELSE failure_reason END,
                       completed_at=CASE WHEN $4 THEN now() ELSE completed_at END
                 WHERE id=$1::uuid
                """,
                intent_id,
                mapped,
                call_status if mapped == "failed" else None,
                terminal,
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/webhooks/twilio/inbound/{endpoint_key}",
    include_in_schema=False,
)
async def twilio_inbound_webhook(endpoint_key: str, request: Request) -> Response:
    """Resolve DID to tenant+agent, disclose AI, then open the existing bridge."""
    try:
        uuid.UUID(endpoint_key)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Inbound route not found.")
    suffix = f"/api/telephony/webhooks/twilio/inbound/{endpoint_key}"
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    account_sid = str(form.get("AccountSid") or "")
    caller_phone = str(form.get("From") or "")
    inbound_did = str(form.get("To") or "")
    route = await resolve_inbound_route(endpoint_key, inbound_did, account_sid)
    if route is None:
        logger.warning("Inbound call did not match an active route")
        return _safe_hangup()
    validate_twilio_signature(
        request,
        form,
        suffix,
        tokens=await _route_twilio_tokens(route),
    )

    agent_forward = str(route.get("agent_forward_e164") or "")
    forward_on_request = bool(agent_forward and route.get("forward_on_request"))
    forward_when_unavailable = bool(
        agent_forward and route.get("forward_when_ai_unavailable")
    )
    forward_caller_id = str(route.get("voice_caller_id_e164") or "") or inbound_did
    forward_timeout = int(route.get("forward_timeout_seconds") or 25)

    try:
        binding = await prepare_inbound_call(
            route,
            call_sid=call_sid,
            caller_phone=caller_phone,
        )
        state = await initialize_inbound_twilio_call_state(
            call_sid,
            inbound_did,
            tenant_id=binding.tenant_id,
            agent_id=binding.agent_id,
            account_sid=account_sid,
            route_id=binding.route_id,
            voice_call_id=binding.call_id,
            intake_mode=binding.intake_mode,
            contact_id=binding.contact_id,
            client_id=binding.client_id,
            forward_available=forward_on_request,
        )
    except (ValueError, InboundVoiceError, TwilioCallStateUnavailable):
        logger.exception("Inbound call could not be bound safely: sid=%s", call_sid)
        if call_sid:
            try:
                await update_inbound_call_status(call_sid, "failed")
            except Exception:
                logger.exception("Unable to mark failed inbound binding: sid=%s", call_sid)
        return _safe_hangup()

    from outreach_compliance import AI_VOICE_DISCLOSURE

    async def _ai_unavailable() -> Response:
        """Hand the caller to the agent rather than dropping them, when possible."""
        await finalize_inbound_voice_call(call_sid, [], state)
        if forward_when_unavailable:
            await record_forward_attempt(
                call_sid, reason="ai_unavailable", outcome="requested"
            )
            # Leave the call in progress — the Dial decides the final status.
            return _dial_agent(
                agent_forward,
                caller_id=forward_caller_id,
                timeout_seconds=forward_timeout,
                say="Thanks for calling. Connecting you to your agent now.",
                action_url=_canonical_webhook_url(
                    request,
                    f"/api/telephony/webhooks/twilio/inbound/{endpoint_key}/handoff",
                ),
            )
        await update_inbound_call_status(call_sid, "failed")
        return _safe_hangup(
            AI_VOICE_DISCLOSURE
            + " The realtime assistant is unavailable, but your agent has been notified. Goodbye."
        )

    if not twilio_qwen_enabled(state):
        return await _ai_unavailable()

    try:
        stream_url = twilio_media_websocket_url()
        bridge_token = create_twilio_bridge_token(call_sid)
    except (ValueError, TwilioCallStateUnavailable):
        logger.exception("Inbound media binding is unavailable: sid=%s", call_sid)
        return await _ai_unavailable()
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{xml_escape(AI_VOICE_DISCLOSURE)}</Say>
    <Connect>
        <Stream url="{xml_escape(stream_url, quote=True)}">
            <Parameter name="bridge_token" value="{xml_escape(bridge_token, quote=True)}"/>
        </Stream>
    </Connect>
    <Say voice="Polly.Joanna">The realtime assistant is unavailable. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.post(
    "/webhooks/twilio/inbound/{endpoint_key}/transfer",
    include_in_schema=False,
)
async def twilio_inbound_transfer(endpoint_key: str, request: Request) -> Response:
    """TwiML fetched when a live AI call is redirected to the agent.

    The realtime bridge redirects the call to this URL rather than injecting raw
    TwiML, so the hand-off stays signature-validated and the destination number
    is read from the database at transfer time instead of travelling over the
    wire.
    """
    suffix = f"/api/telephony/webhooks/twilio/inbound/{endpoint_key}/transfer"
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    account_sid = str(form.get("AccountSid") or "")
    route = await resolve_inbound_call_route(endpoint_key, call_sid, account_sid)
    if route is None:
        raise HTTPException(status_code=404, detail="Inbound call not found.")
    validate_twilio_signature(
        request, form, suffix, tokens=await _route_twilio_tokens(route)
    )

    reason = str(request.query_params.get("reason") or "caller_request")
    target = await resolve_forward_target(call_sid, reason=reason)
    if target is None:
        logger.warning("Hand-off requested with no eligible target: sid=%s", call_sid)
        return _safe_hangup(
            "I could not reach your agent right now, but they have your details "
            "and will call you back. Goodbye."
        )
    await record_forward_attempt(call_sid, reason=reason, outcome="requested")
    return _dial_agent(
        target["forward_e164"],
        caller_id=target["caller_id"],
        timeout_seconds=target["timeout_seconds"],
        say="Connecting you to your agent now, one moment.",
        action_url=_canonical_webhook_url(
            request,
            f"/api/telephony/webhooks/twilio/inbound/{endpoint_key}/handoff",
        ),
    )


@router.post(
    "/webhooks/twilio/inbound/{endpoint_key}/handoff",
    include_in_schema=False,
)
async def twilio_inbound_handoff_result(endpoint_key: str, request: Request) -> Response:
    """Record whether the agent actually picked up the transferred call."""
    suffix = f"/api/telephony/webhooks/twilio/inbound/{endpoint_key}/handoff"
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    account_sid = str(form.get("AccountSid") or "")
    route = await resolve_inbound_call_route(endpoint_key, call_sid, account_sid)
    if route is None:
        raise HTTPException(status_code=404, detail="Inbound call not found.")
    validate_twilio_signature(
        request, form, suffix, tokens=await _route_twilio_tokens(route)
    )

    dial_status = str(form.get("DialCallStatus") or "").strip().lower()
    outcome = {
        "completed": "connected",
        "answered": "connected",
        "no-answer": "no_answer",
        "busy": "busy",
        "failed": "failed",
        "canceled": "failed",
    }.get(dial_status, "failed")
    await record_forward_attempt(
        call_sid, reason="caller_request", outcome=outcome
    )
    if outcome == "connected":
        # Twilio continues the parent call after <Dial>; end it cleanly.
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )
    return _safe_hangup(
        "Your agent is not available right now. They have your details and will "
        "call you back. Goodbye."
    )


@router.post(
    "/webhooks/twilio/status/{endpoint_key}",
    include_in_schema=False,
)
async def twilio_inbound_status(endpoint_key: str, request: Request) -> Response:
    suffix = f"/api/telephony/webhooks/twilio/status/{endpoint_key}"
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    account_sid = str(form.get("AccountSid") or "")
    call_status = str(form.get("CallStatus") or "").strip().lower()
    route = await resolve_inbound_call_route(endpoint_key, call_sid, account_sid)
    if route is None:
        raise HTTPException(status_code=404, detail="Inbound call not found.")
    validate_twilio_signature(
        request,
        form,
        suffix,
        tokens=await _route_twilio_tokens(route),
    )

    await update_inbound_call_status(call_sid, call_status)
    if call_status in _TERMINAL_CALL_STATUSES:
        try:
            await cleanup_twilio_call(call_sid)
        except TwilioCallStateUnavailable:
            logger.warning("Distributed inbound call cleanup was unavailable: sid=%s", call_sid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
