"""Distributed state and authentication for Plivo/Qwen realtime calls.

Mirrors twilio_call_handler.py's shape and invariants exactly (see that
module's docstring), generalized to Plivo's call identity: Plivo call UUIDs
are standard UUIDs, not Twilio's ``CAxxxxxxxx...`` CallSid, and Plivo's Auth
ID (not an "Account SID") is the account identity. Plivo REST credentials
never enter Redis, same as Twilio's.

Redis keys use the generic ``voice:call_state:<provider>:<provider_call_id>``
shape the migration/PR notes describe; this module owns the ``plivo`` half.
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
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.plivo_realtime")

_CALL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_STATE_KEY_PREFIX = "voice:call_state:plivo:"
_STATE_TTL_SECONDS = 3_600
_STATE_WAIT_ATTEMPTS = 20
_STATE_WAIT_SECONDS = 0.25
_BRIDGE_TOKEN_TTL_SECONDS = 300


class PlivoCallStateUnavailable(RuntimeError):
    """Raised when a Plivo call cannot be managed safely across replicas."""


def plivo_qwen_enabled(state: Optional[Mapping[str, Any]] = None) -> bool:
    if state is not None and state.get("qwen_realtime_enabled") is False:
        return False
    raw = os.getenv("ORACLE_PLIVO_QWEN_REALTIME_ENABLED", "true")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _get_redis() -> Any:
    from rate_limit_middleware import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        raise PlivoCallStateUnavailable(
            "Distributed Plivo call state is unavailable; configure REDIS_URL."
        )
    return redis


async def ensure_plivo_call_state_available() -> None:
    redis = await _get_redis()
    try:
        if not await redis.ping():
            raise PlivoCallStateUnavailable("Distributed Plivo call state is unavailable.")
    except PlivoCallStateUnavailable:
        raise
    except Exception as exc:
        raise PlivoCallStateUnavailable(
            "Distributed Plivo call state is unavailable."
        ) from exc


def _state_key(call_uuid: str) -> str:
    return f"{_STATE_KEY_PREFIX}{call_uuid}"


async def _save_call_state(call_uuid: str, state: dict[str, Any]) -> None:
    redis = await _get_redis()
    state["updated_at"] = time.time()
    await redis.set(
        _state_key(call_uuid),
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        ex=_STATE_TTL_SECONDS,
    )


async def load_plivo_call_state(
    call_uuid: str,
    *,
    wait_for_initialization: bool = False,
) -> Optional[dict[str, Any]]:
    if not _CALL_UUID_RE.fullmatch(call_uuid or ""):
        return None
    redis = await _get_redis()
    attempts = _STATE_WAIT_ATTEMPTS if wait_for_initialization else 1
    raw: Any = None
    for attempt in range(attempts):
        raw = await redis.get(_state_key(call_uuid))
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
        await redis.delete(_state_key(call_uuid))
        return None
    if not isinstance(state, dict):
        await redis.delete(_state_key(call_uuid))
        return None
    return state


def _require_uuid(value: str, label: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


async def initialize_inbound_plivo_call_state(
    call_uuid: str,
    callee_number: str,
    *,
    tenant_id: str,
    agent_id: str,
    account_id: str,
    route_id: str,
    voice_call_id: str,
    intake_mode: str,
    contact_id: Optional[str] = None,
    client_id: Optional[str] = None,
    forward_available: bool = False,
) -> dict[str, Any]:
    """Bind a signed inbound Plivo call to its resolved tenant and agent.

    Same PII-minimization rule as Twilio's counterpart: caller phone numbers
    and transcript content stay out of Redis entirely.
    """
    if not _CALL_UUID_RE.fullmatch(call_uuid or ""):
        raise ValueError("Plivo call UUID is invalid")
    if not str(account_id or "").strip():
        raise ValueError("Plivo account id is invalid")
    if not str(agent_id or "").strip() or len(str(agent_id)) > 128:
        raise ValueError("agent_id is invalid")
    if intake_mode not in {"buyer", "seller", "auto"}:
        raise ValueError("intake_mode must be buyer, seller, or auto")

    state: dict[str, Any] = {
        "provider": "plivo",
        "tenant_id": _require_uuid(tenant_id, "tenant_id"),
        "agent_id": str(agent_id).strip(),
        "callee": str(callee_number),
        "account_id": str(account_id).strip(),
        "route_id": _require_uuid(route_id, "route_id"),
        "voice_call_id": _require_uuid(voice_call_id, "voice_call_id"),
        "direction": "inbound",
        "intake_mode": intake_mode,
        "stage": "disclosed",
        "created_at": time.time(),
        "qwen_realtime_enabled": plivo_qwen_enabled(),
        "forward_available": bool(forward_available),
    }
    if contact_id:
        state["contact_id"] = _require_uuid(contact_id, "contact_id")
    if client_id:
        state["client_id"] = _require_uuid(client_id, "client_id")
    await _save_call_state(call_uuid, state)
    return state


async def initialize_outbound_plivo_call_state(
    call_uuid: str,
    callee_number: str,
    *,
    tenant_id: str,
    account_id: str,
) -> dict[str, Any]:
    if not _CALL_UUID_RE.fullmatch(call_uuid or ""):
        raise ValueError("Plivo call UUID is invalid")
    state = {
        "provider": "plivo",
        "tenant_id": str(tenant_id),
        "callee": str(callee_number),
        "account_id": str(account_id),
        "direction": "outbound",
        "stage": "queued",
        "created_at": time.time(),
        "qwen_realtime_enabled": plivo_qwen_enabled(),
    }
    await _save_call_state(call_uuid, state)
    return state


async def mark_plivo_streaming(call_uuid: str) -> None:
    state = await load_plivo_call_state(call_uuid)
    if state is None:
        raise PlivoCallStateUnavailable("Plivo call state is missing.")
    state["stage"] = "qwen_streaming"
    await _save_call_state(call_uuid, state)


async def cleanup_plivo_call(call_uuid: str) -> None:
    if not _CALL_UUID_RE.fullmatch(call_uuid or ""):
        return
    redis = await _get_redis()
    await redis.delete(_state_key(call_uuid))


def plivo_media_websocket_url() -> str:
    base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PlivoCallStateUnavailable(
            "ORACLE_PUBLIC_BASE_URL must be an absolute HTTP(S) URL."
        )
    scheme = "wss" if parsed.scheme == "https" else "ws"
    if scheme != "wss" and os.getenv("ORACLE_ENV", "").lower() not in {
        "dev",
        "development",
        "local",
        "test",
    }:
        raise PlivoCallStateUnavailable("Plivo AudioStream requires WSS outside development.")
    return urllib.parse.urlunsplit(
        (scheme, parsed.netloc, "/api/commands/media/plivo", "", "")
    )


def validate_plivo_webhook_signature(
    url: str,
    nonce: str,
    signature: str,
    *,
    tokens: list[str],
    method: str = "POST",
    params: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Plivo's V3 signature validation (X-Plivo-Signature-V3 / -Nonce).

    Uses ``plivo.utils.validate_v3_signature`` directly — Plivo's own
    maintained helper, confirmed against the installed SDK's source
    (plivo/utils/signature_v3.py) — rather than reimplementing the HMAC,
    matching "do not invent your own signature algorithm" from the
    migration brief. V3 (over V2) per the same brief's preference for the
    current recommended mechanism when the SDK exposes one.
    """
    if not signature or not nonce:
        return False
    try:
        import plivo.utils as plivo_utils
    except Exception:
        logger.exception("Plivo signature validator import failed")
        return False
    string_params = {
        str(key): str(value) for key, value in dict(params or {}).items()
    }
    for token in tokens:
        if not token:
            continue
        try:
            if plivo_utils.validate_v3_signature(
                method, url, nonce, token, signature, params=string_params
            ):
                return True
        except Exception:
            continue
    return False


def _bridge_signing_key() -> bytes:
    master_key = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        raise PlivoCallStateUnavailable("Plivo media binding is not configured.")
    return hashlib.sha256(
        b"oracle:plivo-media-bridge:v1\x00" + master_key.encode("utf-8")
    ).digest()


def create_plivo_bridge_token(call_uuid: str, *, now: Optional[int] = None) -> str:
    """Self-issued capability token authenticating the media WebSocket.

    Plivo's own webhook signature scheme (X-Plivo-Signature-V2) is not
    confirmed to extend to the AudioStream WSS handshake itself, so — like
    Twilio's HMAC bridge token this mirrors — Neoh mints and verifies its
    own short-lived, call-bound token instead of trusting an unconfirmed
    provider signature for the socket upgrade. The signed PlivoXML <Stream>
    URL carries this token as a query parameter (see
    voice_provider.PlivoVoiceProvider.speak_and_stream_markup).
    """
    if not _CALL_UUID_RE.fullmatch(call_uuid or ""):
        raise ValueError("Plivo call UUID is invalid")
    expires_at = int(now if now is not None else time.time()) + _BRIDGE_TOKEN_TTL_SECONDS
    payload = f"v1.{call_uuid}.{expires_at}"
    signature = hmac.new(
        _bridge_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_plivo_bridge_token(
    token: str, call_uuid: str, *, now: Optional[int] = None
) -> bool:
    if not token or not _CALL_UUID_RE.fullmatch(call_uuid or ""):
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    version, token_call_uuid, raw_expires_at, supplied_signature = parts
    if version != "v1" or token_call_uuid != call_uuid:
        return False
    try:
        expires_at = int(raw_expires_at)
    except ValueError:
        return False
    current_time = int(now if now is not None else time.time())
    if expires_at < current_time or expires_at > current_time + _BRIDGE_TOKEN_TTL_SECONDS:
        return False
    payload = f"{version}.{token_call_uuid}.{expires_at}"
    expected_signature = hmac.new(
        _bridge_signing_key(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, supplied_signature)


async def authorize_plivo_media(call_uuid: str, bridge_token: str) -> bool:
    state = await load_plivo_call_state(call_uuid, wait_for_initialization=True)
    if state is None or not plivo_qwen_enabled(state):
        return False
    return verify_plivo_bridge_token(bridge_token, call_uuid)
