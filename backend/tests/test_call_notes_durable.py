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
    SETTINGS, START, Connect, FakeAgent, FakePlivo, _results, _run, _tool, install_notes_backend)

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
    # Hang-up still closes the consented call (its transcript status), with no notes.
    assert bridge.notes == [] and notes_backend["delivered"] == [(REF, [])]


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


def test_the_same_tool_call_id_with_other_text_is_a_new_note(monkeypatch, notes_backend):
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Has a dog"),
    ], state=STATE)
    assert [a["result"] for a in _answers(agent, "t2")] == ["Saved for their agent."] * 2
    assert notes_backend["calls"] == 2, "a reused id with new text was answered from the cache"
    assert notes_backend["saved"] == [(REF, "Prefers afternoon showings"), (REF, "Has a dog")]
    assert bridge.notes == ["Prefers afternoon showings", "Has a dog"]


def test_a_retried_id_that_fails_twice_is_one_unsaved_note(monkeypatch, notes_backend):
    notes_backend["fail"] = 4
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "", note="Has a dog"),
        _tool(el.TOOL_SAVE_NOTE, "", note="Has a cat"),
    ], state=STATE)
    assert all(a["is_error"] for a in _answers(agent, "t2") + _answers(agent, ""))
    # One note retried (t2) and two notes the provider sent without an id.
    assert notes_backend["unsaved"] == [3] and bridge.notes == []


@pytest.mark.parametrize("status", ["full", "empty", "unexpected"])
def test_a_write_that_did_not_save_never_says_saved(monkeypatch, notes_backend, status):
    notes_backend["status"] = status
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
    ], state=STATE)
    answer = _results(agent)["t2"]
    assert answer["is_error"] is True and "Saved for their agent" not in answer["result"]
    assert bridge.notes == []
    if status == "full":
        assert "nothing more was saved" in answer["result"]
    else:
        assert answer["result"] == el.NOTE_NOT_SAVED and notes_backend["unsaved"] == [1]


def test_a_refused_write_withdraws_consent_on_the_bridge(monkeypatch, notes_backend):
    notes_backend["status"] = "refused"
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
    ], state=STATE)
    results = _results(agent)
    assert results["t2"]["is_error"] and "do not say you have noted" in results["t2"]["result"]
    assert bridge.notes_consent is False and notes_backend["calls"] == 1, "wrote again after a refusal"
    assert results["t3"]["is_error"] and bridge.notes == []


def test_a_no_after_notes_were_saved_keeps_nothing_from_the_call(monkeypatch, notes_backend):
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog"),
        _tool(el.TOOL_NOTE_CONSENT, "t4", granted=False),
        _tool(el.TOOL_SAVE_NOTE, "t5", note="Call back Friday"),
    ], state=STATE)
    results = _results(agent)
    assert results["t4"]["is_error"] is False
    assert results["t4"]["result"].startswith("Recorded that they declined. Nothing from this call is kept.")
    assert notes_backend["saved"] == [], "the call's notes outlived the withdrawal"
    assert results["t5"]["is_error"] and notes_backend["calls"] == 2
    assert bridge.notes == [] and notes_backend["delivered"] == []


def test_a_no_that_did_not_reach_the_database_is_recorded_at_hang_up(monkeypatch, notes_backend):
    events = [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoon showings"),
        _tool(el.TOOL_NOTE_CONSENT, "t3", granted=False),
    ]
    real_consent = el_commands().record_call_note_consent

    async def consent(ref, granted):
        if not granted and len(notes_backend["consent"]) == 1:
            notes_backend["consent"].append((ref, granted))
            raise RuntimeError("database unavailable")
        return await real_consent(ref, granted)

    monkeypatch.setattr(el_commands(), "record_call_note_consent", consent)
    _, agent, _, bridge = _run(monkeypatch, [], events, state=STATE)
    assert _results(agent)["t3"]["is_error"] is True
    assert notes_backend["consent"] == [(REF, True), (REF, False), (REF, False)], "not retried at hang-up"
    assert notes_backend["saved"] == [] and notes_backend["delivered"] == []


def test_a_consented_call_with_no_notes_is_closed_at_hang_up(monkeypatch, notes_backend):
    _run(monkeypatch, [], [_tool(el.TOOL_NOTE_CONSENT, "t1", granted=True)], state=STATE)
    assert notes_backend["delivered"] == [(REF, [])] and notes_backend["unsaved"] == [0]


def test_a_failed_no_then_a_recorded_yes_keeps_what_was_saved_after_it(monkeypatch, notes_backend):
    # Review r2: the failed no used to be replayed at hang-up over the later
    # yes, deleting a note the caller had heard was saved.
    real_consent = el_commands().record_call_note_consent

    async def consent(ref, granted):
        if not granted and not any(g is False for _, g in notes_backend["consent"]):
            notes_backend["consent"].append((ref, granted))
            raise RuntimeError("database unavailable")
        return await real_consent(ref, granted)

    monkeypatch.setattr(el_commands(), "record_call_note_consent", consent)
    _, agent, _, bridge = _run(monkeypatch, [], [
        _tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
        _tool(el.TOOL_NOTE_CONSENT, "t2", granted=False),
        _tool(el.TOOL_NOTE_CONSENT, "t3", granted=True),
        _tool(el.TOOL_SAVE_NOTE, "t4", note="Has a dog"),
    ], state=STATE)
    results = _results(agent)
    assert results["t2"]["is_error"] is True and results["t3"]["is_error"] is False
    assert results["t4"]["result"] == "Saved for their agent."
    assert notes_backend["consent"] == [(REF, True), (REF, False), (REF, True)], "the failed no was replayed"
    assert notes_backend["saved"] == [(REF, "Has a dog")]
    assert notes_backend["delivered"] == [(REF, ["Has a dog"])]


def test_the_retry_tag_is_keyed_by_a_key_derived_for_it(monkeypatch):
    import hashlib
    import hmac

    import client_note_crypto
    from crypto import derive_tenant_key

    master, tenant = "unit-test-tag-master-key-0123456789", "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", master)
    tenant_key = derive_tenant_key(tenant, master).encode()
    tag_key = hmac.new(tenant_key, b"call-note-tag", hashlib.sha256).digest()
    text = "Prefers afternoon showings"
    tag = client_note_crypto.content_tag(tenant, text)
    assert tag == hmac.new(tag_key, text.encode(), hashlib.sha256).hexdigest()[:24]
    assert tag != hmac.new(tenant_key, text.encode(), hashlib.sha256).hexdigest()[:24], \
        "the tenant's encryption key is used directly as the HMAC key"
    assert tag == client_note_crypto.content_tag(tenant, text)
    assert tag != client_note_crypto.content_tag(tenant, "Has a dog")
    assert tag != client_note_crypto.content_tag("22222222-2222-2222-2222-222222222222", text)



# ── A hang-up during a note or consent write (external audit 23.7) ──────────
# The caller hangs up (Plivo "stop") while a write is running: the bridge
# cancels the tool call, the write goes on (shielded), and the close-out waits
# for it and counts what it did before it delivers notes or replays a "no".

def _hang_up_during_write(monkeypatch, events, started, after_hang_up=None):
    """Run the call; the caller hangs up once `started` is set (by a fake
    write), and `after_hang_up()` runs alongside the bridge's close-out."""
    import plivo_call_handler

    async def fake_state(uuid, **_kw):
        return dict(STATE)

    async def signed(_settings):
        return "wss://signed.example/conv"

    monkeypatch.setattr(plivo_call_handler, "load_plivo_call_state", fake_state)

    async def scenario():
        plivo_done = asyncio.Event()
        plivo, agent = FakePlivo([]), FakeAgent(events, plivo_done)

        async def receive_text():
            await started.wait()
            plivo_done.set()
            return json.dumps({"event": "stop"})

        plivo.receive_text = receive_text
        bridge = el.PlivoElevenLabsBridge(plivo, "c-1", START, SETTINGS, connect=Connect(agent), signed_url=signed)
        helper = asyncio.ensure_future(after_hang_up()) if after_hang_up else None
        async with asyncio.timeout(5):
            await bridge.run()
        if helper is not None:
            await helper
        return agent, bridge

    return asyncio.run(scenario())


def _release_after_hang_up(started, gate, order=None):
    async def release():
        await started.wait()
        for _ in range(20):   # the hang-up cancels the tool call; run() reaches its close-out
            await asyncio.sleep(0)
        if order is not None:
            order.append("released")
        gate.set()
    return release


def _slow_note(monkeypatch, slow_text, started, gate, *, fails=False, order=None):
    """save_call_note: the note `slow_text` waits for `gate` (and then fails
    if `fails`); every other note is written at once."""
    import commands_api

    real_save = commands_api.save_call_note
    real_deliver = commands_api.deliver_call_notes

    async def save(ref, note, **kw):
        if note == slow_text:
            started.set()
            await gate.wait()
            if fails:
                raise RuntimeError("database went away after the hang-up")
        result = await real_save(ref, note, **kw)
        if order is not None:
            order.append(("committed", note))
        return result

    async def deliver(*args, **kwargs):
        if order is not None:
            order.append("delivered")
        return await real_deliver(*args, **kwargs)

    monkeypatch.setattr(commands_api, "save_call_note", save)
    monkeypatch.setattr(commands_api, "deliver_call_notes", deliver)


NOTES_THEN = [_tool(el.TOOL_NOTE_CONSENT, "t1", granted=True),
              _tool(el.TOOL_SAVE_NOTE, "t2", note="Prefers afternoons")]


def test_a_hang_up_during_a_note_write_delivers_after_it_with_the_note_counted(monkeypatch, notes_backend):
    started, gate, order = asyncio.Event(), asyncio.Event(), []
    _slow_note(monkeypatch, "Has a dog", started, gate, order=order)
    agent, bridge = _hang_up_during_write(
        monkeypatch, NOTES_THEN + [_tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog")],
        started, _release_after_hang_up(started, gate, order))
    assert order == [("committed", "Prefers afternoons"), "released", ("committed", "Has a dog"), "delivered"], \
        "the close-out ran before the note write it interrupted"
    assert notes_backend["delivered"] == [(REF, ["Prefers afternoons", "Has a dog"])]
    assert notes_backend["unsaved"] == [0]
    assert "t3" not in _results(agent), "nothing is said to a caller who has gone"


def test_a_note_write_that_fails_after_the_hang_up_is_counted_unsaved(monkeypatch, notes_backend):
    started, gate = asyncio.Event(), asyncio.Event()
    _slow_note(monkeypatch, "Has a dog", started, gate, fails=True)
    _, bridge = _hang_up_during_write(
        monkeypatch, NOTES_THEN + [_tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog")],
        started, _release_after_hang_up(started, gate))
    assert bridge.notes == ["Prefers afternoons"]
    assert notes_backend["delivered"] == [(REF, ["Prefers afternoons"])]
    assert notes_backend["unsaved"] == [1], "a note lost after the hang-up was not counted"


def test_a_note_write_still_running_at_the_bound_is_counted_unsaved(monkeypatch, notes_backend, caplog):
    monkeypatch.setattr(el, "_SETTLE_TIMEOUT", 0.05)
    started, never = asyncio.Event(), asyncio.Event()
    _slow_note(monkeypatch, "Has a dog", started, never)
    with caplog.at_level(logging.ERROR, logger="oracle.elevenlabs_realtime"):
        _hang_up_during_write(monkeypatch, NOTES_THEN + [_tool(el.TOOL_SAVE_NOTE, "t3", note="Has a dog")],
                              started)
    assert notes_backend["delivered"] == [(REF, ["Prefers afternoons"])]
    assert notes_backend["unsaved"] == [1], "an unconfirmed write is not counted as saved"
    assert any("still running" in r.getMessage() for r in caplog.records)


def _slow_no(monkeypatch, seen, started, gate, *, outcome):
    """record_call_note_consent: the first "no" waits for `gate`, then
    `outcome` ('fails' raises; 'records' records it); later calls are normal.
    seen["attempts"] lists every call as it starts, finished or not."""
    import commands_api

    real_consent = commands_api.record_call_note_consent
    first = {"no": True}
    seen["attempts"] = []

    async def consent(ref, granted):
        seen["attempts"].append(granted)
        if not granted and first["no"]:
            first["no"] = False
            started.set()
            await gate.wait()
            if outcome == "fails":
                seen["consent"].append((ref, granted))
                raise RuntimeError("database went away after the hang-up")
        return await real_consent(ref, granted)

    monkeypatch.setattr(commands_api, "record_call_note_consent", consent)


def test_the_audit_probe_a_no_cut_off_by_a_hang_up_stays_pending(monkeypatch):
    """The 23.7 probe, flipped: cancelling the tool call mid-write (the caller
    hung up) used to leave _withdraw_pending False — CancelledError bypasses
    `except Exception` — so hang-up delivered notes saved under the earlier yes."""
    import commands_api

    started, gate = asyncio.Event(), asyncio.Event()

    async def consent(_ref, granted):
        started.set()
        await gate.wait()
        return True

    monkeypatch.setattr(commands_api, "record_call_note_consent", consent)

    async def scenario():
        bridge = _bridge()
        bridge.notes = ["saved earlier under a yes"]
        task = asyncio.create_task(bridge._on_tool_call(el.TOOL_NOTE_CONSENT, {"granted": False}, "t9"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pending_at_hang_up = bridge._withdraw_pending
        gate.set()
        await bridge._settle_writes()
        return pending_at_hang_up, bridge

    pending_at_hang_up, bridge = asyncio.run(scenario())
    assert pending_at_hang_up is True, "a no cut off by the hang-up is not replayed"
    # The shielded write then recorded it: counted at call end, nothing kept.
    assert bridge._withdraw_pending is False and bridge.notes_consent is False and bridge.notes == []


def test_a_no_whose_write_fails_after_the_hang_up_is_replayed(monkeypatch, notes_backend):
    started, gate = asyncio.Event(), asyncio.Event()
    _slow_no(monkeypatch, notes_backend, started, gate, outcome="fails")
    agent, _ = _hang_up_during_write(
        monkeypatch, NOTES_THEN + [_tool(el.TOOL_NOTE_CONSENT, "t3", granted=False)],
        started, _release_after_hang_up(started, gate))
    assert notes_backend["attempts"] == [True, False, False], "the no was not replayed"
    assert notes_backend["consent"] == [(REF, True), (REF, False), (REF, False)]
    assert notes_backend["saved"] == [], "notes saved before the no outlived it"
    assert notes_backend["delivered"] == []
    assert "t3" not in _results(agent)


def test_a_no_whose_write_commits_after_the_hang_up_is_written_once(monkeypatch, notes_backend):
    started, gate = asyncio.Event(), asyncio.Event()
    _slow_no(monkeypatch, notes_backend, started, gate, outcome="records")
    _, bridge = _hang_up_during_write(
        monkeypatch, NOTES_THEN + [_tool(el.TOOL_NOTE_CONSENT, "t3", granted=False)],
        started, _release_after_hang_up(started, gate))
    assert notes_backend["attempts"] == [True, False], "the write under way was cut off and the no replayed"
    assert notes_backend["consent"] == [(REF, True), (REF, False)]
    assert notes_backend["saved"] == [] and notes_backend["delivered"] == []
    assert bridge.notes == [] and bridge._withdraw_pending is False


def test_a_no_whose_write_is_still_running_at_the_bound_is_replayed(monkeypatch, notes_backend):
    monkeypatch.setattr(el, "_SETTLE_TIMEOUT", 0.05)
    started, never = asyncio.Event(), asyncio.Event()
    _slow_no(monkeypatch, notes_backend, started, never, outcome="records")
    _hang_up_during_write(monkeypatch, NOTES_THEN + [_tool(el.TOOL_NOTE_CONSENT, "t3", granted=False)], started)
    assert notes_backend["attempts"] == [True, False, False], "the replay at hang-up is missing"
    assert notes_backend["consent"] == [(REF, True), (REF, False)], "only the replay recorded it"
    assert notes_backend["saved"] == [] and notes_backend["delivered"] == []


def test_a_failed_no_then_a_yes_recorded_after_the_hang_up_keeps_the_notes(monkeypatch, notes_backend):
    """L09-18 r2 still holds when the later yes is the write a hang-up cut off:
    once recorded it supersedes the failed no, which is not replayed."""
    import commands_api

    real_consent = commands_api.record_call_note_consent
    started, gate = asyncio.Event(), asyncio.Event()

    async def consent(ref, granted):
        if not granted:
            notes_backend["consent"].append((ref, granted))
            raise RuntimeError("database unavailable")
        if any(g is False for _, g in notes_backend["consent"]):
            started.set()   # the second yes: the caller hangs up while it is written
            await gate.wait()
        return await real_consent(ref, granted)

    monkeypatch.setattr(commands_api, "record_call_note_consent", consent)
    _, bridge = _hang_up_during_write(
        monkeypatch, NOTES_THEN + [_tool(el.TOOL_NOTE_CONSENT, "t3", granted=False),
                                   _tool(el.TOOL_NOTE_CONSENT, "t4", granted=True)],
        started, _release_after_hang_up(started, gate))
    assert notes_backend["consent"] == [(REF, True), (REF, False), (REF, True)], "the failed no was replayed"
    assert notes_backend["saved"] == [(REF, "Prefers afternoons")]
    assert notes_backend["delivered"] == [(REF, ["Prefers afternoons"])]
    assert bridge._withdraw_pending is False
