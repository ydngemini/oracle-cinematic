"""ElevenLabs Agents (Conversational AI) as the live voice on Plivo calls.

    Plivo bidirectional <Stream> (8 kHz G.711 mu-law)  <->  this bridge  <->
    ElevenLabs agent WebSocket (asr/tts configured as ulaw_8000)

Plivo speaks the AI disclosure itself (voice_provider.speak_and_stream_markup)
before the stream opens; ElevenLabs carries the conversation after it. Both
legs are mu-law 8 kHz, so audio is relayed byte-for-byte — no resampling, no
re-encoding, nothing written to disk.

Protocol (docs: elevenlabs.io/docs/agents-platform/api-reference/agents-platform/websocket):
  client -> server  conversation_initiation_client_data, {"user_audio_chunk": b64}, pong
  server -> client  conversation_initiation_metadata, audio, interruption, ping,
                    user_transcript, agent_response

The API key never leaves the backend: each call fetches a signed URL
(GET /v1/convai/conversation/get-signed-url) and the agent is created with
auth enabled, so it cannot be reached without one. The agent itself is
provisioned by scripts/elevenlabs-agent.py (voice recording off, minimal
retention, per-call prompt/first-message overrides allowed).

Behaviour mirrors the Qwen bridge: a turn limit (QWEN_REALTIME_MAX_TURNS applies
to every realtime engine), an inbound caller asking for a human is transferred,
and a failure mid-call hands an inbound caller to the agent. Outbound
transcripts are not stored (live-transcription consent is pending by design).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("oracle.elevenlabs_realtime")

_API_BASE = "https://api.elevenlabs.io"
_MAX_AUDIO_B64 = 256 * 1024
#: Plivo's stream format; the agent's asr/tts formats must match it exactly.
AUDIO_FORMAT = "ulaw_8000"


class ElevenLabsRealtimeError(RuntimeError):
    pass


class ElevenLabsCallLimitReached(ElevenLabsRealtimeError):
    pass


class ElevenLabsHandoffRequested(ElevenLabsRealtimeError):
    pass


@dataclass(frozen=True)
class ElevenLabsSettings:
    api_key: str
    agent_id: str
    api_base: str = _API_BASE

    @classmethod
    def from_env(cls) -> "ElevenLabsSettings":
        return cls(
            api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
            agent_id=os.getenv("ELEVENLABS_AGENT_ID", "").strip(),
            api_base=(os.getenv("ELEVENLABS_API_BASE", "").strip() or _API_BASE).rstrip("/"),
        )

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.agent_id)


def realtime_provider() -> str:
    """Which engine carries live call audio on Plivo: 'qwen' (default) or
    'elevenlabs'."""
    return os.getenv("ORACLE_PLIVO_REALTIME_PROVIDER", "qwen").strip().lower() or "qwen"


def elevenlabs_enabled() -> bool:
    return realtime_provider() == "elevenlabs" and ElevenLabsSettings.from_env().complete


async def fetch_signed_url(settings: ElevenLabsSettings) -> str:
    """A one-conversation WebSocket URL for the agent; the key stays here."""
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.api_base}/v1/convai/conversation/get-signed-url",
            params={"agent_id": settings.agent_id},
            headers={"xi-api-key": settings.api_key},
        )
    if response.status_code != 200:
        raise ElevenLabsRealtimeError(f"ElevenLabs signed URL request failed ({response.status_code})")
    url = str((response.json() or {}).get("signed_url") or "")
    if not url.startswith("wss://"):
        raise ElevenLabsRealtimeError("ElevenLabs returned no signed WebSocket URL")
    return url


def plivo_mulaw_play_frame(stream_id: str, audio_b64: str) -> dict[str, Any]:
    """Plivo playAudio carrying mu-law 8 kHz — the format the agent emits."""
    return {
        "event": "playAudio",
        "streamId": stream_id,
        "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000, "payload": audio_b64},
    }


def initiation_message(instructions: str, first_message: Optional[str]) -> dict[str, Any]:
    agent: dict[str, Any] = {"prompt": {"prompt": instructions}}
    if first_message:
        agent["first_message"] = first_message
    return {
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {"agent": agent},
    }


def outbound_instructions(base: str, state: dict[str, Any]) -> tuple[str, Optional[str]]:
    """The system prompt and opening line for an approved outbound call. The
    approved command's own reason is the briefing — the agent knows why it is
    calling and nothing more."""
    briefing = str(state.get("briefing") or "").strip()[:800]
    if not briefing:
        return base, None
    prompt = (
        f"{base}\n\nWhy you are calling (approved by the agent; do not add to it): {briefing}\n"
        "Goal: find out whether they are interested and, if so, whether they would like "
        "a showing. Do not schedule anything yourself: say their agent will confirm a time."
    )
    first = "Hi, thanks for picking up. I'm calling about a home that may be a good fit for you. Is now a good time?"
    return prompt, first


class PlivoElevenLabsBridge:
    """One ElevenLabs agent conversation bound to one authenticated Plivo stream."""

    def __init__(
        self,
        plivo_websocket: Any,
        call_uuid: str,
        start_event: dict[str, Any],
        settings: Optional[ElevenLabsSettings] = None,
        *,
        connect: Optional[Callable[..., Any]] = None,
        signed_url: Optional[Callable[[ElevenLabsSettings], Awaitable[str]]] = None,
    ) -> None:
        start = start_event.get("start") if isinstance(start_event, dict) else None
        if not isinstance(start, dict):
            raise ElevenLabsRealtimeError("Plivo start event is missing")
        stream_id = str(start.get("streamId") or start_event.get("streamId") or "")
        media_format = start.get("mediaFormat")
        if not stream_id:
            raise ElevenLabsRealtimeError("Plivo stream id is missing")
        if (not isinstance(media_format, dict)
                or str(media_format.get("encoding") or "").lower() != "audio/x-mulaw"
                or int(media_format.get("sampleRate") or 0) != 8000):
            raise ElevenLabsRealtimeError("Plivo media must be 8 kHz mono G.711 mu-law")
        self.plivo = plivo_websocket
        self.call_uuid = call_uuid
        self.stream_id = stream_id
        self.settings = settings or ElevenLabsSettings.from_env()
        if not self.settings.complete:
            raise ElevenLabsRealtimeError("ElevenLabs is not configured (ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID)")
        if connect is None:
            import websockets

            connect = websockets.connect
        self._connect = connect
        self._signed_url = signed_url or fetch_signed_url
        self._call_state: dict[str, Any] = {}
        self._transcript: list[dict[str, str]] = []
        self._turns = 0
        self._max_turns = max(1, min(80, int(os.getenv("QWEN_REALTIME_MAX_TURNS", "20"))))
        self._handoff_started = False
        self.conversation_id = ""

    # ── instructions ────────────────────────────────────────────────────
    def _instructions(self) -> tuple[str, Optional[str]]:
        from qwen_omni_realtime import _system_instructions

        if self._call_state.get("direction") == "inbound":
            from inbound_voice import build_inbound_intake_instructions

            return build_inbound_intake_instructions(self._call_state), None
        return outbound_instructions(_system_instructions(), self._call_state)

    # ── run ─────────────────────────────────────────────────────────────
    async def run(self) -> None:
        from plivo_call_handler import load_plivo_call_state

        state = await load_plivo_call_state(self.call_uuid)
        self._call_state = state if isinstance(state, dict) else {}
        inbound = self._call_state.get("direction") == "inbound"
        if inbound:
            from inbound_voice import mark_inbound_streaming

            await mark_inbound_streaming(self.call_uuid)
        try:
            url = await self._signed_url(self.settings)
            async with self._connect(url, max_size=2 ** 20, open_timeout=10) as agent:
                instructions, first_message = self._instructions()
                await agent.send(json.dumps(initiation_message(instructions, first_message)))
                logger.info("ElevenLabs conversation opened: uuid=%s", self.call_uuid)
                to_agent = asyncio.create_task(self._plivo_to_agent(agent))
                to_caller = asyncio.create_task(self._agent_to_plivo(agent))
                done, pending = await asyncio.wait({to_agent, to_caller}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        except ElevenLabsHandoffRequested:
            logger.info("ElevenLabs session ended for live agent hand-off: uuid=%s", self.call_uuid)
        except Exception as exc:
            from fastapi import WebSocketDisconnect

            if not isinstance(exc, (WebSocketDisconnect, ElevenLabsCallLimitReached)):
                await self._hand_off_after_failure()
            raise
        finally:
            if inbound:
                try:
                    from inbound_voice import finalize_inbound_voice_call

                    await finalize_inbound_voice_call(self.call_uuid, self._transcript, self._call_state)
                except Exception:
                    logger.exception("Inbound transcript handoff failed: uuid=%s", self.call_uuid)

    async def _plivo_to_agent(self, agent: Any) -> None:
        while True:
            raw = await self.plivo.receive_text()
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("event") or "")
            if kind == "stop":
                return
            if kind != "media" or event.get("streamId") != self.stream_id:
                continue
            media = event.get("media")
            payload = media.get("payload") if isinstance(media, dict) else None
            if isinstance(payload, str) and payload and len(payload) <= _MAX_AUDIO_B64:
                # Same format both sides (ulaw_8000): relayed as-is.
                await agent.send(json.dumps({"user_audio_chunk": payload}))

    async def _agent_to_plivo(self, agent: Any) -> None:
        async for raw in agent:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "audio":
                audio = (event.get("audio_event") or {}).get("audio_base_64")
                if isinstance(audio, str) and audio and len(audio) <= _MAX_AUDIO_B64:
                    await self.plivo.send_json(plivo_mulaw_play_frame(self.stream_id, audio))
            elif kind == "interruption":
                await self.plivo.send_json({"event": "clearAudio", "streamId": self.stream_id})
            elif kind == "ping":
                ping = event.get("ping_event") or {}
                await agent.send(json.dumps({"type": "pong", "event_id": ping.get("event_id")}))
            elif kind == "conversation_initiation_metadata":
                meta = event.get("conversation_initiation_metadata_event") or {}
                self.conversation_id = str(meta.get("conversation_id") or "")
                for side in ("agent_output_audio_format", "user_input_audio_format"):
                    if meta.get(side) and meta.get(side) != AUDIO_FORMAT:
                        raise ElevenLabsRealtimeError(
                            f"ElevenLabs agent {side} is {meta.get(side)!r}, not {AUDIO_FORMAT}; "
                            "re-run scripts/elevenlabs-agent.py")
            elif kind == "user_transcript":
                text = str((event.get("user_transcription_event") or {}).get("user_transcript") or "")
                await self._on_transcript("caller", text)
            elif kind == "agent_response":
                text = str((event.get("agent_response_event") or {}).get("agent_response") or "")
                await self._on_transcript("assistant", text)
            elif kind == "client_error":
                err = event.get("error_event") or {}
                raise ElevenLabsRealtimeError(f"ElevenLabs error {err.get('code')}: {err.get('error_name')}")

    # ── transcript, limits, hand-off ────────────────────────────────────
    async def _on_transcript(self, role: str, text: str) -> None:
        if not text:
            return
        if self._call_state.get("direction") == "inbound":
            self._transcript.append({"role": role, "text": text[:4_000]})
        if role != "caller":
            return
        self._turns += 1
        if await self._maybe_hand_off():
            raise ElevenLabsHandoffRequested("caller asked for a human agent")
        if self._turns >= self._max_turns:
            raise ElevenLabsCallLimitReached(f"call reached {self._max_turns} caller turns")

    async def _maybe_hand_off(self) -> bool:
        if (self._handoff_started or self._call_state.get("direction") != "inbound"
                or not self._call_state.get("forward_available")):
            return False
        from inbound_voice import requested_human_handoff

        if not requested_human_handoff(self._transcript):
            return False
        self._handoff_started = True
        try:
            await self._redirect_to_agent()
        except Exception:
            logger.exception("Live agent hand-off failed; continuing with AI: uuid=%s", self.call_uuid)
            self._handoff_started = False
            return False
        return True

    async def _redirect_to_agent(self, reason: str = "caller_request") -> None:
        from telephony_api import plivo_transfer_webhook_url
        from voice_provider import get_voice_provider

        url = await plivo_transfer_webhook_url(self.call_uuid, reason=reason)
        if not url:
            raise ElevenLabsRealtimeError("No transfer URL is available for this call")
        await get_voice_provider("plivo").transfer_call(self.call_uuid, redirect_url=url)

    async def _hand_off_after_failure(self) -> None:
        if self._call_state.get("direction") != "inbound" or self._handoff_started:
            return
        self._handoff_started = True
        try:
            await self._redirect_to_agent(reason="ai_unavailable")
            logger.warning("ElevenLabs failed mid-call; caller handed to the agent: uuid=%s", self.call_uuid)
        except Exception:
            logger.exception("ElevenLabs failed and the agent hand-off failed too: uuid=%s", self.call_uuid)
