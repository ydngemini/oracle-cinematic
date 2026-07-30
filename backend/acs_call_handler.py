"""Distributed ACS call handling for inbound and outbound conversations.

The default call path uses the media callback cycle:
PlayCompleted -> RecognizeCompleted -> AI response -> Play.

When ORACLE_QWEN_REALTIME_ENABLED is on, the disclosure greeting remains an
ACS TextSource operation, then PlayCompleted starts a bidirectional 16 kHz PCM
stream through qwen_omni_realtime.py. If that bridge fails, the call falls back
to the default recognition path.

Call state and the tenant-scoped ACS connection details are stored in Redis
with a bounded TTL. This allows any backend replica to service a callback after
a deployment, restart, or load-balancer hop without falling back to process
memory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("oracle.acs_inbound")

_MAX_TURNS = 20
_MAX_NO_INPUT_RETRIES = 2
_END_SILENCE_TIMEOUT_SEC = 2
_VOICE = os.getenv("ACS_VOICE_NAME", "en-US-AriaNeural")
_STALE_CALL_TTL_SECONDS = int(os.getenv("ACS_STALE_CALL_TTL", "3600"))
_STATE_KEY_PREFIX = "acs:call_state:"
_LOCK_KEY_PREFIX = "acs:call_lock:"
_STATE_WAIT_ATTEMPTS = 20
_STATE_WAIT_SECONDS = 0.25


class ACSCallStateUnavailable(RuntimeError):
    """Raised when durable ACS state cannot be read or written."""


def _state_key(call_connection_id: str) -> str:
    return f"{_STATE_KEY_PREFIX}{call_connection_id}"


def _lock_key(call_connection_id: str) -> str:
    return f"{_LOCK_KEY_PREFIX}{call_connection_id}"


async def _get_redis() -> Any:
    """Reuse the process Redis connection initialized by the app lifespan."""
    from rate_limit_middleware import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        raise ACSCallStateUnavailable(
            "Distributed ACS call state is unavailable; configure REDIS_URL."
        )
    return redis


async def ensure_call_state_available() -> None:
    """Fail closed before creating a call that requires callback state."""
    redis = await _get_redis()
    try:
        if not await redis.ping():
            raise ACSCallStateUnavailable("Distributed ACS call state is unavailable.")
    except ACSCallStateUnavailable:
        raise
    except Exception as exc:
        raise ACSCallStateUnavailable(
            "Distributed ACS call state is unavailable."
        ) from exc


def _resolve_acs_credentials(
    credentials: Optional[Mapping[str, Any]] = None,
) -> dict[str, str]:
    supplied = dict(credentials or {})
    connection_string = str(
        supplied.get("connection_string")
        or os.getenv("ACS_CONNECTION_STRING", "")
    ).strip()
    from_number = str(
        supplied.get("from_number")
        or os.getenv("ACS_FROM_NUMBER", "")
    ).strip()
    if not connection_string:
        raise RuntimeError("ACS connection string is not configured")
    if not from_number:
        raise RuntimeError("ACS from-number is not configured")
    return {
        "connection_string": connection_string,
        "from_number": from_number,
    }


def _state_cipher() -> Fernet:
    """Derive a dedicated authenticated-encryption key for short-lived state."""
    master_key = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        raise ACSCallStateUnavailable(
            "ACS call-state credential encryption is not configured."
        )
    digest = hashlib.sha256(
        b"oracle:acs-call-state:v1\x00" + master_key.encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal_acs_credentials(
    credentials: Mapping[str, Any],
    tenant_id: str,
) -> str:
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "credentials": _resolve_acs_credentials(credentials),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _state_cipher().encrypt(payload).decode("ascii")


def _credentials_from_state(state: Mapping[str, Any]) -> dict[str, str]:
    # Raw credentials are accepted only for the short-lived in-process state
    # needed to answer/place a call. _save_call_state always seals and removes
    # this field before writing Redis.
    transient = state.get("acs_credentials")
    if isinstance(transient, Mapping):
        return _resolve_acs_credentials(transient)

    sealed = str(state.get("acs_credentials_ciphertext") or "")
    tenant_id = str(state.get("tenant_id") or "")
    if not sealed or not tenant_id:
        raise ACSCallStateUnavailable("ACS call state has no tenant credentials.")
    try:
        payload = json.loads(
            _state_cipher().decrypt(sealed.encode("ascii")).decode("utf-8")
        )
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ACSCallStateUnavailable(
            "ACS call-state credentials failed authentication."
        ) from exc
    if not isinstance(payload, dict) or payload.get("tenant_id") != tenant_id:
        raise ACSCallStateUnavailable(
            "ACS call-state credentials do not match the tenant."
        )
    credentials = payload.get("credentials")
    if not isinstance(credentials, Mapping):
        raise ACSCallStateUnavailable("ACS call state has no tenant credentials.")
    return _resolve_acs_credentials(credentials)


def _platform_tenant_id() -> str:
    return os.getenv(
        "ORACLE_PLATFORM_TENANT_ID",
        "00000000-0000-0000-0000-000000000000",
    )


async def _save_call_state(
    call_connection_id: str,
    state: dict[str, Any],
) -> None:
    tenant_id = str(state.get("tenant_id") or "")
    transient_credentials = state.pop("acs_credentials", None)
    if isinstance(transient_credentials, Mapping):
        state["acs_credentials_ciphertext"] = _seal_acs_credentials(
            transient_credentials,
            tenant_id,
        )
    if not state.get("acs_credentials_ciphertext"):
        raise ACSCallStateUnavailable("ACS call state has no tenant credentials.")
    redis = await _get_redis()
    state["updated_at"] = time.time()
    await redis.set(
        _state_key(call_connection_id),
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        ex=_STALE_CALL_TTL_SECONDS,
    )


async def _load_call_state(
    call_connection_id: str,
    *,
    wait_for_initialization: bool = False,
) -> Optional[dict[str, Any]]:
    redis = await _get_redis()
    attempts = _STATE_WAIT_ATTEMPTS if wait_for_initialization else 1
    raw: Any = None
    for attempt in range(attempts):
        raw = await redis.get(_state_key(call_connection_id))
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
        logger.error("Invalid Redis ACS state removed: cid=%s", call_connection_id)
        await redis.delete(_state_key(call_connection_id))
        return None
    if not isinstance(state, dict):
        logger.error("Non-object Redis ACS state removed: cid=%s", call_connection_id)
        await redis.delete(_state_key(call_connection_id))
        return None
    return state


async def call_state_exists(call_connection_id: str) -> bool:
    redis = await _get_redis()
    return bool(await redis.exists(_state_key(call_connection_id)))


async def initialize_call_state(
    call_connection_id: str,
    caller_number: str,
    *,
    tenant_id: Optional[str] = None,
    credentials: Optional[Mapping[str, Any]] = None,
    direction: str = "outbound",
) -> dict[str, Any]:
    """Persist all callback-routing context immediately after call creation."""
    if not call_connection_id:
        raise ValueError("call_connection_id is required")
    if direction not in {"inbound", "outbound"}:
        raise ValueError("direction must be inbound or outbound")
    state = {
        "caller": str(caller_number or ""),
        "direction": direction,
        "stage": "answering",
        "turns": 0,
        "no_input_count": 0,
        "created_at": time.time(),
        "tenant_id": str(tenant_id or _platform_tenant_id()),
        "acs_credentials": _resolve_acs_credentials(credentials),
    }
    await _save_call_state(call_connection_id, state)
    return state


@asynccontextmanager
async def _call_lock(call_connection_id: str) -> AsyncIterator[None]:
    """Serialize callbacks for a call across every backend replica."""
    redis = await _get_redis()
    lock = redis.lock(
        _lock_key(call_connection_id),
        timeout=30,
        blocking_timeout=5,
    )
    async with lock:
        yield


def _get_client(state: Mapping[str, Any]) -> Any:
    from azure.communication.callautomation import CallAutomationClient

    credentials = _credentials_from_state(state)
    connection_string = str(credentials.get("connection_string") or "").strip()
    if not connection_string:
        raise ACSCallStateUnavailable("ACS call state has no connection string.")
    return CallAutomationClient.from_connection_string(connection_string)


def _get_callback_url() -> str:
    base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError(
            "ORACLE_PUBLIC_BASE_URL is not set - absolute callback URL required for ACS"
        )
    secret = os.getenv("ORACLE_ACS_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("ORACLE_ACS_WEBHOOK_SECRET is not configured")
    return (
        f"{base}/api/commands/webhooks/acs"
        f"?token={urllib.parse.quote(secret, safe='')}"
    )


def qwen_realtime_enabled(state: Optional[Mapping[str, Any]] = None) -> bool:
    raw = os.getenv("ORACLE_QWEN_REALTIME_ENABLED", "")
    enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
    return enabled and not bool((state or {}).get("qwen_realtime_failed"))


def _get_media_streaming_url() -> str:
    base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError(
            "ORACLE_PUBLIC_BASE_URL is not set - absolute media URL required for ACS"
        )
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("ORACLE_PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    secret = os.getenv("ORACLE_ACS_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("ORACLE_ACS_WEBHOOK_SECRET is not configured")
    return urllib.parse.urlunsplit(
        (
            scheme,
            parsed.netloc,
            "/api/commands/media/acs",
            urllib.parse.urlencode({"token": secret}),
            "",
        )
    )


def build_qwen_media_streaming_options() -> Any:
    """Build an inactive bidirectional stream; disclosure plays before start."""
    if not qwen_realtime_enabled():
        return None
    from azure.communication.callautomation import (
        AudioFormat,
        MediaStreamingAudioChannelType,
        MediaStreamingContentType,
        MediaStreamingOptions,
        StreamingTransportType,
    )

    return MediaStreamingOptions(
        transport_url=_get_media_streaming_url(),
        transport_type=StreamingTransportType.WEBSOCKET,
        content_type=MediaStreamingContentType.AUDIO,
        audio_channel_type=MediaStreamingAudioChannelType.MIXED,
        start_media_streaming=False,
        enable_bidirectional=True,
        enable_dtmf_tones=True,
        audio_format=AudioFormat.PCM16_K_MONO,
    )


async def answer_incoming_call(
    incoming_call_context: str,
    caller_number: str,
    *,
    tenant_id: Optional[str] = None,
    credentials: Optional[Mapping[str, Any]] = None,
) -> str:
    """Answer an incoming call and persist its tenant callback context."""
    from outreach_compliance import AI_VOICE_DISCLOSURE

    await ensure_call_state_available()
    resolved_credentials = _resolve_acs_credentials(credentials)
    resolved_tenant_id = str(tenant_id or _platform_tenant_id())
    initial_state = {
        "tenant_id": resolved_tenant_id,
        "acs_credentials": resolved_credentials,
    }
    callback_url = _get_callback_url()

    def _answer() -> str:
        client = _get_client(initial_state)
        props = client.answer_call(
            incoming_call_context=incoming_call_context,
            callback_url=callback_url,
            operation_context="answered",
            media_streaming=build_qwen_media_streaming_options(),
        )
        return props.call_connection_id or ""

    call_connection_id = await asyncio.to_thread(_answer)
    if not call_connection_id:
        logger.error("ACS answer_call returned no call_connection_id")
        return ""

    try:
        state = await initialize_call_state(
            call_connection_id,
            caller_number,
            tenant_id=resolved_tenant_id,
            credentials=resolved_credentials,
            direction="inbound",
        )
    except Exception:
        await abort_unmanaged_call(
            call_connection_id,
            credentials=resolved_credentials,
            tenant_id=resolved_tenant_id,
        )
        raise
    greeting = (
        AI_VOICE_DISCLOSURE
        + " I'm NEOH, your real estate AI assistant. How can I help you today?"
    )
    await _play_text(
        call_connection_id,
        greeting,
        operation_context="greeting",
        state=state,
    )
    state["stage"] = "greeting"
    await _save_call_state(call_connection_id, state)
    logger.info(
        "ACS inbound call answered: cid=%s tenant=%s",
        call_connection_id,
        state["tenant_id"],
    )
    return call_connection_id


async def start_outbound_conversation(
    call_connection_id: str,
    callee_number: str,
) -> None:
    """Play the disclosure greeting after an outbound call connects."""
    from outreach_compliance import AI_VOICE_DISCLOSURE

    async with _call_lock(call_connection_id):
        state = await _load_call_state(
            call_connection_id,
            wait_for_initialization=True,
        )
        if state is None:
            logger.error(
                "ACS outbound callback has no Redis state: cid=%s",
                call_connection_id,
            )
            return
        if state.get("direction") != "outbound":
            logger.info(
                "Ignoring outbound start for inbound ACS call: cid=%s",
                call_connection_id,
            )
            return
        if state.get("stage") not in {"answering", "queued"}:
            logger.info(
                "Ignoring duplicate ACS CallConnected callback: cid=%s stage=%s",
                call_connection_id,
                state.get("stage"),
            )
            return
        if not state.get("caller") and callee_number:
            state["caller"] = callee_number

        greeting = (
            AI_VOICE_DISCLOSURE
            + " I'm NEOH, your real estate AI assistant. How can I help you today?"
        )
        await _play_text(
            call_connection_id,
            greeting,
            operation_context="greeting",
            state=state,
        )
        state["stage"] = "greeting"
        await _save_call_state(call_connection_id, state)
        logger.info(
            "ACS outbound call connected: cid=%s tenant=%s",
            call_connection_id,
            state.get("tenant_id"),
        )


async def start_listening(call_connection_id: str) -> None:
    """Start speech recognition on an active call."""
    from azure.communication.callautomation import (
        PhoneNumberIdentifier,
        RecognizeInputType,
    )

    state = await _load_call_state(call_connection_id)
    if state is None:
        logger.error(
            "Cannot start ACS recognition without Redis state: cid=%s",
            call_connection_id,
        )
        return
    caller_number = str(state.get("caller") or "").strip()
    if not caller_number:
        logger.error("ACS call state has no caller: cid=%s", call_connection_id)
        return

    state["stage"] = "listening"
    await _save_call_state(call_connection_id, state)
    callback_url = _get_callback_url()

    def _recognize() -> None:
        client = _get_client(state)
        connection = client.get_call_connection(call_connection_id)
        caller = PhoneNumberIdentifier(caller_number)
        connection.start_recognizing_media(
            input_type=RecognizeInputType.SPEECH,
            target_participant=caller,
            end_silence_timeout=_END_SILENCE_TIMEOUT_SEC,
            speech_language="en-US",
            operation_context="listening",
            operation_callback_url=callback_url,
            initial_silence_timeout=8,
        )

    await asyncio.to_thread(_recognize)


async def handle_speech_recognized(
    call_connection_id: str,
    speech_text: str,
) -> None:
    """Send recognized speech to the AI and play its response."""
    async with _call_lock(call_connection_id):
        state = await _load_call_state(call_connection_id)
        if state is None:
            logger.error(
                "Ignoring ACS speech callback without Redis state: cid=%s",
                call_connection_id,
            )
            return
        state["no_input_count"] = 0
        state["turns"] = int(state.get("turns", 0)) + 1
        state["stage"] = "responding"
        await _save_call_state(call_connection_id, state)

        if state["turns"] >= _MAX_TURNS:
            await _play_text(
                call_connection_id,
                "I've reached my conversation limit. Please call back or visit "
                "neohrs.com. Goodbye.",
                operation_context="farewell",
                state=state,
            )
            state["stage"] = "ending"
            await _save_call_state(call_connection_id, state)
            return

        response_text = await _generate_voice_response(speech_text, state)
        await _play_text(
            call_connection_id,
            response_text,
            operation_context="response",
            state=state,
        )


async def handle_no_input(call_connection_id: str) -> None:
    """Handle a speech-recognition timeout."""
    async with _call_lock(call_connection_id):
        state = await _load_call_state(call_connection_id)
        if state is None:
            logger.error(
                "Ignoring ACS no-input callback without Redis state: cid=%s",
                call_connection_id,
            )
            return
        state["no_input_count"] = int(state.get("no_input_count", 0)) + 1
        await _save_call_state(call_connection_id, state)

        if state["no_input_count"] >= _MAX_NO_INPUT_RETRIES:
            await _play_text(
                call_connection_id,
                "I'm not hearing a response. Feel free to call back anytime. Goodbye.",
                operation_context="farewell",
                state=state,
            )
            state["stage"] = "ending"
            await _save_call_state(call_connection_id, state)
            return

        await _play_text(
            call_connection_id,
            "I didn't catch that. Could you please repeat?",
            operation_context="no_input_retry",
            state=state,
        )


async def handle_play_completed(
    call_connection_id: str,
    operation_context: str,
) -> None:
    """Start listening after media playback, unless the call is ending."""
    async with _call_lock(call_connection_id):
        state = await _load_call_state(call_connection_id)
        if state is None:
            logger.error(
                "Ignoring ACS play callback without Redis state: cid=%s",
                call_connection_id,
            )
            return
        if operation_context == "farewell":
            await _hangup(call_connection_id, state)
            return
        if operation_context == "greeting" and qwen_realtime_enabled(state):
            await start_qwen_media_streaming(call_connection_id, state)
        elif operation_context in {"greeting", "response", "no_input_retry"}:
            await start_listening(call_connection_id)


async def start_qwen_media_streaming(
    call_connection_id: str,
    state: Optional[dict[str, Any]] = None,
) -> None:
    """Start the configured ACS media socket only after disclosure playback."""
    call_state = state or await _load_call_state(call_connection_id)
    if call_state is None:
        logger.error(
            "Cannot start Qwen media without Redis state: cid=%s",
            call_connection_id,
        )
        return
    if not qwen_realtime_enabled(call_state):
        await start_listening(call_connection_id)
        return

    def _start() -> None:
        connection = _get_client(call_state).get_call_connection(call_connection_id)
        connection.start_media_streaming(operation_context="qwen-omni-realtime")

    try:
        await asyncio.to_thread(_start)
    except Exception:
        logger.exception(
            "Qwen media streaming could not start; falling back: cid=%s",
            call_connection_id,
        )
        call_state["qwen_realtime_failed"] = True
        await _save_call_state(call_connection_id, call_state)
        await start_listening(call_connection_id)
        return
    call_state["stage"] = "qwen_streaming"
    await _save_call_state(call_connection_id, call_state)
    logger.info("Qwen realtime media started: cid=%s", call_connection_id)


async def fallback_from_qwen_media(call_connection_id: str) -> None:
    """Stop a failed realtime stream and resume the legacy ACS STT/TTS cycle."""
    async with _call_lock(call_connection_id):
        state = await _load_call_state(call_connection_id)
        if state is None or state.get("stage") in {"ending", "completed"}:
            return

        def _stop() -> None:
            connection = _get_client(state).get_call_connection(call_connection_id)
            connection.stop_media_streaming(operation_context="qwen-fallback")

        try:
            await asyncio.to_thread(_stop)
        except Exception:
            logger.debug(
                "ACS media stop failed during Qwen fallback: cid=%s",
                call_connection_id,
                exc_info=True,
            )
        state["qwen_realtime_failed"] = True
        state["stage"] = "qwen_fallback"
        await _save_call_state(call_connection_id, state)
        await start_listening(call_connection_id)
        logger.warning(
            "Qwen realtime failed; ACS recognition fallback active: cid=%s",
            call_connection_id,
        )


async def cleanup_call(call_connection_id: str) -> None:
    try:
        redis = await _get_redis()
        await redis.delete(_state_key(call_connection_id))
    except ACSCallStateUnavailable:
        logger.warning(
            "ACS Redis state unavailable during cleanup: cid=%s",
            call_connection_id,
        )
    logger.info("ACS call cleaned up: cid=%s", call_connection_id)


async def _hangup(
    call_connection_id: str,
    state: Mapping[str, Any],
) -> None:
    def _disconnect() -> None:
        client = _get_client(state)
        connection = client.get_call_connection(call_connection_id)
        connection.hang_up(is_for_everyone=True)

    try:
        await asyncio.to_thread(_disconnect)
    except Exception:
        logger.debug(
            "hangup failed for cid=%s",
            call_connection_id,
            exc_info=True,
        )


async def abort_unmanaged_call(
    call_connection_id: str,
    *,
    credentials: Optional[Mapping[str, Any]] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """Best-effort disconnect when durable state cannot be established."""
    state = {
        "tenant_id": str(tenant_id or _platform_tenant_id()),
        "acs_credentials": _resolve_acs_credentials(credentials),
    }
    await _hangup(call_connection_id, state)


async def reap_stale_calls() -> None:
    """Refresh missing TTLs left by an interrupted legacy deployment."""
    redis = await _get_redis()
    async for key in redis.scan_iter(match=f"{_STATE_KEY_PREFIX}*"):
        ttl = await redis.ttl(key)
        if ttl < 0:
            await redis.expire(key, _STALE_CALL_TTL_SECONDS)


async def _play_text(
    call_connection_id: str,
    text: str,
    *,
    operation_context: str,
    state: Optional[Mapping[str, Any]] = None,
) -> None:
    from azure.communication.callautomation import TextSource

    call_state = state or await _load_call_state(call_connection_id)
    if call_state is None:
        raise ACSCallStateUnavailable(
            f"ACS call state is missing for {call_connection_id}"
        )
    callback_url = _get_callback_url()

    def _play() -> None:
        client = _get_client(call_state)
        connection = client.get_call_connection(call_connection_id)
        source = TextSource(
            text=text,
            voice_name=_VOICE,
            source_locale="en-US",
        )
        connection.play_media_to_all(
            play_source=[source],
            operation_context=operation_context,
            operation_callback_url=callback_url,
        )

    await asyncio.to_thread(_play)


async def _generate_voice_response(
    speech_text: str,
    state: dict[str, Any],
) -> str:
    """Generate a spoken response without exposing tenant credentials."""
    caller = state.get("caller", "Unknown")
    try:
        from ai_chat_agent import _generate_voice_reply

        return await _generate_voice_reply(caller, speech_text)
    except Exception:
        logger.exception(
            "Voice response generation failed for tenant=%s",
            state.get("tenant_id"),
        )
        return (
            "I apologize, but I'm having trouble processing that right now. "
            "Could you try again, or visit neohrs.com for help?"
        )
