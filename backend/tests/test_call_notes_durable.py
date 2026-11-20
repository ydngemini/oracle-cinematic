"""The AI says a note is saved only after it is (external audit 34.4 / RUN-03).

Owner decision 2026-11-20: one AI note per call, written when each note is
taken (commands_api.save_call_note) and appended to as the call goes; the
save_note tool answers "saved" only after that write committed, a failed
write is an error the AI says out loud, a retried tool call never appends
twice, and call-end delivery writes no second note. The database half is
proved against real PostgreSQL in test_call_notes_incremental_live.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import elevenlabs_realtime as el  # noqa: E402
from tests.test_elevenlabs_realtime import (  # noqa: E402 - after the sys.path line above
    SETTINGS, START, FakeAgent, FakePlivo, _results, _run, _tool, install_notes_backend)

REF = "ref-notes"
STATE = {"direction": "outbound", "reference": REF, "briefing": "123 Main Street."}


@pytest.fixture
def notes_backend(monkeypatch):
    return install_notes_backend(monkeypatch)


def _answers(agent, call_id):
    return [m for m in agent.sent if m.get("type") == "client_tool_result" and m["tool_call_id"] == call_id]


def _bridge(**state):
    bridge = el.PlivoElevenLabsBridge(FakePlivo([]), "c-1", START, SETTINGS, connect=lambda *a, **k: None)
    bridge._call_state = {**STATE, **state}
    bridge.notes_consent = True
    return bridge


def test_the_tool_answers_only_after_the_write_committed(monkeypatch, notes_backend):
    order = []
    real_save = el_commands().save_call_note
    real_send = FakeAgent.send

    async def save(ref, note, **kw):
        result = await real_save(ref, note, **kw)
        order.append(("committed", kw["tool_call_id"]))
        return result

    async def send(self, raw):
        msg = json.loads(raw)
        if msg.get("type") == "client_tool_result":
            order.append(("answered", msg["tool_call_id"]))
        await real_send(self, raw)

    monkeypatch.setattr(el_commands(), "save_call_note", save)
    monkeypatch.setattr(FakeAgent, "send", send)
    _, agent, _, _ = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
    ], state=STATE)
    assert order == [("answered", "t1"), ("committed", "t2"), ("answered", "t2"),
                     ("committed", "t3"), ("answered", "t3")]
    assert [_results(agent)[t]["result"] for t in ("t2", "t3")] == ["Saved for their agent."] * 2


def test_while_the_write_is_running_nothing_is_said_or_counted(monkeypatch):
    import commands_api

    gate, started = asyncio.Event(), asyncio.Event()

    async def save(ref, note, *, tool_call_id="", conversation_id=""):
        started.set()
        await gate.wait()
        return commands_api.CallNoteWrite("saved", "n1", 1)

    monkeypatch.setattr(commands_api, "save_call_note", save)

    async def scenario():
        bridge = _bridge()
        task = asyncio.create_task(bridge._on_tool_call(el.TOOL_SAVE_NOTE, {"note": "Prefers afternoons"}, "t1"))
        await started.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        pending = (task.done(), list(bridge.notes))
        gate.set()
        return pending, await task, bridge

    pending, answer, bridge = asyncio.run(scenario())
    assert pending == (False, []), "answered or counted before the write returned"
    assert answer == ("Saved for their agent.", False) and bridge.notes == ["Prefers afternoons"]


def test_a_failed_write_is_said_out_loud_and_nothing_is_claimed(monkeypatch, notes_backend, caplog):
    notes_backend["fail"] = 1
    with caplog.at_level(logging.ERROR, logger="oracle.elevenlabs_realtime"):
        _, agent, _, bridge = _run(monkeypatch, [], [
            _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
            _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
            _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
        ], state=STATE)
    results = _results(agent)
    assert results["t2"]["is_error"] is True
    assert "couldn't save that note" in results["t2"]["result"]
    assert "Do not say it was saved" in results["t2"]["result"]
    assert not results["t2"]["result"].startswith("Saved")
    assert (results["t3"]["result"], results["t3"]["is_error"]) == ("Saved for their agent.", False)
    assert bridge.notes == ["Has a dog"], "a note whose write failed is never counted as kept"
    assert notes_backend["saved"] == [(REF, "Has a dog")]
    # The agent sees it: logged now, and counted on the call's note at call end.
    assert any("Call note NOT saved" in r.getMessage() for r in caplog.records)
    assert notes_backend["delivered"] == [(REF, ["Has a dog"])] and notes_backend["unsaved"] == [1]


def test_when_every_write_fails_call_end_still_reports_the_unsaved_notes(monkeypatch, notes_backend):
    notes_backend["fail"] = 2
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
    ], state=STATE)
    assert all(_results(agent)[t]["is_error"] for t in ("t2", "t3"))
    assert bridge.notes == [] and notes_backend["saved"] == []
    assert notes_backend["delivered"] == [(REF, [])] and notes_backend["unsaved"] == [2]


def test_a_retried_tool_call_is_saved_once(monkeypatch, notes_backend):
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
    ], state=STATE)
    assert [(a["result"], a["is_error"]) for a in _answers(agent, "t2")] == [("Saved for their agent.", False)] * 2
    assert notes_backend["calls"] == 2, "the retry was answered without a second write"
    assert notes_backend["saved"] == [(REF, "Prefers afternoon showings"), (REF, "Has a dog")]
    assert bridge.notes == ["Prefers afternoon showings", "Has a dog"]


def test_a_retry_after_a_lost_answer_is_not_appended_twice(monkeypatch, notes_backend):
    # The first write committed but its answer was lost: the caller hears it
    # was not saved; the provider's retry of the same tool call finds it saved.
    notes_backend["fail_after_commit"] = 1
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
    ], state=STATE)
    first, retry = _answers(agent, "t2")
    assert first["is_error"] is True and "couldn't save that note" in first["result"]
    assert (retry["result"], retry["is_error"]) == ("Saved for their agent.", False)
    assert notes_backend["saved"] == [(REF, "Prefers afternoon showings")], "appended twice"
    assert bridge.notes == ["Prefers afternoon showings"]
    assert notes_backend["unsaved"] == [0], "the retry that found it saved clears the failure"


def test_notes_saved_before_a_crash_survive_it(monkeypatch, notes_backend):
    import commands_api

    answered = []
    real_send = FakeAgent.send

    async def send(self, raw):
        msg = json.loads(raw)
        if msg.get("type") == "client_tool_result":
            answered.append((msg["tool_call_id"], msg["result"]))
        await real_send(self, raw)

    async def delivery_fails(*_a, **_k):
        raise RuntimeError("database gone at hang-up")

    monkeypatch.setattr(FakeAgent, "send", send)
    monkeypatch.setattr(commands_api, "deliver_call_notes", delivery_fails)
    with pytest.raises(el.ElevenLabsRealtimeError):
        _run(monkeypatch, [], [
            _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
            _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
            _tool(el.TOOL_SAVE_NOTE, "t3", note="Would love waterfront"),
            {"type": "client_error", "error_event": {"code": 1011, "error_name": "internal"}},
        ], state=STATE)
    assert [a for a in answered if a[0] != "t1"] == [
        ("t2", "Saved for their agent."), ("t3", "Saved for their agent.")]
    # Both were written when the caller heard "saved"; neither waited for the
    # call-end delivery that never happened.
    assert notes_backend["saved"] == [(REF, "Prefers afternoon showings"), (REF, "Would love waterfront")]


def test_call_end_delivers_once_against_the_saved_note(monkeypatch, notes_backend):
    _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
    ], state=STATE)
    assert notes_backend["delivered"] == [(REF, ["Prefers afternoon showings", "Has a dog"])]
    assert notes_backend["unsaved"] == [0]


def test_a_call_with_nowhere_to_keep_notes_says_so_and_stops_writing(monkeypatch, notes_backend):
    notes_backend["status"] = "no_client"
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Wants to sell in spring"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
    ], state=STATE)
    results = _results(agent)
    assert results["t2"]["is_error"] and results["t3"]["is_error"]
    assert results["t2"]["result"] == el.NOTE_NOT_KEPT and "couldn't save that note" in el.NOTE_NOT_KEPT
    assert notes_backend["calls"] == 1, "a call with no client is not written again"
    assert bridge.notes == [] and notes_backend["delivered"] == []


def test_a_hang_up_does_not_cancel_a_write_already_running(monkeypatch):
    import commands_api

    gate, started, written = asyncio.Event(), asyncio.Event(), []

    async def save(ref, note, *, tool_call_id="", conversation_id=""):
        started.set()
        await gate.wait()
        written.append(note)
        return commands_api.CallNoteWrite("saved", "n1", 1)

    monkeypatch.setattr(commands_api, "save_call_note", save)

    async def scenario():
        bridge = _bridge()
        task = asyncio.create_task(bridge._on_tool_call(el.TOOL_SAVE_NOTE, {"note": "Prefers afternoons"}, "t1"))
        await started.wait()
        task.cancel()  # the caller hung up
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        return bridge

    bridge = asyncio.run(scenario())
    assert written == ["Prefers afternoons"], "the write was cut off by the hang-up"
    assert bridge.notes == [], "nothing was said to the caller, so nothing is counted as told"


def el_commands():
    import commands_api

    return commands_api
