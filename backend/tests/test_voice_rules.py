"""The voice rules on every carrier path and engine (prompt 09, T09.19,
AC09.01; ledger L09-07).

Every AI call — Twilio, Plivo and ACS; inbound and outbound; Qwen and
ElevenLabs — speaks the AI disclosure before any AI audio, and every engine's
instructions forbid denying automation or the recording. A new path or engine
that skips either fails here.
"""

from __future__ import annotations

import asyncio
import html
import re

import pytest

from outreach_compliance import AI_VOICE_DISCLOSURE

STREAM = "wss://neoh.test/api/voice/stream"
NO_DENIAL = re.compile(r"Never (deny|hide)[^.]*(automated|AI)", re.IGNORECASE)


def test_the_disclosure_says_what_it_must():
    text = AI_VOICE_DISCLOSURE.lower()
    assert "automated ai assistant" in text and "recorded line" in text
    assert "say 'stop'" in text and "won't contact you again" in text


def _before(markup: str, first: str, then: str) -> None:
    markup = html.unescape(markup)
    assert first in markup and then in markup, markup
    assert markup.index(first) < markup.index(then), markup


@pytest.mark.parametrize("cls_name, stream_tag", [
    ("TwilioVoiceProvider", "<Connect>"),
    ("PlivoVoiceProvider", "<Stream"),
])
def test_each_carrier_speaks_the_disclosure_before_the_media_stream(cls_name, stream_tag):
    import voice_provider

    provider = object.__new__(getattr(voice_provider, cls_name))
    markup = provider.speak_and_stream_markup(AI_VOICE_DISCLOSURE, stream_url=STREAM, bridge_token="tok")
    _before(markup, "you're speaking with an automated AI assistant", stream_tag)


@pytest.mark.parametrize("qwen", [True, False])
def test_the_outbound_twilio_call_opens_with_the_disclosure(monkeypatch, qwen):
    import commands_api
    import twilio_call_handler
    import voice_tts

    class _Request:
        async def form(self):
            return {"CallSid": "CA123", "To": "+13025550100"}

    async def state(*a, **k):
        return {"direction": "outbound", "tenant_id": "t"}

    async def no_tts(*a, **k):
        return None

    monkeypatch.setattr(commands_api, "_validate_twilio_webhook_signature", lambda *a, **k: None)
    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", state)
    monkeypatch.setattr(twilio_call_handler, "twilio_qwen_enabled", lambda s: qwen)
    monkeypatch.setattr(twilio_call_handler, "twilio_media_websocket_url", lambda: STREAM)
    monkeypatch.setattr(twilio_call_handler, "create_twilio_bridge_token", lambda sid: "tok")
    monkeypatch.setattr(voice_tts, "play_url", no_tts)
    response = asyncio.run(commands_api.twilio_webhook(_Request()))
    markup = response.body.decode()
    _before(markup, "you're speaking with an automated AI assistant", "<Connect>" if qwen else "<Gather")


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_acs_streams_only_after_the_disclosure_greeting(monkeypatch, direction):
    import acs_call_handler

    # The realtime engine on: the stream exists from answer, but inactive.
    monkeypatch.setattr(acs_call_handler, "qwen_realtime_enabled", lambda *a, **k: True)
    monkeypatch.setattr(acs_call_handler, "_get_media_streaming_url", lambda: STREAM)
    assert acs_call_handler.build_qwen_media_streaming_options().start_media_streaming is False

    played: list[tuple[str, str]] = []

    async def play(cid, text, *, operation_context, state):
        played.append((operation_context, text))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(acs_call_handler, "_play_text", play)
    monkeypatch.setattr(acs_call_handler, "_save_call_state", noop)
    if direction == "inbound":
        class _Client:
            def answer_call(self, **kw):
                assert kw["media_streaming"].start_media_streaming is False
                return type("P", (), {"call_connection_id": "cid-1"})()

        async def init_state(cid, caller, **kw):
            return {"direction": "inbound", "tenant_id": "t"}

        monkeypatch.setattr(acs_call_handler, "ensure_call_state_available", noop)
        monkeypatch.setattr(acs_call_handler, "_resolve_acs_credentials", lambda c: {})
        monkeypatch.setattr(acs_call_handler, "_get_callback_url", lambda: "https://neoh.test/cb")
        monkeypatch.setattr(acs_call_handler, "_get_client", lambda s: _Client())
        monkeypatch.setattr(acs_call_handler, "initialize_call_state", init_state)
        asyncio.run(acs_call_handler.answer_incoming_call("ctx", "+13025550100", tenant_id="t"))
    else:
        async def load(cid, **kw):
            return {"direction": "outbound", "stage": "answering", "tenant_id": "t"}

        import contextlib

        @contextlib.asynccontextmanager
        async def no_lock(cid):
            yield

        monkeypatch.setattr(acs_call_handler, "_load_call_state", load)
        monkeypatch.setattr(acs_call_handler, "_call_lock", no_lock)
        asyncio.run(acs_call_handler.start_outbound_conversation("cid-2", "+13025550100"))
    assert played and played[0][0] == "greeting"
    assert played[0][1].startswith(AI_VOICE_DISCLOSURE)


def test_every_engine_forbids_denying_automation():
    from elevenlabs_realtime import outbound_instructions
    from inbound_voice import build_inbound_intake_instructions
    from qwen_omni_realtime import _system_instructions

    prompts = {
        "qwen outbound": _system_instructions(),
        "elevenlabs outbound": outbound_instructions(_system_instructions(), {"briefing": "a home"})[0],
        "inbound intake (both engines)": build_inbound_intake_instructions({"direction": "inbound"}),
    }
    for name, prompt in prompts.items():
        assert NO_DENIAL.search(prompt), name
        assert "recorded" in prompt, name
