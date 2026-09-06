"""A send that leaves no trace can never be answered.

Until this existed, an approved message a provider accepted left no
interaction_logs row — crm.send_message wrote one for manual sends, the
command worker never did. So a thread could hold a client's reply and no send
for it to be a reply TO, intent_states saw the brokerage's outreach as silence,
and lead_response_events had a 'sent' disposition that nothing could write.
"""

from __future__ import annotations

from datetime import datetime, timezone

import commands_api as ca


def test_send_types_map_to_interaction_types_and_calendar_is_deliberately_absent():
    """A calendar event is an appointment, not a touch on a thread. It is
    recorded as an outcome, and must not also appear as an interaction."""
    assert ca._SEND_INTERACTION_TYPE == {"EMAIL": "email", "SMS": "sms", "CALL": "call_transcript"}
    assert "CALENDAR" not in ca._SEND_INTERACTION_TYPE


def test_every_mapped_interaction_type_is_one_the_check_accepts():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0095_perception_and_living_graph.sql").read_text()
    for value in ca._SEND_INTERACTION_TYPE.values():
        assert f"'{value}'" in sql, value


def test_event_start_reads_google_shape_and_survives_garbage():
    """Google drafts carry start.dateTime; a bare ISO string is tolerated; anything
    else yields None so the caller falls back to now() rather than crashing
    inside a best-effort block."""
    assert ca._event_start({"event": {"start": {"dateTime": "2026-09-05T14:00:00Z"}}}) == \
        datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
    assert ca._event_start({"event": {"start": "2026-09-05T14:00:00+00:00"}}).hour == 14
    assert ca._event_start({"event": {"start": {"date": "2026-09-05"}}}) is None
    assert ca._event_start({}) is None
    assert ca._event_start({"event": {"start": {"dateTime": "not a date"}}}) is None
    naive = ca._event_start({"event": {"start": {"dateTime": "2026-09-05T14:00:00"}}})
    assert naive is not None and naive.tzinfo is not None


def test_bookkeeping_sits_after_the_acknowledgement_not_inside_its_retry_loop():
    """The acknowledgement is the one write that must not be lost. Bookkeeping
    that could make that loop re-run — or turn a delivered message into a
    failed job — has to live after it, and be unable to raise."""
    import inspect

    worker = inspect.getsource(ca._execute_command_job)
    ack = worker.index("recording provider acknowledgement")
    guard = worker.index("provider acknowledgement could not be persisted")
    hook = worker.index("_record_send_bookkeeping(")
    assert ack < guard < hook, "bookkeeping must follow the persisted acknowledgement"

    helper = inspect.getsource(ca._record_send_bookkeeping)
    assert "except Exception" in helper
    assert "raise" not in helper.replace("raise_exception", ""), \
        "the helper must never raise into the worker"


def test_send_row_is_guarded_against_a_retried_worker():
    """interaction_logs has no dedupe key. A worker that reaches the block twice
    must not write two sends for one provider reference — that is the same
    inflation 0097 fixed for portal views."""
    import inspect

    helper = inspect.getsource(ca._record_send_bookkeeping)
    assert "NOT EXISTS" in helper
    assert "external_message_id = $6" in helper


def test_a_contact_only_target_writes_nothing_rather_than_inventing_an_anchor():
    import inspect

    helper = inspect.getsource(ca._record_send_bookkeeping)
    assert "chk_interaction_anchor" in helper
    assert "if interaction_type and (client_id or lead_id)" in helper


def test_sent_disposition_is_an_update_guarded_on_staged():
    """Enrich in place; never a second row (0097)."""
    import inspect

    helper = inspect.getsource(ca._record_send_bookkeeping)
    assert "SET disposition = 'sent'" in helper
    assert "AND disposition = 'staged'" in helper
