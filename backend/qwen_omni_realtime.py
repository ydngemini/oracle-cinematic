"""Alibaba Qwen Omni realtime bridges for ACS and Twilio phone calls.

ACS streams 16 kHz mono PCM while Twilio Media Streams uses 8 kHz G.711
mu-law. Both transports are normalized to Qwen's PCM input and Qwen's 24 kHz
PCM response is converted back to the provider's wire format.

The bridges deliberately own no call credentials and persist no audio.
Provider call state is managed in Redis; the DashScope API key is read only
from the environment.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import jwt
import websockets
from fastapi import WebSocket
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from outreach_compliance import AI_VOICE_DISCLOSURE

logger = logging.getLogger("oracle.qwen_omni_realtime")

_INPUT_SAMPLE_RATE = 16_000
_OUTPUT_SAMPLE_RATE = 24_000
_TWILIO_SAMPLE_RATE = 8_000
_SAMPLE_WIDTH = 2
_CHANNELS = 1
_MAX_PROVIDER_AUDIO_BYTES = 64 * 1024
_DEFAULT_MODEL = "qwen3.5-omni-flash-realtime"
_DEFAULT_VOICE = "Ethan"
_SESSION_READY_TIMEOUT = 10.0
_ACS_JWKS_URL = "https://acscallautomation.communication.azure.com/calling/keys"
_ACS_JWT_ISSUER = "https://acscallautomation.communication.azure.com"


class QwenRealtimeError(RuntimeError):
    """Raised when a Qwen realtime session cannot safely continue."""


class QwenHandoffRequested(QwenRealtimeError):
    """The live call is being transferred to a human agent; end the AI session."""


class QwenCallLimitReached(QwenRealtimeError):
    """Raised when a call reaches the configured conversation-turn ceiling."""


@dataclass(frozen=True)
class QwenRealtimeSettings:
    api_key: str
    workspace_id: str
    region: str = "intl"
    model: str = _DEFAULT_MODEL
    voice: str = _DEFAULT_VOICE
    base_url: str = ""

    @classmethod
    def from_env(cls) -> "QwenRealtimeSettings":
        settings = cls(
            api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
            workspace_id=os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip(),
            region=os.getenv("DASHSCOPE_REGION", "intl").strip().lower(),
            model=os.getenv("QWEN_REALTIME_MODEL", _DEFAULT_MODEL).strip(),
            voice=os.getenv("QWEN_REALTIME_VOICE", _DEFAULT_VOICE).strip(),
            base_url=os.getenv("DASHSCOPE_REALTIME_URL", "").strip().rstrip("/"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.api_key:
            raise QwenRealtimeError("DASHSCOPE_API_KEY is not configured")
        if not self.base_url and not self.workspace_id:
            raise QwenRealtimeError(
                "DASHSCOPE_WORKSPACE_ID or DASHSCOPE_REALTIME_URL is required"
            )
        if self.region not in {"cn", "intl"}:
            raise QwenRealtimeError("DASHSCOPE_REGION must be cn or intl")
        if not self.model:
            raise QwenRealtimeError("QWEN_REALTIME_MODEL is empty")

    @property
    def websocket_url(self) -> str:
        if self.base_url:
            base = self.base_url
        elif self.region == "cn":
            base = (
                f"wss://{self.workspace_id}.cn-beijing.maas.aliyuncs.com"
                "/api-ws/v1/realtime"
            )
        else:
            base = (
                f"wss://{self.workspace_id}.ap-southeast-1.maas.aliyuncs.com"
                "/api-ws/v1/realtime"
            )
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}model={self.model}"


@lru_cache(maxsize=1)
def _acs_jwks_client() -> PyJWKClient:
    """Cache ACS signing metadata while preserving routine key-set refreshes."""
    return PyJWKClient(
        _ACS_JWKS_URL,
        cache_keys=True,
        max_cached_keys=16,
        cache_jwk_set=True,
        lifespan=300,
        timeout=5,
    )


def verify_acs_websocket_jwt(authorization: str) -> bool:
    """Validate the signed JWT ACS supplies during the WebSocket handshake."""
    scheme, separator, token = (authorization or "").strip().partition(" ")
    audience = os.getenv("ORACLE_ACS_RESOURCE_ID", "").strip()
    if (
        scheme.lower() != "bearer"
        or not separator
        or not token.strip()
        or not audience
    ):
        return False

    token = token.strip()
    try:
        signing_key = _acs_jwks_client().get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_ACS_JWT_ISSUER,
            audience=audience,
            leeway=30,
            options={"require": ["exp", "iss", "aud"]},
        )
    except (InvalidTokenError, PyJWKClientError, OSError, ValueError):
        return False
    return True


def acs_audio_frame(audio_b64: str) -> dict[str, Any]:
    return {
        "Kind": "AudioData",
        "AudioData": {"Data": audio_b64},
        "StopAudio": None,
    }


def acs_stop_audio_frame() -> dict[str, Any]:
    return {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}


def twilio_audio_frame(stream_sid: str, audio_b64: str) -> dict[str, Any]:
    return {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": audio_b64},
    }


def twilio_clear_audio_frame(stream_sid: str) -> dict[str, Any]:
    return {"event": "clear", "streamSid": stream_sid}


def twilio_mark_frame(stream_sid: str, name: str) -> dict[str, Any]:
    return {
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": name},
    }


def _system_instructions() -> str:
    return (
        "You are NEOH, an automated AI real-estate call assistant speaking on a "
        "recorded phone line. The application already played this exact disclosure "
        f"before connecting audio: {AI_VOICE_DISCLOSURE!r}. Never deny or obscure "
        "that you are automated or that the line is recorded. Be concise, warm, and "
        "natural. Ask one question at a time. Do not invent property facts, prices, "
        "repair estimates, legal conclusions, or promises. Never make a binding offer "
        "or agreement; explain that any numbers and terms require human approval. "
        "If the caller asks to stop, not be contacted, or be removed, acknowledge the "
        "request immediately, do not persuade them, and say goodbye. Do not request "
        "full Social Security numbers, bank credentials, card numbers, passwords, or "
        "other authentication secrets."
    )


class QwenOmniRealtimeBridge:
    """One full-duplex Qwen session bound to one ACS media WebSocket."""

    def __init__(
        self,
        acs_websocket: WebSocket,
        call_connection_id: str,
        settings: Optional[QwenRealtimeSettings] = None,
    ) -> None:
        self.acs_websocket = acs_websocket
        self.call_connection_id = call_connection_id
        self.settings = settings or QwenRealtimeSettings.from_env()
        self.qwen_websocket: Any = None
        self._session_ready = asyncio.Event()
        self._responding = False
        self._resample_state: Any = None
        self._closed = False
        self._turns = 0
        self._max_turns = max(
            1,
            min(80, int(os.getenv("QWEN_REALTIME_MAX_TURNS", "20"))),
        )

    async def run(self) -> None:
        logger.info(
            "Opening Qwen realtime media bridge: cid=%s model=%s region=%s",
            self.call_connection_id,
            self.settings.model,
            self.settings.region,
        )
        self.qwen_websocket = await websockets.connect(
            self.settings.websocket_url,
            additional_headers={
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )
        await self._send_qwen(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "voice": self.settings.voice,
                    "instructions": self._session_instructions(),
                    "enable_search": False,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "input_audio_transcription": {
                        "model": "qwen3-asr-flash-realtime"
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "threshold": 0.1,
                        "prefix_padding_ms": 500,
                        "silence_duration_ms": 700,
                    },
                },
            }
        )

        qwen_task = asyncio.create_task(
            self._qwen_to_acs(), name=f"qwen-to-acs:{self.call_connection_id}"
        )
        try:
            await asyncio.wait_for(
                self._session_ready.wait(), timeout=_SESSION_READY_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            qwen_task.cancel()
            await asyncio.gather(qwen_task, return_exceptions=True)
            raise QwenRealtimeError("Qwen session configuration timed out") from exc

        acs_task = asyncio.create_task(
            self._acs_to_qwen(), name=f"acs-to-qwen:{self.call_connection_id}"
        )
        tasks = {qwen_task, acs_task}
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error is not None:
                    raise error
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.qwen_websocket is not None:
            try:
                await self._send_qwen({"type": "session.finish"})
            except Exception:
                pass
            try:
                await self.qwen_websocket.close()
            except Exception:
                pass
        logger.info("Qwen realtime media bridge closed: cid=%s", self.call_connection_id)

    async def _send_qwen(self, event: dict[str, Any]) -> None:
        if self.qwen_websocket is None:
            raise QwenRealtimeError("Qwen WebSocket is not connected")
        event.setdefault("event_id", f"event_{time.time_ns()}")
        await self.qwen_websocket.send(json.dumps(event, separators=(",", ":")))

    def _session_instructions(self) -> str:
        return _system_instructions()

    async def _on_transcript_completed(self, role: str, transcript: str) -> None:
        if role != "caller":
            return
        self._turns += 1
        logger.info(
            "Qwen caller turn transcribed: cid=%s chars=%d turn=%d",
            self.call_connection_id,
            len(transcript),
            self._turns,
        )
        if self._turns >= self._max_turns:
            raise QwenCallLimitReached(
                f"Qwen call reached {self._max_turns} turns"
            )

    async def _acs_to_qwen(self) -> None:
        while True:
            message = await self.acs_websocket.receive_text()
            try:
                event = json.loads(message)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring malformed ACS media packet: cid=%s",
                    self.call_connection_id,
                )
                continue

            kind = str(event.get("kind") or event.get("Kind") or "")
            if kind == "AudioMetadata":
                metadata = event.get("audioMetadata") or {}
                sample_rate = int(metadata.get("sampleRate") or 0)
                channels = int(metadata.get("channels") or 0)
                if sample_rate != _INPUT_SAMPLE_RATE or channels != _CHANNELS:
                    raise QwenRealtimeError(
                        "ACS media must be 16 kHz mono PCM for Qwen realtime"
                    )
                continue
            if kind != "AudioData":
                continue

            audio_data = event.get("audioData") or event.get("AudioData") or {}
            audio_b64 = audio_data.get("data") or audio_data.get("Data")
            if not isinstance(audio_b64, str) or not audio_b64:
                continue
            await self._send_qwen(
                {"type": "input_audio_buffer.append", "audio": audio_b64}
            )

    async def _qwen_to_acs(self) -> None:
        async for message in self.qwen_websocket:
            try:
                event = json.loads(message)
            except (TypeError, ValueError):
                continue
            event_type = str(event.get("type") or "")

            if event_type == "session.updated":
                self._session_ready.set()
            elif event_type == "error":
                detail = event.get("error") or {}
                code = detail.get("code") if isinstance(detail, dict) else ""
                message_text = (
                    detail.get("message") if isinstance(detail, dict) else str(detail)
                )
                raise QwenRealtimeError(
                    f"Qwen realtime error {code or 'unknown'}: {message_text or 'unknown'}"
                )
            elif event_type == "response.created":
                self._responding = True
            elif event_type == "response.done":
                self._responding = False
                await self._mark_response_complete()
            elif event_type == "input_audio_buffer.speech_started":
                await self._clear_provider_audio()
                if self._responding:
                    await self._send_qwen({"type": "response.cancel"})
                    self._responding = False
            elif event_type == "response.audio.delta":
                delta = event.get("delta")
                if not isinstance(delta, str) or not delta:
                    continue
                try:
                    pcm_24k = base64.b64decode(delta, validate=True)
                except (ValueError, TypeError):
                    logger.warning(
                        "Ignoring invalid Qwen audio delta: cid=%s",
                        self.call_connection_id,
                    )
                    continue
                await self._send_provider_audio(pcm_24k)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = str(event.get("transcript") or "").strip()
                if transcript:
                    await self._on_transcript_completed("caller", transcript)
            elif event_type in {
                "response.audio_transcript.done",
                "response.output_audio_transcript.done",
                "response.text.done",
            }:
                transcript = str(
                    event.get("transcript") or event.get("text") or ""
                ).strip()
                if transcript:
                    await self._on_transcript_completed("assistant", transcript)

        raise QwenRealtimeError("Qwen realtime connection closed unexpectedly")

    async def _send_provider_audio(self, pcm_24k: bytes) -> None:
        pcm_16k, self._resample_state = audioop.ratecv(
            pcm_24k,
            _SAMPLE_WIDTH,
            _CHANNELS,
            _OUTPUT_SAMPLE_RATE,
            _INPUT_SAMPLE_RATE,
            self._resample_state,
        )
        if pcm_16k:
            await self.acs_websocket.send_json(
                acs_audio_frame(base64.b64encode(pcm_16k).decode("ascii"))
            )

    async def _clear_provider_audio(self) -> None:
        await self.acs_websocket.send_json(acs_stop_audio_frame())

    async def _mark_response_complete(self) -> None:
        return


class TwilioQwenRealtimeBridge(QwenOmniRealtimeBridge):
    """One Qwen session bound to one authenticated Twilio bidirectional Stream."""

    def __init__(
        self,
        twilio_websocket: WebSocket,
        call_sid: str,
        start_event: dict[str, Any],
        settings: Optional[QwenRealtimeSettings] = None,
    ) -> None:
        super().__init__(twilio_websocket, call_sid, settings=settings)
        start = start_event.get("start")
        if not isinstance(start, dict):
            raise QwenRealtimeError("Twilio start event is missing")
        stream_sid = str(start.get("streamSid") or start_event.get("streamSid") or "")
        media_format = start.get("mediaFormat")
        if not stream_sid.startswith("MZ") or len(stream_sid) > 64:
            raise QwenRealtimeError("Twilio stream SID is invalid")
        if not isinstance(media_format, dict):
            raise QwenRealtimeError("Twilio media format is missing")
        if (
            str(media_format.get("encoding") or "").lower() != "audio/x-mulaw"
            or int(media_format.get("sampleRate") or 0) != _TWILIO_SAMPLE_RATE
            or int(media_format.get("channels") or 0) != _CHANNELS
        ):
            raise QwenRealtimeError(
                "Twilio media must be 8 kHz mono G.711 mu-law"
            )
        self.stream_sid = stream_sid
        self._input_resample_state: Any = None
        self._mark_sequence = 0
        self._call_state: dict[str, Any] = {}
        self._transcript: list[dict[str, str]] = []
        self._handoff_started = False

    async def run(self) -> None:
        from inbound_voice import finalize_inbound_voice_call, mark_inbound_streaming
        from twilio_call_handler import load_twilio_call_state

        state = await load_twilio_call_state(self.call_connection_id)
        self._call_state = state if isinstance(state, dict) else {}
        if self._call_state.get("direction") == "inbound":
            await mark_inbound_streaming(self.call_connection_id)
        try:
            await super().run()
        except QwenHandoffRequested:
            # Not a failure: the call lives on, bridged to the agent's phone.
            logger.info(
                "Qwen session ended for live agent hand-off: sid=%s",
                self.call_connection_id,
            )
        finally:
            if self._call_state.get("direction") == "inbound":
                try:
                    await finalize_inbound_voice_call(
                        self.call_connection_id,
                        self._transcript,
                        self._call_state,
                    )
                except Exception:
                    logger.exception(
                        "Inbound transcript handoff failed: sid=%s",
                        self.call_connection_id,
                    )

    def _session_instructions(self) -> str:
        if self._call_state.get("direction") != "inbound":
            return super()._session_instructions()
        from inbound_voice import build_inbound_intake_instructions

        return build_inbound_intake_instructions(self._call_state)

    async def _on_transcript_completed(self, role: str, transcript: str) -> None:
        if transcript:
            self._transcript.append(
                {"role": role, "text": transcript[:4_000]}
            )
        if role == "caller" and await self._maybe_hand_off():
            # The caller is being bridged to a human; stop the AI session so the
            # assistant is not still talking over the transfer.
            raise QwenHandoffRequested("caller asked for a human agent")
        await super()._on_transcript_completed(role, transcript)

    async def _maybe_hand_off(self) -> bool:
        """Redirect the live call to the agent when the caller asks for a person."""
        if self._handoff_started:
            return False
        if not self._call_state.get("forward_available"):
            return False
        if self._call_state.get("direction") != "inbound":
            return False

        from inbound_voice import requested_human_handoff

        if not requested_human_handoff(self._transcript):
            return False

        self._handoff_started = True
        try:
            await self._redirect_to_agent()
        except Exception:
            # A failed redirect must not kill the call — the AI keeps handling it.
            logger.exception(
                "Live agent hand-off failed; continuing with AI: sid=%s",
                self.call_connection_id,
            )
            self._handoff_started = False
            return False
        logger.info(
            "Live agent hand-off started: sid=%s", self.call_connection_id
        )
        return True

    async def _redirect_to_agent(self) -> None:
        from telephony_api import transfer_webhook_url
        from twilio_call_handler import twilio_redirect_call

        url = await transfer_webhook_url(
            self.call_connection_id, reason="caller_request"
        )
        if not url:
            raise QwenRealtimeError("No transfer URL is available for this call")
        await twilio_redirect_call(self.call_connection_id, url)

    async def _acs_to_qwen(self) -> None:
        while True:
            message = await self.acs_websocket.receive_text()
            try:
                event = json.loads(message)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring malformed Twilio media packet: cid=%s",
                    self.call_connection_id,
                )
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event") or "")
            if event_type == "stop":
                return
            if event_type != "media" or event.get("streamSid") != self.stream_sid:
                continue
            media = event.get("media")
            audio_b64 = media.get("payload") if isinstance(media, dict) else None
            if not isinstance(audio_b64, str) or not audio_b64:
                continue
            try:
                mulaw_8k = base64.b64decode(audio_b64, validate=True)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid Twilio audio payload: cid=%s",
                    self.call_connection_id,
                )
                continue
            if not mulaw_8k or len(mulaw_8k) > _MAX_PROVIDER_AUDIO_BYTES:
                continue
            pcm_8k = audioop.ulaw2lin(mulaw_8k, _SAMPLE_WIDTH)
            pcm_16k, self._input_resample_state = audioop.ratecv(
                pcm_8k,
                _SAMPLE_WIDTH,
                _CHANNELS,
                _TWILIO_SAMPLE_RATE,
                _INPUT_SAMPLE_RATE,
                self._input_resample_state,
            )
            if pcm_16k:
                await self._send_qwen(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm_16k).decode("ascii"),
                    }
                )

    async def _send_provider_audio(self, pcm_24k: bytes) -> None:
        pcm_8k, self._resample_state = audioop.ratecv(
            pcm_24k,
            _SAMPLE_WIDTH,
            _CHANNELS,
            _OUTPUT_SAMPLE_RATE,
            _TWILIO_SAMPLE_RATE,
            self._resample_state,
        )
        if not pcm_8k:
            return
        mulaw_8k = audioop.lin2ulaw(pcm_8k, _SAMPLE_WIDTH)
        await self.acs_websocket.send_json(
            twilio_audio_frame(
                self.stream_sid,
                base64.b64encode(mulaw_8k).decode("ascii"),
            )
        )

    async def _clear_provider_audio(self) -> None:
        await self.acs_websocket.send_json(
            twilio_clear_audio_frame(self.stream_sid)
        )

    async def _mark_response_complete(self) -> None:
        self._mark_sequence += 1
        await self.acs_websocket.send_json(
            twilio_mark_frame(
                self.stream_sid,
                f"qwen-response-{self._mark_sequence}",
            )
        )
