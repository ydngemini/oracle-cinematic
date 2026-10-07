"""ElevenLabs Agents as the live voice on Plivo calls (elevenlabs_realtime.py)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import elevenlabs_realtime as el  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SETTINGS = el.ElevenLabsSettings(api_key="k", agent_id="agent_1")
START = {"event": "start", "start": {"streamId": "s-1", "callId": "c-1",
                                     "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}


class FakePlivo:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def receive_text(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        await asyncio.sleep(3600)

    async def send_json(self, data):
        self.sent.append(data)


class FakeAgent:
    def __init__(self, events, plivo_done):
        self.events = list(events)
        self.sent = []
        self.plivo_done = plivo_done
        self.drained = asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.events:
            await asyncio.sleep(0)
            return json.dumps(self.events.pop(0))
        # Let the bridge finish handling the last event before the caller hangs up.
        await asyncio.sleep(0.05)
        self.drained.set()
        await self.plivo_done.wait()
        raise StopAsyncIteration


class Connect:
    def __init__(self, agent):
        self.agent = agent
        self.url = None

    def __call__(self, url, **kwargs):
        self.url = url
        agent = self.agent

        class _Ctx:
            async def __aenter__(self_inner):
                return agent

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _run(monkeypatch, plivo_frames, agent_events, state=None):
    import plivo_call_handler

    async def fake_state(uuid, **_kw):
        return state if state is not None else {"direction": "outbound",
                                                 "briefing": "Sarah is a strong match for 123 Main Street."}

    monkeypatch.setattr(plivo_call_handler, "load_plivo_call_state", fake_state)

    async def signed(_settings):
        return "wss://signed.example/conv"

    async def scenario():
        done = asyncio.Event()
        plivo = FakePlivo(plivo_frames)
        agent = FakeAgent(agent_events, done)
        original = plivo.receive_text

        async def receive_text():
            if plivo.frames:
                return await original()
            await agent.drained.wait()   # the caller hangs up only after the agent's script
            done.set()
            return json.dumps({"event": "stop"})

        plivo.receive_text = receive_text
        connect = Connect(agent)
        bridge = el.PlivoElevenLabsBridge(plivo, "c-1", START, SETTINGS, connect=connect, signed_url=signed)
        await asyncio.wait_for(bridge.run(), 5)
        return plivo, agent, connect, bridge

    return asyncio.run(scenario())


def test_the_call_relays_audio_both_ways_untouched(monkeypatch):
    plivo, agent, connect, bridge = _run(
        monkeypatch,
        [{"event": "media", "streamId": "s-1", "media": {"payload": "AAEC"}},
         {"event": "media", "streamId": "other", "media": {"payload": "IGNORED"}}],
        [{"type": "conversation_initiation_metadata", "conversation_initiation_metadata_event": {
            "conversation_id": "conv-1", "agent_output_audio_format": "ulaw_8000",
            "user_input_audio_format": "ulaw_8000"}},
         {"type": "audio", "audio_event": {"audio_base_64": "f39/", "event_id": 1}},
         {"type": "interruption", "interruption_event": {"event_id": 2}},
         {"type": "ping", "ping_event": {"event_id": 7, "ping_ms": 40}}])
    assert connect.url == "wss://signed.example/conv", "a signed URL, never the API key"
    first = agent.sent[0]
    assert first["type"] == "conversation_initiation_client_data"
    override = first["conversation_config_override"]["agent"]
    assert "123 Main Street" in override["prompt"]["prompt"], "the call's own briefing"
    assert "AI" in override["prompt"]["prompt"] or "automated" in override["prompt"]["prompt"]
    assert override["first_message"]
    assert {"user_audio_chunk": "AAEC"} in agent.sent
    assert {"user_audio_chunk": "IGNORED"} not in agent.sent, "only this call's stream"
    assert {"type": "pong", "event_id": 7} in agent.sent
    assert el.plivo_mulaw_play_frame("s-1", "f39/") in plivo.sent
    assert {"event": "clearAudio", "streamId": "s-1"} in plivo.sent
    assert bridge.conversation_id == "conv-1"


def test_an_agent_with_the_wrong_audio_format_is_refused(monkeypatch):
    with pytest.raises(el.ElevenLabsRealtimeError, match="ulaw_8000"):
        _run(monkeypatch, [], [{"type": "conversation_initiation_metadata",
                                "conversation_initiation_metadata_event": {
                                    "agent_output_audio_format": "pcm_16000",
                                    "user_input_audio_format": "ulaw_8000"}}])


def test_the_caller_turn_limit_ends_the_session(monkeypatch):
    monkeypatch.setenv("QWEN_REALTIME_MAX_TURNS", "2")
    with pytest.raises(el.ElevenLabsCallLimitReached):
        _run(monkeypatch, [], [
            {"type": "user_transcript", "user_transcription_event": {"user_transcript": "hello"}},
            {"type": "user_transcript", "user_transcription_event": {"user_transcript": "yes"}}])


def test_outbound_transcripts_are_not_kept(monkeypatch):
    _, _, _, bridge = _run(monkeypatch, [], [
        {"type": "user_transcript", "user_transcription_event": {"user_transcript": "my private words"}}])
    assert bridge._transcript == [], "live-transcription consent is pending for outbound calls"


def test_a_call_without_a_briefing_gets_the_base_rules_and_no_opening_line():
    prompt, first = el.outbound_instructions("BASE", {})
    assert prompt.startswith("BASE") and first is None


@pytest.mark.parametrize("state", [{}, {"briefing": "Sarah may like 123 Main Street."}])
def test_the_agent_may_claim_a_note_only_after_consent_and_the_tool(state):
    # Call #5: it said "I have noted down that you are interested in a
    # waterfront property" while nothing kept a word.
    prompt, _ = el.outbound_instructions("BASE", state)
    assert el.TOOL_NOTE_CONSENT in prompt and el.TOOL_SAVE_NOTE in prompt
    assert "only after the tool confirms" in prompt
    assert "never say you have noted something" in prompt
    assert "confirm a time" not in prompt and "cannot book" in prompt


def _tool(name, call_id, **params):
    return {"type": "client_tool_call",
            "client_tool_call": {"tool_name": name, "tool_call_id": call_id, "parameters": params}}


def _results(agent):
    return {m["tool_call_id"]: m for m in agent.sent if m.get("type") == "client_tool_result"}


@pytest.fixture
def notes_backend(monkeypatch):
    import commands_api

    seen = {"consent": [], "delivered": []}

    async def consent(ref, granted):
        seen["consent"].append((ref, granted))
        return seen.get("session_exists", True)

    async def deliver(ref, notes, conversation_id=""):
        seen["delivered"].append((ref, list(notes)))
        return "note-1"

    monkeypatch.setattr(commands_api, "record_call_note_consent", consent)
    monkeypatch.setattr(commands_api, "deliver_call_notes", deliver)
    return seen


def test_notes_taken_after_a_yes_reach_the_agent(monkeypatch, notes_backend):
    state = {"direction": "outbound", "reference": "ref-1", "briefing": "123 Main Street."}
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Would love waterfront or a pond in the backyard"),
    ], state=state)
    results = _results(agent)
    assert [results[t]["is_error"] for t in ("t1", "t2", "t3")] == [False, False, False]
    assert notes_backend["consent"] == [("ref-1", True)], "consent keyed like the status callback"
    assert notes_backend["delivered"] == [("ref-1", [
        "Prefers afternoon showings", "Would love waterfront or a pond in the backyard"])]


def test_without_a_yes_save_note_refuses_and_nothing_is_delivered(monkeypatch, notes_backend):
    _, agent, _, _ = _run(monkeypatch, [], [
        _tool(el.TOOL_SAVE_NOTE, "t1", note="Prefers afternoons"),
        _tool(el.TOOL_NOTE_CONSENT, "t2", granted=False),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Prefers afternoons"),
    ])
    results = _results(agent)
    assert results["t1"]["is_error"] and results["t3"]["is_error"]
    assert "do not say you have noted" in results["t3"]["result"]
    assert notes_backend["delivered"] == []


def test_a_yes_with_no_call_session_keeps_nothing(monkeypatch, notes_backend):
    notes_backend["session_exists"] = False
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoons"),
    ])
    results = _results(agent)
    assert results["t1"]["is_error"] and results["t2"]["is_error"]
    assert bridge.notes == [] and notes_backend["delivered"] == []


def test_inbound_calls_do_not_use_the_note_tools(monkeypatch, notes_backend):
    import inbound_voice

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(inbound_voice, "mark_inbound_streaming", noop)
    monkeypatch.setattr(inbound_voice, "finalize_inbound_voice_call", noop)
    _, agent, _, _ = _run(monkeypatch, [], [_tool(el.TOOL_NOTE_CONSENT, "t1", granted=True)],
                          state={"direction": "inbound"})
    assert _results(agent)["t1"]["is_error"] and notes_backend["consent"] == []


def test_the_call_notes_read_as_the_callers_consented_words():
    import commands_api

    body = commands_api.format_call_notes(["Prefers afternoon showings"])
    assert "the caller agreed to notes" in body and "- Prefers afternoon showings" in body
    assert commands_api.AI_CALL_NOTE_AUTHOR == "neoh-ai-call", "never the agent's own id"


def test_a_non_mulaw_stream_is_refused():
    bad = {"start": {"streamId": "s", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 16000}}}
    with pytest.raises(el.ElevenLabsRealtimeError):
        el.PlivoElevenLabsBridge(FakePlivo([]), "c", bad, SETTINGS, connect=lambda *a, **k: None)


def test_live_voice_is_enabled_only_when_elevenlabs_is_fully_configured(monkeypatch):
    import plivo_call_handler

    monkeypatch.setenv("ORACLE_PLIVO_REALTIME_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ORACLE_PLIVO_QWEN_REALTIME_ENABLED", "0")
    monkeypatch.delenv("ELEVENLABS_AGENT_ID", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert plivo_call_handler.plivo_qwen_enabled() is False, "no agent id: the honest fallback"
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_1")
    assert plivo_call_handler.plivo_qwen_enabled() is True
    assert plivo_call_handler.plivo_qwen_enabled({"qwen_realtime_enabled": False}) is False
    monkeypatch.setenv("ORACLE_PLIVO_REALTIME_PROVIDER", "qwen")
    assert plivo_call_handler.plivo_qwen_enabled() is False, "qwen stays behind its own flag"


def test_the_provisioned_agent_matches_what_the_bridge_relies_on(monkeypatch):
    spec = importlib.util.spec_from_file_location("el_agent", REPO / "scripts" / "elevenlabs-agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.agent_config("staging")
    conv, plat = cfg["conversation_config"], cfg["platform_settings"]
    assert conv["asr"]["user_input_audio_format"] == el.AUDIO_FORMAT
    assert conv["tts"]["agent_output_audio_format"] == el.AUDIO_FORMAT
    assert plat["auth"]["enable_auth"] is True, "signed URLs only"
    assert plat["privacy"]["record_voice"] is False
    assert plat["overrides"]["conversation_config_override"]["agent"]["prompt"]["prompt"] is True
    assert conv["conversation"]["max_duration_seconds"] <= 600
    tools = {t["name"]: t for t in mod.tool_configs()}
    assert set(tools) == {el.TOOL_NOTE_CONSENT, el.TOOL_SAVE_NOTE}
    assert all(t["type"] == "client" and t["expects_response"] for t in tools.values())
    assert mod.agent_config("staging", ["a", "b"])["conversation_config"]["agent"]["prompt"]["tool_ids"] == ["a", "b"]


def test_the_approved_call_carries_its_reason_as_the_briefing():
    import inspect

    import commands_api

    src = inspect.getsource(commands_api)
    at = src.index("await initialize_outbound_plivo_call_state(")
    assert 'briefing=str(draft.get("reason") or "")' in src[at:at + 400]
