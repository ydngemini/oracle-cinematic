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
import secrets
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
#: The media socket's path carries an opaque per-call reference: Twilio
#: forbids a query string in <Stream url> (errors 31920/31922), and the
#: handshake has nothing else to name the call by before it is accepted.
TWILIO_MEDIA_PATH = "/api/commands/media/twilio"
_MEDIA_REF_KEY_PREFIX = "twilio:media_ref:"
_MEDIA_REF_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")   # secrets.token_urlsafe(32)
_TWILIO_CREDENTIAL_FIELDS = ("account_sid", "auth_token", "api_key", "api_secret")


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
    correlation: str = "",
    keep_existing: bool = False,
) -> dict[str, Any]:
    """`keep_existing`: an early answer may already have bound this CallSid
    from the correlation (24.2) and opened the media socket; never reset it."""
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        raise ValueError("Twilio account SID is invalid")
    state = {
        **({"correlation": correlation} if correlation else {}),
        "tenant_id": str(tenant_id),
        "callee": str(callee_number),
        "account_sid": account_sid,
        "direction": "outbound",
        "stage": "queued",
        "created_at": time.time(),
        "qwen_realtime_enabled": twilio_qwen_enabled(),
    }
    if keep_existing:
        redis = await _get_redis()
        state["updated_at"] = time.time()
        written = await redis.set(_state_key(call_sid), json.dumps(state, separators=(",", ":"), sort_keys=True),
                                  ex=_STATE_TTL_SECONDS, nx=True)
        if not written:
            return await load_twilio_call_state(call_sid) or state
        return state
    await _save_call_state(call_sid, state)
    return state


#: Neoh's own reference for an outbound call, carried in the answer and status
#: URLs and stored BEFORE Twilio is asked to place the call (external audit
#: 24.2). Twilio signs the full URL, query included, so it cannot be forged
#: onto a webhook.
TWILIO_CORRELATION_PARAM = "neoh_ref"
_CORRELATION_KEY_PREFIX = "twilio:call_ref:"


def _correlation_key(correlation: str) -> Optional[str]:
    try:
        return f"{_CORRELATION_KEY_PREFIX}{uuid.UUID(str(correlation))}"
    except (TypeError, ValueError, AttributeError):
        return None


async def initialize_outbound_twilio_correlation(
    correlation: str,
    callee_number: str,
    *,
    tenant_id: str,
    account_sid: str,
    command_id: str = "",
) -> dict[str, Any]:
    """The call's state before the call exists: a webhook that beats the
    worker's CallSid write (a slow create response, a paused worker, or a
    create that timed out after Twilio placed the call) finds it by
    correlation instead of hanging up. `command_id` lets that webhook record
    the CallSid on the command, so an uncertain submission can be reconciled."""
    key = _correlation_key(correlation)
    if key is None:
        raise ValueError("Twilio correlation must be a UUID")
    if not _ACCOUNT_SID_RE.fullmatch(account_sid or ""):
        raise ValueError("Twilio account SID is invalid")
    state = {
        "tenant_id": _require_uuid(tenant_id, "tenant_id"),
        "callee": str(callee_number),
        "account_sid": account_sid,
        "direction": "outbound",
        "stage": "submitting",
        "correlation": str(uuid.UUID(str(correlation))),
        **({"command_id": _require_uuid(command_id, "command_id")} if command_id else {}),
        "created_at": time.time(),
        "qwen_realtime_enabled": twilio_qwen_enabled(),
    }
    redis = await _get_redis()
    state["updated_at"] = time.time()
    await redis.set(key, json.dumps(state, separators=(",", ":"), sort_keys=True),
                    ex=_STATE_TTL_SECONDS)
    return state


async def load_twilio_correlation(correlation: str) -> Optional[dict[str, Any]]:
    key = _correlation_key(correlation)
    if key is None:
        return None
    raw = await (await _get_redis()).get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("direction") != "outbound":
        return None
    return state


async def resolve_outbound_twilio_call_state(
    call_sid: str,
    correlation: str = "",
    *,
    wait_for_initialization: bool = False,
) -> Optional[dict[str, Any]]:
    """The outbound call's state by CallSid, else by the signed correlation —
    then bound to the CallSid so the media socket and later webhooks find it.

    With a correlation the CallSid is read once (no wait): the correlation
    state exists from before the call. Without one (a call placed before this
    change) the old bounded wait applies.
    """
    has_ref = _correlation_key(correlation) is not None and bool(_CALL_SID_RE.fullmatch(call_sid or ""))
    state = await load_twilio_call_state(
        call_sid, wait_for_initialization=wait_for_initialization and not has_ref)
    if state is not None or not has_ref:
        return state
    pending = await load_twilio_correlation(correlation)
    if pending is None:
        # The reference is unknown here (expired, or a store that lost it):
        # fall back to the old bounded wait for the worker's own write.
        return await load_twilio_call_state(call_sid, wait_for_initialization=wait_for_initialization)
    # SET NX: if the worker bound the CallSid meanwhile, keep its state.
    bound = {**pending, "stage": "queued", "via": "correlation", "updated_at": time.time()}
    redis = await _get_redis()
    await redis.set(_state_key(call_sid), json.dumps(bound, separators=(",", ":"), sort_keys=True),
                    ex=_STATE_TTL_SECONDS, nx=True)
    return await load_twilio_call_state(call_sid) or bound


async def find_outbound_twilio_call_state(
    call_sid: str,
    correlation: str = "",
    *,
    wait_for_initialization: bool = False,
) -> Optional[dict[str, Any]]:
    """Read only — it binds nothing, so it runs BEFORE a webhook's signature
    is checked, to choose the tokens that signature may be made with (23.2).

    The outbound call's state by CallSid, else by the pre-submission
    correlation — the same order resolve_outbound_twilio_call_state acts in,
    so the call whose account checks the signature is the call the webhook
    then acts on. None when neither names an outbound call, and when the
    CallSid is bound to a different reference than the one the request
    carries: a crossed reference resolves to neither call.
    """
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        return None
    has_ref = bool(correlation)
    if has_ref and _correlation_key(correlation) is None:
        return None
    state = await load_twilio_call_state(
        call_sid, wait_for_initialization=wait_for_initialization and not has_ref)
    if state is None and has_ref:
        pending = await load_twilio_correlation(correlation)
        if pending is not None:
            return pending
        # The reference is unknown here: the old bounded wait for the
        # worker's own CallSid write, as resolve_outbound_twilio_call_state.
        state = await load_twilio_call_state(call_sid, wait_for_initialization=wait_for_initialization)
    if state is None or state.get("direction") != "outbound":
        return None
    if has_ref and str(state.get("correlation") or "") != str(uuid.UUID(str(correlation))):
        return None
    return state


async def cleanup_twilio_correlation(correlation: str) -> None:
    key = _correlation_key(correlation)
    if key is not None:
        await (await _get_redis()).delete(key)


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


def platform_twilio_account_sid() -> str:
    return os.getenv("TWILIO_ACCOUNT_SID", "").strip()


def platform_twilio_auth_tokens() -> list[str]:
    """The platform account's signing tokens: the current one and the one
    being rotated out (TWILIO_AUTH_TOKEN_PREVIOUS, docs/credential-rotation.md)
    — the set telephony_api._twilio_auth_tokens accepts for inbound calls."""
    current = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    previous = os.getenv("TWILIO_AUTH_TOKEN_PREVIOUS", "").strip()
    return [token for token in dict.fromkeys((current, previous)) if token]


async def _stored_twilio_credential(tenant_id: str) -> dict[str, str]:
    """A brokerage's own Twilio credential (PUT /api/commands/providers/twilio)
    — the row the outbound rail places its calls with (_load_provider_credential,
    no label) and telephony_api._route_twilio_tokens reads for inbound ones.
    Only its own fields: never completed from the platform's environment."""
    from commands_api import _load_provider_credential
    from tenancy import Role, TenantContext

    ctx = TenantContext(
        agent_id="twilio-call-credential",
        tenant_id=_require_uuid(tenant_id, "tenant_id"),
        role=Role.AGENT,
    )
    raw = await _load_provider_credential(ctx, "twilio")
    if not raw:
        raise TwilioCallStateUnavailable("the brokerage has no Twilio credential")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise TwilioCallStateUnavailable("the stored Twilio credential is invalid") from exc
    if not isinstance(decoded, dict):
        raise TwilioCallStateUnavailable("the stored Twilio credential is invalid")
    return {key: str(decoded.get(key) or "").strip() for key in _TWILIO_CREDENTIAL_FIELDS}


async def _call_account_credentials(state: Mapping[str, Any]) -> tuple[dict[str, str], bool]:
    """The credential of the Twilio account a call's own state names — state
    Neoh wrote before the call was placed (or when it was bound) — and whether
    that account is the platform's (external audit 23.2/23.3).

    The platform account: the environment's credential. Any other account:
    that brokerage's stored credential, which must be for the same account.
    Nothing else: a brokerage-account call is never given the platform's
    credential, nor one brokerage's call another's. Raises
    TwilioCallStateUnavailable when no such credential exists.
    """
    account_sid = str(state.get("account_sid") or "")
    if not _ACCOUNT_SID_RE.fullmatch(account_sid):
        raise TwilioCallStateUnavailable("the call state names no Twilio account")
    platform_sid = platform_twilio_account_sid()
    if platform_sid and account_sid == platform_sid:
        return {
            "account_sid": platform_sid,
            "auth_token": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            "api_key": os.getenv("TWILIO_API_KEY", "").strip(),
            "api_secret": os.getenv("TWILIO_API_SECRET", "").strip(),
        }, True
    tenant_id = str(state.get("tenant_id") or "")
    if not tenant_id:
        raise TwilioCallStateUnavailable("the call state names no brokerage")
    try:
        stored = await _stored_twilio_credential(tenant_id)
    except TwilioCallStateUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable is not "use another account's"
        raise TwilioCallStateUnavailable(
            f"the brokerage's Twilio credential is unreadable ({type(exc).__name__})") from exc
    if stored["account_sid"] != account_sid:
        raise TwilioCallStateUnavailable(
            "the brokerage's Twilio credential is not for the account that carries the call")
    return stored, False


async def twilio_signing_tokens(state: Mapping[str, Any]) -> list[str]:
    """The tokens a webhook or media handshake for this call may be signed
    with: the platform's current and previous token when the call's account
    is the platform's, else only that brokerage's own token. Raises
    TwilioCallStateUnavailable when none can be chosen (the caller refuses)."""
    credentials, platform = await _call_account_credentials(state)
    tokens = platform_twilio_auth_tokens() if platform else [credentials["auth_token"]]
    tokens = [token for token in dict.fromkeys(tokens) if token]
    if not tokens:
        raise TwilioCallStateUnavailable("no auth token is configured for the call's Twilio account")
    return tokens


async def credentials_for_call(call_sid: str) -> dict[str, str]:
    """The REST credential for acting on a live call (the agent hand-off, the
    refusal line): the whole credential of the account the call's own state
    names. Raises TwilioCallStateUnavailable — never falls back to the
    platform's credential — when the state, or a credential for exactly that
    account, is missing (external audit 23.3)."""
    try:
        state = await load_twilio_call_state(call_sid)
        if state is None:
            raise TwilioCallStateUnavailable("Twilio call state is missing.")
        credentials, _platform = await _call_account_credentials(state)
    except Exception as exc:  # noqa: BLE001 - logged, then refused
        reason = str(exc) if isinstance(exc, TwilioCallStateUnavailable) else type(exc).__name__
        logger.warning("transfer_credentials_unavailable: sid=%s reason=%s", call_sid, reason)
        if isinstance(exc, TwilioCallStateUnavailable):
            raise
        raise TwilioCallStateUnavailable("Twilio call credentials are unavailable.") from exc
    return credentials


async def twilio_redirect_call(
    call_sid: str,
    twiml_url: str,
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> None:
    """Redirect a live Twilio call to new TwiML — the live agent hand-off.

    Passing a URL rather than inline TwiML keeps the destination number out of
    this process and the resulting request signature-validated like every other
    Twilio webhook.

    `credentials` are used whole or not at all: never completed field by field
    from the environment, which could pair a brokerage's account SID with the
    platform's token (23.3). Without them, the call's own account is resolved
    (credentials_for_call), never assumed to be the platform's.
    """
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    parsed = urllib.parse.urlsplit(twiml_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Twilio redirect URL must be absolute HTTP(S)")

    from command_providers import _twilio_client, _twilio_credential_error

    if credentials is None:
        credentials = await credentials_for_call(call_sid)
    account_sid, auth_token, api_key, api_secret = (
        str(credentials.get(key) or "").strip() for key in _TWILIO_CREDENTIAL_FIELDS
    )
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


def _media_ref_key(media_ref: str) -> Optional[str]:
    if not isinstance(media_ref, str) or not _MEDIA_REF_RE.fullmatch(media_ref):
        return None
    return f"{_MEDIA_REF_KEY_PREFIX}{media_ref}"


def twilio_media_websocket_url(media_ref: str) -> str:
    """The exact WSS URL Twilio is given for one call's media stream — and
    signs the handshake over: the per-call reference rides in the path."""
    if _media_ref_key(media_ref) is None:
        raise ValueError("Twilio media reference is invalid")
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
        (scheme, parsed.netloc, f"{TWILIO_MEDIA_PATH}/{media_ref}", "", "")
    )


async def create_twilio_media_stream_url(call_sid: str) -> str:
    """The <Stream url> for one call: a fresh opaque reference, bound to the
    call's state (and only the latest one is live), that the media socket's
    handshake is resolved by before it is accepted. Expires with the bridge
    token. Raises TwilioCallStateUnavailable when it cannot be stored."""
    if not _CALL_SID_RE.fullmatch(call_sid or ""):
        raise ValueError("Twilio call SID is invalid")
    media_ref = secrets.token_urlsafe(32)
    url = twilio_media_websocket_url(media_ref)
    try:
        state = await load_twilio_call_state(call_sid)
        if state is None:
            raise TwilioCallStateUnavailable("Twilio call state is missing.")
        redis = await _get_redis()
        await redis.set(_media_ref_key(media_ref), call_sid, ex=_BRIDGE_TOKEN_TTL_SECONDS)
        state["media_ref"] = media_ref
        await _save_call_state(call_sid, state)
    except TwilioCallStateUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - the caller falls back, never streams unbound
        raise TwilioCallStateUnavailable("Twilio media binding could not be stored.") from exc
    return url


async def authenticate_twilio_media_handshake(media_ref: str, signature: str) -> Optional[str]:
    """Before a media socket is accepted: the CallSid it may serve, or None to
    refuse it (4403).

    The path's reference must be live and still the one its call's state
    carries; the signature must verify over the exact URL Twilio was given,
    under the tokens of the account that call's own state names — the
    platform's current or previous token for a platform-account call, the
    brokerage's own stored token for a call on its account (23.2). The start
    frame is checked after this (authorize_twilio_media); it is never
    authority on its own.
    """
    key = _media_ref_key(media_ref)
    if key is None or not signature:
        return None
    raw = await (await _get_redis()).get(key)
    if raw is None:
        return None
    call_sid = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    state = await load_twilio_call_state(call_sid)
    if state is None or not hmac.compare_digest(
        str(state.get("media_ref") or "").encode("utf-8"), media_ref.encode("utf-8")
    ):
        return None
    try:
        tokens = await twilio_signing_tokens(state)
    except TwilioCallStateUnavailable as exc:
        logger.warning("Twilio media handshake refused: sid=%s reason=%s", call_sid, exc)
        return None
    from twilio.request_validator import RequestValidator

    url = twilio_media_websocket_url(media_ref)
    if any(RequestValidator(token).validate(url, {}, signature) for token in tokens):
        return call_sid
    logger.warning("Twilio media handshake signature is invalid: sid=%s", call_sid)
    return None


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
