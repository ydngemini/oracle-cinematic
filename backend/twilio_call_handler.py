"""Distributed state and authentication for Twilio/Qwen realtime calls.

Twilio REST credentials never enter Redis. The short-lived state binds a
Twilio-signed WebSocket handshake and start frame to a call that passed the
CRM approval/compliance pipeline before the Qwen bridge is allowed to open.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.twilio_realtime")

_CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
_STATE_KEY_PREFIX = "twilio:call_state:"
_STATE_TTL_SECONDS = 3_600
_STATE_WAIT_ATTEMPTS = 20
_STATE_WAIT_SECONDS = 0.25
_BRIDGE_TOKEN_TTL_SECONDS = 300


class TwilioCallStateUnavailable(RuntimeError):
    """Raised when a Twilio call cannot be managed safely across replicas."""


def twilio_qwen_enabled(state: Optional[Mapping[str, Any]] = None) -> bool:
    if state is not None and state.get("qwen_realtime_enabled") is False:
        return False
    raw = os.getenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "false")
    requested = raw.strip().lower() in {"1", "true", "yes", "on"}
    account_tier = os.getenv("ORACLE_TWILIO_ACCOUNT_TIER", "").strip().lower()
    # Twilio's current Voice Trial explicitly strips the <Stream> verb. Keep
    # the supported <Gather> conversation path active until the account is
    # upgraded instead of presenting a realtime bridge that cannot connect.
    return requested and account_tier not in {"trial", "free-trial"}


async def _get_redis() -> Any:
    from rate_limit_middleware import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        raise TwilioCallStateUnavailable(
            "Distributed Twilio call state is unavailable; configure REDIS_URL."
        )
    return redis


async def ensure_twilio_call_state_available() -> None:
    redis = await _get_redis()
    try:
        if not await redis.ping():
            raise TwilioCallStateUnavailable(
                "Distributed Twilio call state is unavailable."
            )
    except TwilioCallStateUnavailable:
        raise
    except Exception as exc:
        raise TwilioCallStateUnavailable(
            "Distributed Twilio call state is unavailable."
        ) from exc


def _state_key(call_sid: str) -> str:
    return f"{_STATE_KEY_PREFIX}{call_sid}"


async def _save_call_state(call_sid: str, state: dict[str, Any]) -> None:
    redis = await _get_redis()
    state["updated_at"] = time.time()
    await redis.set(
        _state_key(call_sid),
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        ex=_STATE_TTL_SECONDS,
    )


async def load_twilio_call_state(
    call_sid: str,
    *,
    wait_for_initialization: bool = False,
) -> Optional[dict[str, Any]]:
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return None
    redis = await _get_redis()
    attempts = _STATE_WAIT_ATTEMPTS if wait_for_initialization else 1
    raw: Any = None
    for attempt in range(attempts):
        raw = await redis.get(_state_key(call_sid))
        if raw is not None:
            break
        if attempt + 1 < attempts:
            await asyncio.sleep(_STATE_WAIT_SECONDS)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        await redis.delete(_state_key(call_sid))
        return None
    if not isinstance(state, dict):
        await redis.delete(_state_key(call_sid))
        return None
    return state


async def initialize_twilio_call_state(
    call_sid: str,
    callee_number: str,
    *,
    tenant_id: str,
    account_sid: str,
) -> dict[str, Any]:
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        raise ValueError("Twilio account SID is invalid")
    state = {
        "tenant_id": str(tenant_id),
        "callee": str(callee_number),
        "account_sid": account_sid,
        "direction": "outbound",
        "stage": "queued",
        "created_at": time.time(),
        "qwen_realtime_enabled": twilio_qwen_enabled(),
    }
    await _save_call_state(call_sid, state)
    return state


def _require_uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


async def initialize_inbound_twilio_call_state(
    call_sid: str,
    callee_number: str,
    *,
    tenant_id: str,
    agent_id: str,
    account_sid: str,
    route_id: str,
    voice_call_id: str,
    intake_mode: str,
    contact_id: Optional[str] = None,
    client_id: Optional[str] = None,
    forward_available: bool = False,
) -> dict[str, Any]:
    """Bind a signed inbound Twilio call to its resolved tenant and agent.

    Caller phone numbers and transcript content deliberately stay out of Redis.
    They are persisted only in the tenant-encrypted call record.  The short-lived
    state contains the minimum identifiers needed to authorize the media stream
    and select the bounded intake persona.
    """
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        raise ValueError("Twilio account SID is invalid")
    if not str(agent_id or "").strip() or len(str(agent_id)) > 128:
        raise ValueError("agent_id is invalid")
    if intake_mode not in {"buyer", "seller", "auto"}:
        raise ValueError("intake_mode must be buyer, seller, or auto")

    state: dict[str, Any] = {
        "tenant_id": _require_uuid(tenant_id, "tenant_id"),
        "agent_id": str(agent_id).strip(),
        "callee": str(callee_number),
        "account_sid": account_sid,
        "route_id": _require_uuid(route_id, "route_id"),
        "voice_call_id": _require_uuid(voice_call_id, "voice_call_id"),
        "direction": "inbound",
        "intake_mode": intake_mode,
        "stage": "disclosed",
        "created_at": time.time(),
        "qwen_realtime_enabled": twilio_qwen_enabled(),
        # Only whether a hand-off is possible — the agent's own number stays in
        # the database, consistent with the caller-PII rule above.
        "forward_available": bool(forward_available),
    }
    if contact_id:
        state["contact_id"] = _require_uuid(contact_id, "contact_id")
    if client_id:
        state["client_id"] = _require_uuid(client_id, "client_id")
    await _save_call_state(call_sid, state)
    return state


async def mark_twilio_streaming(call_sid: str) -> None:
    state = await load_twilio_call_state(call_sid)
    if state is None:
        raise TwilioCallStateUnavailable("Twilio call state is missing.")
    state["stage"] = "qwen_streaming"
    await _save_call_state(call_sid, state)


async def twilio_redirect_call(call_sid: str, twiml_url: str) -> None:
    """Redirect a live Twilio call to new TwiML — the live agent hand-off.

    Passing a URL rather than inline TwiML keeps the destination number out of
    this process and the resulting request signature-validated like every other
    Twilio webhook.
    """
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    parsed = urllib.parse.urlsplit(twiml_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Twilio redirect URL must be absolute HTTP(S)")

    from command_providers import _twilio_client, _twilio_credential_error

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    api_key = os.getenv("TWILIO_API_KEY", "").strip()
    api_secret = os.getenv("TWILIO_API_SECRET", "").strip()
    error = _twilio_credential_error(account_sid, auth_token, api_key, api_secret)
    if error:
        raise TwilioCallStateUnavailable(f"Cannot redirect call: {error}")

    def _redirect() -> None:
        client = _twilio_client(account_sid, auth_token, api_key, api_secret)
        client.calls(call_sid).update(url=twiml_url, method="POST")

    await asyncio.wait_for(asyncio.to_thread(_redirect), timeout=15.0)


async def cleanup_twilio_call(call_sid: str) -> None:
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return
    redis = await _get_redis()
    await redis.delete(_state_key(call_sid))


def twilio_media_websocket_url() -> str:
    base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TwilioCallStateUnavailable(
            "ORACLE_PUBLIC_BASE_URL must be an absolute HTTP(S) URL."
        )
    scheme = "wss" if parsed.scheme == "https" else "ws"
    if scheme != "wss" and os.getenv("ORACLE_ENV", "").lower() not in {
        "dev",
        "development",
        "local",
        "test",
    }:
        raise TwilioCallStateUnavailable(
            "Twilio Media Streams require WSS outside development."
        )
    return urllib.parse.urlunsplit(
        (scheme, parsed.netloc, "/api/commands/media/twilio", "", "")
    )


def verify_twilio_websocket_signature(signature: str) -> bool:
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator

        return bool(
            RequestValidator(auth_token).validate(
                twilio_media_websocket_url(),
                {},
                signature,
            )
        )
    except Exception:
        logger.exception("Twilio WebSocket signature validation failed")
        return False


def _bridge_signing_key() -> bytes:
    master_key = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        raise TwilioCallStateUnavailable(
            "Twilio media binding is not configured."
        )
    return hashlib.sha256(
        b"oracle:twilio-media-bridge:v1\x00" + master_key.encode("utf-8")
    ).digest()


def create_twilio_bridge_token(
    call_sid: str,
    *,
    now: Optional[int] = None,
) -> str:
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    expires_at = int(now if now is not None else time.time()) + _BRIDGE_TOKEN_TTL_SECONDS
    payload = f"v1.{call_sid}.{expires_at}"
    signature = hmac.new(
        _bridge_signing_key(),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_twilio_bridge_token(
    token: str,
    call_sid: str,
    *,
    now: Optional[int] = None,
) -> bool:
    if not token or not _CALL_SID_RE.fullmatch(call_sid or ""):
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    version, token_call_sid, raw_expires_at, supplied_signature = parts
    if version != "v1" or token_call_sid != call_sid:
        return False
    try:
        expires_at = int(raw_expires_at)
    except ValueError:
        return False
    current_time = int(now if now is not None else time.time())
    if expires_at < current_time or expires_at > current_time + _BRIDGE_TOKEN_TTL_SECONDS:
        return False
    payload = f"{version}.{token_call_sid}.{expires_at}"
    expected_signature = hmac.new(
        _bridge_signing_key(),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, supplied_signature)


async def authorize_twilio_media(
    call_sid: str,
    account_sid: str,
    bridge_token: str,
) -> bool:
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        return False
    state = await load_twilio_call_state(
        call_sid,
        wait_for_initialization=True,
    )
    if state is None or not twilio_qwen_enabled(state):
        return False
    if not hmac.compare_digest(str(state.get("account_sid") or ""), account_sid):
        return False
    return verify_twilio_bridge_token(bridge_token, call_sid)
