"""Alibaba Qwen Omni realtime bridge for Azure Communication Services calls.

ACS streams 16 kHz mono PCM to this service over a bidirectional WebSocket.
The bridge forwards those chunks to Qwen3.5 Omni Flash Realtime and streams the
model's 24 kHz PCM response back to ACS after stateful 24 -> 16 kHz resampling.

The bridge deliberately owns no call credentials and persists no audio. ACS
credentials remain in the encrypted Redis call state managed by
acs_call_handler.py; the DashScope API key is read only from the environment.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import websockets
from fastapi import WebSocket

from outreach_compliance import AI_VOICE_DISCLOSURE

logger = logging.getLogger("oracle.qwen_omni_realtime")

_INPUT_SAMPLE_RATE = 16_000
_OUTPUT_SAMPLE_RATE = 24_000
_SAMPLE_WIDTH = 2
_CHANNELS = 1
_DEFAULT_MODEL = "qwen3.5-omni-flash-realtime"
_DEFAULT_VOICE = "Ethan"
_SESSION_READY_TIMEOUT = 10.0


class QwenRealtimeError(RuntimeError):
    """Raised when a Qwen realtime session cannot safely continue."""


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


def verify_media_token(token: str) -> bool:
    expected = os.getenv("ORACLE_ACS_WEBHOOK_SECRET", "").strip()
    return bool(expected) and hmac.compare_digest(
        expected.encode("utf-8"),
        (token or "").encode("utf-8"),
    )


def acs_audio_frame(audio_b64: str) -> dict[str, Any]:
    return {
        "Kind": "AudioData",
        "AudioData": {"Data": audio_b64},
        "StopAudio": None,
    }


def acs_stop_audio_frame() -> dict[str, Any]:
    return {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}


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
                    "instructions": _system_instructions(),
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
            elif event_type == "input_audio_buffer.speech_started":
                await self.acs_websocket.send_json(acs_stop_audio_frame())
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
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = str(event.get("transcript") or "").strip()
                if transcript:
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

        raise QwenRealtimeError("Qwen realtime connection closed unexpectedly")
