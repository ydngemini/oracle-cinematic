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
                    user_transcript, agent_response, client_tool_call
  client -> server  client_tool_result {tool_call_id, result, is_error}

The API key never leaves the backend: each call fetches a signed URL
(GET /v1/convai/conversation/get-signed-url) and the agent is created with
auth enabled, so it cannot be reached without one. The agent itself is
provisioned by scripts/elevenlabs-agent.py (voice recording off, minimal
retention, per-call prompt/first-message overrides allowed).

Behaviour mirrors the Qwen bridge: a turn limit (QWEN_REALTIME_MAX_TURNS applies
to every realtime engine), an inbound caller asking for a human is transferred,
and a failure mid-call hands an inbound caller to the agent.

Outbound calls keep no transcript. The agent asks the callee whether it may
take notes for their agent (client tool record_note_consent); only after a yes
does save_note keep anything, and only the notes themselves — short facts the
callee shared. Each one is written to the call's one AI note on the client's
timeline when it is taken (commands_api.save_call_note), and the tool answers
"saved" only after that write committed; a failed write answers an error the
AI must say out loud (external audit 34.4 / RUN-03). At call end
commands_api.deliver_call_notes finds that note and writes no second one.
Without a yes, save_note refuses, so the agent cannot claim to have noted what
nothing kept.

A hang-up never cancels a note or consent write that already started, and the
call's close-out waits for it (at most _SETTLE_TIMEOUT) and counts what it did
before delivering notes or replaying a withdrawal (external audit 23.7). A "no"
is pending from the moment it is said until the database records it, so a
hang-up during that write still withdraws the call's notes.
"""

from __future__ import annotations

import asyncio
import hashlib
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
#: Client tools the provisioned agent declares (scripts/elevenlabs-agent.py).
TOOL_NOTE_CONSENT = "record_note_consent"
TOOL_SAVE_NOTE = "save_note"
_MAX_NOTES = 25
#: The answer to a save_note whose write did not commit: the AI says so.
NOTE_NOT_SAVED = ("Not saved: the note could not be saved. Tell them you couldn't save that note "
                  "and suggest they tell their agent directly. Do not say it was saved or noted.")
#: The answer once the call turns out to have nowhere to keep notes.
NOTE_NOT_KEPT = ("Not saved: notes cannot be kept on this call. Tell them you couldn't save that "
                 "note, take no more notes, and suggest they tell their agent directly.")
#: The answer whenever no note may be kept.
_REFUSE = ("No notes can be kept. Do not take notes and do not say you have noted "
           "anything; suggest they tell their agent directly.")
#: How long the call's close-out waits for a note or consent write the hang-up
#: interrupted (23.7). Each write is bounded in commands_api at 2 s of lock wait
#: and 5 s per statement; one still running after this is counted as not
#: confirmed (an unsaved note; a "no" replayed).
_SETTLE_TIMEOUT = 10.0


#: Note and consent writes in flight, kept referenced: a hang-up cancels the
#: bridge, never a write that already started (nothing was said about it yet
#: either way).
_NOTE_WRITES: set = set()


def _note_write_done(task: "asyncio.Task") -> None:
    _NOTE_WRITES.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved; the write logged its own failure


def _note_key(tool_call_id: Any, note: str) -> str:
    """A tool call is a retry only with the same id and the same text."""
    call_id = str(tool_call_id or "").strip()
    if not call_id:
        return ""
    return f"{call_id}:{hashlib.sha256(note.encode()).hexdigest()[:24]}"


def _write_result(write: "Optional[asyncio.Task]") -> Any:
    """What a settled write returned; None when it raised, was cancelled or
    had not finished (write is None)."""
    if write is None or write.cancelled() or write.exception() is not None:
        return None
    return write.result()


class ElevenLabsRealtimeError(RuntimeError):
    pass


class ElevenLabsCallLimitReached(ElevenLabsRealtimeError):
    pass


class ElevenLabsCallOptOut(ElevenLabsCallLimitReached):
    """The outbound callee said "stop" (voice_opt_out, L09-05). A limit
    subclass so the Plivo handler already ends the call on it."""


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
    calling and nothing more.

    Notes are real only after the callee agrees and save_note accepts them; on
    the first live call the agent told the callee "I have noted down that you
    are interested in a waterfront property" while nothing kept a word.
    """
    prompt = (
        f"{base}\n\nNotes for their agent: once they are happy to talk, ask once whether it is "
        f"alright to take a few notes for their agent, and call {TOOL_NOTE_CONSENT} with their "
        f"answer. If they agree, call {TOOL_SAVE_NOTE} with one short factual note each time they "
        "share an interest, a preference, a question or a good time to talk or visit, in their "
        "own terms; only after the tool confirms may you say you have noted it for their agent. "
        "If they decline or are unsure, take no notes. "
        f"If {TOOL_SAVE_NOTE} reports an error, tell them you couldn't save that note. "
        "Whenever they decline or a tool reports an error, never say you have noted "
        "something or will pass it along; suggest they tell their agent directly. "
        "You cannot book or hold times: their agent will follow up to confirm."
    )
    from call_briefing import briefing_instructions

    addendum = briefing_instructions(state)  # the same contract as Qwen (34.3)
    if not addendum:
        return prompt, None
    prompt += addendum
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
        self._inbound_opt_out_recorded = False  # RUN-02: once per call is enough
        self.conversation_id = ""
        #: None until the callee answers; notes are kept only after a True
        #: that the call's session row recorded.
        self.notes_consent: Optional[bool] = None
        #: Notes whose write committed (34.4 / RUN-03) — never one that did not.
        self.notes: list[str] = []
        #: Tool call key (id + text, _note_key) -> the answer it got, for each
        #: committed save_note: a retried tool call is answered again and never
        #: appended twice.
        self._saved_tool_calls: dict[str, str] = {}
        #: Keys ("" when the provider sent no id) of notes whose write failed
        #: and the caller was told so; a later success of the same key clears it.
        self.notes_unsaved: list[str] = []
        #: The call has nowhere to keep notes (a lead with no client record).
        self._notes_unavailable = False
        #: The callee said no and the database has not (yet) recorded it:
        #: set when the no is said, cleared only by a recorded answer, and
        #: replayed at hang-up while still set (23.7).
        self._withdraw_pending = False
        #: Shielded note / consent writes whose tool call a hang-up cancelled
        #: before it could count the result: write -> how to count it. The
        #: close-out waits for them before delivery or withdrawal (23.7).
        self._unsettled: dict[asyncio.Task, Callable[[Optional[asyncio.Task]], Any]] = {}
        #: Card numbers/codes removed from save_note calls (counts only).
        self.pan_cards = 0
        self.pan_codes = 0

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
                to_agent = asyncio.create_task(self._plivo_to_agent(agent),
                                               name=f"plivo-to-agent:{self.call_uuid}")
                to_caller = asyncio.create_task(self._agent_to_plivo(agent),
                                                name=f"agent-to-plivo:{self.call_uuid}")
                # The agent side first: its exceptions carry the call's outcome
                # (opt-out, hand-off, turn limit), Plivo's only a closed socket.
                streams = (to_caller, to_agent)
                done: set = set()
                try:
                    done, _ = await asyncio.wait(streams, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    # Joined here, inside the agent connection, as the Qwen
                    # bridge does (23.7): the losing side is cancelled AND
                    # awaited, so none of it runs after run() returns, and
                    # every exception is retrieved.
                    for task in streams:
                        task.cancel()
                    await asyncio.gather(*streams, return_exceptions=True)
                for task in streams:
                    if task in done and task.exception() is not None:
                        raise task.exception()
        except ElevenLabsHandoffRequested:
            logger.info("ElevenLabs session ended for live agent hand-off: uuid=%s", self.call_uuid)
        except Exception as exc:
            from fastapi import WebSocketDisconnect

            if not isinstance(exc, (WebSocketDisconnect, ElevenLabsCallLimitReached)):
                await self._hand_off_after_failure()
            raise
        finally:
            # A write the hang-up interrupted is counted first: delivery and a
            # replayed withdrawal must see what it did (23.7).
            await self._settle_writes()
            if not inbound and self._withdraw_pending:
                await self._withdraw_at_hang_up()
            elif not inbound and (self.notes_consent or self.notes or self.notes_unsaved):
                await self._deliver_notes()
            if inbound:
                try:
                    from inbound_voice import finalize_inbound_voice_call_with_retry

                    await finalize_inbound_voice_call_with_retry(self.call_uuid, self._transcript, self._call_state)
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
            elif kind == "client_tool_call":
                call = event.get("client_tool_call") or {}
                result, is_error = await self._on_tool_call(
                    str(call.get("tool_name") or ""), call.get("parameters") or {},
                    str(call.get("tool_call_id") or ""))
                await agent.send(json.dumps({
                    "type": "client_tool_result", "tool_call_id": call.get("tool_call_id"),
                    "result": result, "is_error": is_error}))
            elif kind == "client_error":
                err = event.get("error_event") or {}
                raise ElevenLabsRealtimeError(f"ElevenLabs error {err.get('code')}: {err.get('error_name')}")

    # ── notes for the agent ─────────────────────────────────────────────
    def _session_ref(self) -> str:
        """live_call_sessions.provider_call_id — the same chain the status
        callback uses (telephony_api.plivo_outbound_status)."""
        state = self._call_state
        return str(state.get("reference") or state.get("request_uuid") or self.call_uuid)

    async def _on_tool_call(self, name: str, params: Any, tool_call_id: str = "") -> tuple[str, bool]:
        params = params if isinstance(params, dict) else {}
        if self._call_state.get("direction") == "inbound":
            return "Notes are not used on this call.", True
        if name == TOOL_NOTE_CONSENT:
            from commands_api import record_call_note_consent

            granted = params.get("granted") is True
            if not granted:
                # Pending from the moment it is said (23.7), not only once its
                # write failed: a hang-up that cancels this tool call mid-write
                # bypasses `except Exception`, and the withdrawal must still be
                # replayed at hang-up — notes saved before a no never outlive
                # it. Only a recorded answer clears it (_consent_counted).
                self._withdraw_pending = True
            # Shielded like a note write: a hang-up right after "no" (the
            # likeliest moment) lets the write already under way commit
            # instead of tearing it down; the close-out waits for it and
            # replays the no only if it did not record.
            try:
                found = await self._shielded_write(
                    record_call_note_consent(self._session_ref(), granted),
                    lambda write: self._consent_settled(granted, write))
            except Exception:
                logger.exception("Note consent not recorded: uuid=%s", self.call_uuid)
                found = False
            return self._consent_counted(granted, bool(found))
        if name == TOOL_SAVE_NOTE:
            if self.notes_consent is not True:
                return "The caller has not agreed to notes. " + _REFUSE, True
            if self._notes_unavailable:
                return NOTE_NOT_KEPT, True
            from pan_redaction import CVV_PLACEHOLDER, PAN_PLACEHOLDER, redact, report

            redaction = redact(str(params.get("note") or "").strip())
            note = redaction.text.strip()[:300]
            if not note:
                return "Empty note — nothing saved.", True
            key = _note_key(tool_call_id, note)
            if key and key in self._saved_tool_calls:
                # A retried tool call (same id, same text): saved once, never
                # twice. The same id with other text is a new note.
                return self._saved_tool_calls[key], False
            if redaction.changed:
                # Counted, never kept: the in-call note list never holds the
                # number (PCI DSS SAQ A).
                self.pan_cards += redaction.cards
                self.pan_codes += redaction.codes
                report("ai_call_save_note", redaction.cards, redaction.codes)
                rest = note.replace(PAN_PLACEHOLDER, "").replace(CVV_PLACEHOLDER, "")
                if not any(ch.isalnum() for ch in rest):
                    return ("Card details are never kept and nothing was saved. Do not repeat "
                            "them; any payment is made only through a secure payment link."), True
            if len(self.notes) >= _MAX_NOTES:
                return "The note limit for this call is reached; nothing more was saved.", True
            # Written now, and acknowledged only once the write committed
            # (34.4 / RUN-03): a crash or hang-up cannot lose what the caller
            # heard was saved, and nothing is claimed for a failed write.
            redacted = redaction.changed
            try:
                outcome = await self._shielded_write(
                    self._save_note(note, str(tool_call_id or "").strip()),
                    lambda write: self._note_counted(key, note, redacted, _write_result(write)))
            except Exception:
                outcome = None
            return self._note_counted(key, note, redacted, outcome)
        return f"Unknown tool {name!r}.", True

    # ── writes a hang-up cannot cancel (RUN-06, 23.7) ───────────────────
    async def _shielded_write(self, coro: Awaitable[Any],
                              settle: Callable[[Optional[asyncio.Task]], Any]) -> Any:
        """Run a note or consent write that a hang-up cannot cancel. The tool
        call awaiting it counts the result itself; if a hang-up cancels the
        tool call first, `settle` counts it at call end (_settle_writes)."""
        task = asyncio.ensure_future(coro)
        _NOTE_WRITES.add(task)
        task.add_done_callback(_note_write_done)
        self._unsettled[task] = settle
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise  # left in _unsettled, even if the write just finished
        except BaseException:
            self._unsettled.pop(task, None)
            raise
        self._unsettled.pop(task, None)
        return result

    async def _settle_writes(self) -> None:
        """Call end: wait (at most _SETTLE_TIMEOUT) for writes whose tool call
        the hang-up cancelled, and count what each did — a note saved, a note
        not saved, an answer recorded or not — before the close-out reads it."""
        if not self._unsettled:
            return
        writes = dict(self._unsettled)
        _, late = await asyncio.wait(set(writes), timeout=_SETTLE_TIMEOUT)
        self._unsettled.clear()
        for write, settle in writes.items():
            if write in late:
                logger.error("A note or consent write was still running %.0f s after the hang-up; "
                             "counted as not confirmed: uuid=%s", _SETTLE_TIMEOUT, self.call_uuid)
                settle(None)
            else:
                settle(write)

    def _consent_settled(self, granted: bool, write: Optional[asyncio.Task]) -> None:
        if write is not None and not write.cancelled() and write.exception() is not None:
            logger.error("Note consent not recorded (write ended after the hang-up): uuid=%s",
                         self.call_uuid, exc_info=write.exception())
        self._consent_counted(granted, bool(_write_result(write)))

    def _consent_counted(self, granted: bool, found: bool) -> tuple[str, bool]:
        """Count one consent answer: `found` is whether the call's session row
        recorded it (False also when the write failed)."""
        if not found:
            # A no stays pending (set when it was said): replayed at hang-up.
            self.notes_consent = False
            return _REFUSE, True
        # The callee's latest answer is now recorded: an earlier no that
        # failed is superseded, never replayed over a later yes (L09-18 r2).
        self._withdraw_pending = False
        self.notes_consent = granted
        if granted:
            return f"Consent recorded. Save each detail with {TOOL_SAVE_NOTE}.", False
        # The database deleted any note saved earlier on this call.
        self.notes.clear()
        self.notes_unsaved.clear()
        self._saved_tool_calls.clear()
        return "Recorded that they declined. Nothing from this call is kept. " + _REFUSE, False

    def _note_counted(self, key: str, note: str, redacted: bool, outcome: Any) -> tuple[str, bool]:
        """Count one note write (`outcome` None: it raised, or at call end did
        not finish) and give the answer the AI says, if it is still there."""
        if outcome is None:
            self._mark_unsaved(key)
            return NOTE_NOT_SAVED, True
        if outcome.status == "refused":
            self.notes_consent = False
            return _REFUSE, True
        if outcome.status == "no_client":
            self._notes_unavailable = True
            return NOTE_NOT_KEPT, True
        if outcome.status == "full":
            return "The note limit for this call is reached; nothing more was saved.", True
        if not outcome.saved:
            logger.error("Call note NOT saved (%s); counted as unsaved: uuid=%s",
                         outcome.status, self.call_uuid)
            self._mark_unsaved(key)
            return NOTE_NOT_SAVED, True
        self.notes.append(note)
        if key and key in self.notes_unsaved:
            self.notes_unsaved.remove(key)
        if redacted:
            result = ("Saved for their agent without the card details, which are never kept. "
                      "Do not repeat them; any payment is made only through a secure payment link.")
        else:
            result = "Saved for their agent."
        if key:
            self._saved_tool_calls[key] = result
        return result, False

    def _mark_unsaved(self, key: str) -> None:
        """Count a note the caller was told was not saved — a retried tool
        call that fails again is still one note."""
        if not key or key not in self.notes_unsaved:
            self.notes_unsaved.append(key)

    async def _withdraw_at_hang_up(self) -> None:
        from commands_api import record_call_note_consent

        try:
            found = await record_call_note_consent(self._session_ref(), False)
            logger.info("Note consent withdrawal recorded at hang-up: uuid=%s found=%s", self.call_uuid, found)
        except Exception:
            logger.exception("Note consent withdrawal NOT recorded; the call's notes remain: uuid=%s",
                             self.call_uuid)

    async def _save_note(self, note: str, call_id: str) -> Any:
        from commands_api import save_call_note

        try:
            return await save_call_note(self._session_ref(), note, tool_call_id=call_id,
                                        conversation_id=self.conversation_id)
        except Exception:
            logger.exception("Call note NOT saved; the caller is told: uuid=%s", self.call_uuid)
            raise

    async def _deliver_notes(self) -> None:
        """Call end. The notes are already saved, one by one, in the call's
        note; delivery finds it and writes no second note. It closes the
        session's transcript status and shows any notes that could not be
        saved on the call's timeline row (or a task when none was saved)."""
        from commands_api import deliver_call_notes

        unsaved = len(self.notes_unsaved)
        try:
            note_id = await deliver_call_notes(self._session_ref(), self.notes, self.conversation_id,
                                               unsaved=unsaved)
            logger.info("Call notes closed: uuid=%s saved=%d unsaved=%d new_note_id=%s",
                        self.call_uuid, len(self.notes), unsaved, note_id)
        except Exception:
            # The saved notes are already in the call's note; only the close-out failed.
            logger.exception("Call notes close-out failed: uuid=%s saved=%d unsaved=%d",
                             self.call_uuid, len(self.notes), unsaved)

    # ── transcript, limits, hand-off ────────────────────────────────────
    async def _on_transcript(self, role: str, text: str) -> None:
        if not text:
            return
        if self._call_state.get("direction") == "inbound":
            self._transcript.append({"role": role, "text": text[:4_000]})
        if role != "caller":
            return
        import voice_opt_out

        if voice_opt_out.is_outbound(self._call_state) and voice_opt_out.spoken_opt_out(text):
            # The disclosure promised it (L09-05): suppress the number, then end
            # the call. Checked per utterance; nothing said is stored.
            await voice_opt_out.honour(self._call_state, self.call_uuid)
            raise ElevenLabsCallOptOut("outbound callee asked not to be contacted")
        if (voice_opt_out.is_inbound(self._call_state) and not self._inbound_opt_out_recorded
                and voice_opt_out.spoken_opt_out(text)):
            # RUN-02: written now, not only by finalize at hang-up.
            self._inbound_opt_out_recorded = await voice_opt_out.honour_inbound(
                self._call_state, self.call_uuid)
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
