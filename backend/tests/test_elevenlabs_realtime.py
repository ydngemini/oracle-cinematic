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


def test_a_call_without_a_briefing_gets_the_base_rules_only():
    prompt, first = el.outbound_instructions("BASE", {})
    assert (prompt, first) == ("BASE", None)


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


def test_the_approved_call_carries_its_reason_as_the_briefing():
    import inspect

    import commands_api

    src = inspect.getsource(commands_api)
    at = src.index("await initialize_outbound_plivo_call_state(")
    assert 'briefing=str(draft.get("reason") or "")' in src[at:at + 400]
