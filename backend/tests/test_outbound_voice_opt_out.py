"""A spoken opt-out on an outbound AI call is recorded, with no transcript kept (L09-04, AC09.05)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import inbound_voice  # noqa: E402

# tests/ is a package, so load the sibling's bridge harness by path.
_spec = importlib.util.spec_from_file_location(
    "_el_harness", Path(__file__).with_name("test_elevenlabs_realtime.py"))
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
_run = _harness._run

TENANT = "11111111-1111-1111-1111-111111111111"
STATE = {"direction": "outbound", "tenant_id": TENANT, "callee": "+15555550123"}


def _say(*turns):
    return [{"type": "user_transcript", "user_transcription_event": {"user_transcript": t}} for t in turns]


@pytest.fixture
def recorded(monkeypatch):
    calls = []

    async def fake(tenant_id, callee):
        calls.append((tenant_id, callee))

    monkeypatch.setattr(inbound_voice, "record_outbound_voice_opt_out", fake)
    return calls


def test_a_spoken_opt_out_is_recorded_once_and_no_transcript_is_kept(monkeypatch, recorded):
    _, _, _, bridge = _run(monkeypatch, [], _say("Please stop calling me.", "I said stop"), state=dict(STATE))
    assert recorded == [(TENANT, "+15555550123")]
    assert bridge.opt_out_recorded is True
    assert bridge._transcript == []


def test_ordinary_outbound_speech_records_nothing(monkeypatch, recorded):
    _, _, _, bridge = _run(monkeypatch, [], _say("Sure, stop by the house on Tuesday."), state=dict(STATE))
    assert recorded == []
    assert bridge.opt_out_heard is False


def test_a_failed_recording_is_retried_when_the_call_ends(monkeypatch):
    attempts = []

    async def flaky(tenant_id, callee):
        attempts.append(callee)
        if len(attempts) == 1:
            raise RuntimeError("db down")

    monkeypatch.setattr(inbound_voice, "record_outbound_voice_opt_out", flaky)
    _, _, _, bridge = _run(monkeypatch, [], _say("remove me from your list"), state=dict(STATE))
    assert len(attempts) == 2, "the media loop survived the failure and the call end retried"
    assert bridge.opt_out_recorded is True


def test_inbound_calls_do_not_use_the_outbound_recorder(monkeypatch, recorded):
    state = {"direction": "inbound", "tenant_id": TENANT, "callee": "+15555550123"}
    import inbound_voice as iv

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(iv, "mark_inbound_streaming", noop)
    monkeypatch.setattr(iv, "finalize_inbound_voice_call", noop)
    _run(monkeypatch, [], _say("stop calling me"), state=state)
    assert recorded == []


def test_the_recorder_refuses_an_unusable_tenant_or_number():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(inbound_voice.record_outbound_voice_opt_out("not-a-uuid", "+15555550123"))
    with pytest.raises(ValueError):
        asyncio.run(inbound_voice.record_outbound_voice_opt_out(TENANT, ""))
