"""One lifecycle vocabulary, and lead_engaged metered only at delivery (A05).

speed_to_lead.py metered lead_engaged — the one Stripe-reported metric — when a
draft was merely STAGED. These tests pin the replacement:

  * every store's native status maps into outreach_lifecycle.Stage, and the maps
    cover every value the store's CHECK constraint allows;
  * staged, approved and accepted never meter;
  * delivered meters exactly once, however many receipts arrive;
  * a failed or cancelled send never meters;
  * a row the old code wrote at the staged step is promoted, not duplicated.

The SQL itself is proven against real PostgreSQL in
test_engagement_lifecycle_live.py; here a small in-memory fake stands in.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import functools
import re
from pathlib import Path

import pytest

import billing_usage
import outreach_lifecycle as ol
from outreach_lifecycle import Stage

TENANT = "11111111-1111-4111-8111-111111111111"
COMMAND = "33333333-3333-4333-8333-333333333333"
MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ── The vocabulary covers every stored value ────────────────────────────────

def _check_values(sql: str, anchor: str) -> set[str]:
    """The quoted values of the IN (...) list that follows ``anchor``."""
    start = sql.index(anchor)
    body = sql[start:sql.index(")", sql.index("IN", start))]
    return set(re.findall(r"'([a-z_]+)'", body))


def test_command_states_all_map():
    sql = (MIGRATIONS / "0027_real_estate_intelligence_platform.sql").read_text()
    table = sql[sql.index("CREATE TABLE IF NOT EXISTS command_executions"):]
    assert _check_values(table, "CHECK (state IN") == set(ol.COMMAND_STATE_STAGE)


def test_email_outbox_statuses_all_map():
    sql = (MIGRATIONS / "0012_agent_crm.sql").read_text()
    assert _check_values(sql, "CONSTRAINT chk_email_status") == set(ol.EMAIL_OUTBOX_STAGE)


def test_sms_statuses_all_map():
    sql = (MIGRATIONS / "0108_telnyx_messaging.sql").read_text()
    assert _check_values(sql, "CONSTRAINT sms_messages_status_chk") == set(ol.SMS_STATUS_STAGE)


def test_first_response_dispositions_all_map():
    sql = (MIGRATIONS / "0133_engagement_lifecycle.sql").read_text()
    allowed = _check_values(sql, "ADD CONSTRAINT lead_response_events_disposition_check")
    assert allowed == set(ol.DISPOSITION_STAGE)


def test_acceptance_is_never_delivery_in_any_store():
    """C6: "accepted" is not "delivered". SMTP has no receipt, so no email
    status may ever read as delivered; neither may a command's own state."""
    assert Stage.DELIVERED not in ol.EMAIL_OUTBOX_STAGE.values()
    assert Stage.DELIVERED not in ol.COMMAND_STATE_STAGE.values()
    assert ol.EMAIL_OUTBOX_STAGE["sent"] is Stage.ACCEPTED
    assert ol.COMMAND_STATE_STAGE["succeeded"] is Stage.ACCEPTED
    assert ol.COMMAND_STATE_STAGE["reconciliation_required"] is Stage.UNKNOWN
    assert ol.DISPOSITION_STAGE["sent"] is Stage.ACCEPTED
    assert ol.SMS_STATUS_STAGE["undelivered"] is Stage.FAILED


def test_only_delivered_is_billable():
    assert [s for s in Stage if ol.is_billable_engagement(s)] == [Stage.DELIVERED]


@pytest.mark.parametrize(
    ("status", "duration", "stage"),
    [
        ("answered", None, Stage.DELIVERED),
        ("in-progress", None, Stage.DELIVERED),
        ("completed", 42, Stage.DELIVERED),
        # Plivo reports a never-answered call as completed with zero billed time.
        ("completed", 0, Stage.FAILED),
        ("completed", None, Stage.FAILED),
        ("no-answer", None, Stage.FAILED),
        ("busy", None, Stage.FAILED),
        ("ringing", None, Stage.ACCEPTED),
        ("something-new", None, Stage.UNKNOWN),
    ],
)
def test_only_an_answer_delivers_a_call(status, duration, stage):
    assert ol.call_status_stage(status, duration) is stage


def test_the_ledger_records_only_its_own_stages():
    with pytest.raises(ValueError):
        asyncio.run(ol.advance_first_response(
            _Conn(_World()), tenant_id=TENANT, command_id=COMMAND, stage=Stage.RESPONDED,
        ))


# ── Metering happens at delivery, once ──────────────────────────────────────

class _World:
    """The three rows the lifecycle reads and writes, in memory."""

    def __init__(self, *, disposition="staged", state="awaiting_approval", origin="speed_to_lead"):
        self.lre = {"disposition": disposition, "origin": origin, "intake_event_id": "evt-1",
                    "contact_id": None, "client_id": None, "lead_id": None,
                    "delivered_at": None}
        self.command = {"state": state}
        self.sms_delivered = False
        self.sms_failed = False
        self.usage: dict[str, dict] = {}


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, world: _World):
        self.w = world

    def transaction(self):
        return _Savepoint()

    async def execute(self, sql, *args):
        assert "UPDATE lead_response_events" in sql
        _tenant, _command, target, earlier = args[:4]
        if target in ("accepted", "delivered", "failed"):
            assert "c.state = 'succeeded'" in sql
            if self.w.command["state"] != "succeeded":
                return "UPDATE 0"
        if self.w.lre["disposition"] in earlier:
            self.w.lre["disposition"] = target
            if target == "delivered":
                self.w.lre["delivered_at"] = args[4]
            return "UPDATE 1"
        return "UPDATE 0"

    async def fetchrow(self, sql, *args):
        if "INSERT INTO billing_usage_events" in sql:
            key = args[2]
            row = self.w.usage.get(key)
            if row is None:
                self.w.usage[key] = {"lifecycle_stage": "delivered", "source_id": args[3],
                                     "reported_at": None}
                return {"id": "u1"}
            if row["lifecycle_stage"] is None and row["reported_at"] is None:
                row.update(lifecycle_stage="delivered", source_id=args[3])
                return {"id": "u1"}
            return None
        assert "FROM lead_response_events r" in sql
        lre, cmd = self.w.lre, self.w.command
        if (lre["disposition"] == "delivered" and cmd["state"] == "succeeded"
                and lre["origin"] == "speed_to_lead"):
            return dict(lre)
        return None

    def _sms_status(self):
        if self.w.command["state"] != "succeeded":
            return None
        return "delivered" if self.w.sms_delivered else ("failed" if self.w.sms_failed else None)

    async def fetchval(self, sql, *args):
        return self._sms_status()

    async def fetch(self, sql, *args):
        status = self._sms_status()
        wanted = "'delivered'" if "m.status = 'delivered'" in sql else "('failed', 'undelivered')"
        if status and ((status == "delivered") == (wanted == "'delivered'")):
            return [{"tenant_id": TENANT, "id": COMMAND}]
        return []


async def _advance(conn, stage):
    return await ol.advance_first_response(conn, tenant_id=TENANT, command_id=COMMAND, stage=stage)


@sync
async def test_staged_approved_and_accepted_never_meter():
    world = _World()
    conn = _Conn(world)
    assert world.usage == {}, "staging alone leaves no usage row"
    world.command["state"] = "queued"
    assert await _advance(conn, Stage.APPROVED) is True
    world.command["state"] = "succeeded"
    assert await _advance(conn, Stage.ACCEPTED) is True
    assert world.lre["disposition"] == "accepted"
    assert world.usage == {}, "provider acceptance is not delivery"


@sync
async def test_delivered_meters_exactly_once():
    world = _World(disposition="accepted", state="succeeded")
    conn = _Conn(world)
    assert await _advance(conn, Stage.DELIVERED) is True
    # A duplicate receipt, a redelivery, a retried worker.
    assert await _advance(conn, Stage.DELIVERED) is False
    assert await ol.meter_delivered_engagement(conn, tenant_id=TENANT, command_id=COMMAND) is False
    assert list(world.usage) == ["lead-engaged:evt-1"]
    assert world.usage["lead-engaged:evt-1"] == {
        "lifecycle_stage": "delivered", "source_id": COMMAND, "reported_at": None,
    }


@sync
async def test_a_receipt_that_beat_the_acceptance_is_applied_at_acceptance():
    world = _World(disposition="staged", state="succeeded")
    world.sms_delivered = True
    conn = _Conn(world)
    await _advance(conn, Stage.ACCEPTED)
    assert await ol.reconcile_command_delivery(conn, tenant_id=TENANT, command_id=COMMAND) is True
    assert world.lre["disposition"] == "delivered"
    assert len(world.usage) == 1


@sync
async def test_a_failed_send_never_meters():
    world = _World(disposition="approved", state="failed")
    conn = _Conn(world)
    # Even a stray "delivered" signal cannot move or bill a command that never
    # succeeded.
    assert await _advance(conn, Stage.DELIVERED) is False
    assert world.lre["disposition"] == "approved"
    assert world.usage == {}
    assert await ol.record_sms_delivered(conn, provider="telnyx", provider_message_id="m1") == 0
    assert world.usage == {}


@sync
async def test_a_cancelled_send_never_meters():
    world = _World(disposition="staged", state="cancelled")
    conn = _Conn(world)
    assert await _advance(conn, Stage.CANCELLED) is True
    assert world.lre["disposition"] == "cancelled"
    # Terminal: nothing moves it on, and nothing bills it.
    assert await _advance(conn, Stage.DELIVERED) is False
    assert world.lre["disposition"] == "cancelled"
    assert world.usage == {}


@sync
async def test_a_blocked_lead_never_moves_or_meters():
    world = _World(disposition="blocked", state="succeeded")
    conn = _Conn(world)
    assert await _advance(conn, Stage.DELIVERED) is False
    assert world.usage == {}


@sync
async def test_a_manual_first_response_is_not_a_speed_to_lead_engagement():
    world = _World(disposition="accepted", state="succeeded", origin="manual")
    await _advance(_Conn(world), Stage.DELIVERED)
    assert world.usage == {}


@sync
async def test_a_staged_step_row_is_promoted_not_duplicated():
    world = _World(disposition="accepted", state="succeeded")
    world.usage["lead-engaged:evt-1"] = {"lifecycle_stage": None, "source_id": None,
                                         "reported_at": None}
    assert await _advance(_Conn(world), Stage.DELIVERED) is True
    assert world.usage == {"lead-engaged:evt-1": {
        "lifecycle_stage": "delivered", "source_id": COMMAND, "reported_at": None,
    }}


@sync
async def test_a_staged_step_row_already_reported_to_stripe_is_left_for_a_human():
    world = _World(disposition="accepted", state="succeeded")
    world.usage["lead-engaged:evt-1"] = {"lifecycle_stage": None, "source_id": None,
                                         "reported_at": "2026-10-01"}
    await _advance(_Conn(world), Stage.DELIVERED)
    assert world.usage["lead-engaged:evt-1"]["lifecycle_stage"] is None


@sync
async def test_a_database_failure_never_raises_into_the_provider_event():
    class _Broken(_Conn):
        async def execute(self, *a, **k):
            raise RuntimeError("connection lost")

    assert await _advance(_Broken(_World()), Stage.ACCEPTED) is False


def test_the_earlier_stage_lists_are_monotonic():
    assert ol._earlier_dispositions(Stage.APPROVED) == ["staged"]
    assert set(ol._earlier_dispositions(Stage.ACCEPTED)) == {"staged", "approved"}
    assert set(ol._earlier_dispositions(Stage.DELIVERED)) == {"staged", "approved", "accepted", "sent"}
    assert ol._earlier_dispositions(Stage.CANCELLED) == ["staged", "approved"]
    # A carrier failure: only after the provider took it, never after delivered.
    assert ol._earlier_dispositions(Stage.FAILED) == ["approved", "accepted", "sent"]


def test_the_engagement_writer_names_its_delivered_source():
    """The 0133 CHECK refuses a billable lead_engaged row without one."""
    import inspect

    src = inspect.getsource(billing_usage.record_lead_engagement)
    assert "'delivered','command_executions'" in src
    assert "lifecycle_stage IS NULL" in src and "reported_at IS NULL" in src


# ── The provider events reach the lifecycle ─────────────────────────────────

def test_a_carrier_delivery_receipt_reaches_the_lifecycle():
    import inspect

    import messaging_data

    src = inspect.getsource(messaging_data.record_delivery_update)
    assert "outreach_lifecycle.record_sms_delivered" in src
    assert "outreach_lifecycle.record_sms_failed" in src
    assert "SMS_STATUS_STAGE" in src


@sync
async def test_a_carrier_failure_marks_the_first_response_failed_and_never_meters():
    """L04-01 gap: a text the carrier rejected used to stay 'accepted', so it
    counted as a fast response the lead never received."""
    world = _World(disposition="accepted", state="succeeded")
    world.sms_failed = True
    conn = _Conn(world)
    assert await ol.record_sms_failed(conn, provider="telnyx", provider_message_id="m1") == 1
    assert world.lre["disposition"] == "failed"
    assert world.usage == {}
    # Terminal: a duplicate failure, or a late "delivered", moves nothing.
    assert await ol.record_sms_failed(conn, provider="telnyx", provider_message_id="m1") == 0
    assert await _advance(conn, Stage.DELIVERED) is False
    assert world.lre["disposition"] == "failed" and world.usage == {}


@sync
async def test_a_failure_never_undoes_a_delivery_or_touches_an_unaccepted_command():
    delivered = _World(disposition="delivered", state="succeeded")
    delivered.sms_failed = True
    assert await _advance(_Conn(delivered), Stage.FAILED) is False
    assert delivered.lre["disposition"] == "delivered"
    never_taken = _World(disposition="approved", state="failed")
    never_taken.sms_failed = True
    assert await _advance(_Conn(never_taken), Stage.FAILED) is False
    assert never_taken.lre["disposition"] == "approved"


@sync
async def test_a_failure_receipt_that_beat_the_acceptance_is_applied_at_acceptance():
    world = _World(disposition="staged", state="succeeded")
    world.sms_failed = True
    conn = _Conn(world)
    await _advance(conn, Stage.ACCEPTED)
    assert await ol.reconcile_command_delivery(conn, tenant_id=TENANT, command_id=COMMAND) is False
    assert world.lre["disposition"] == "failed"
    assert world.usage == {}


@sync
async def test_only_an_answered_call_reaches_the_lifecycle(monkeypatch):
    import commands_api as ca

    answered: list[str] = []

    async def _answered(call_id):
        answered.append(call_id)

    async def _session(*a, **k):
        return None

    monkeypatch.setattr(ca, "_record_call_answered", _answered)
    monkeypatch.setattr(ca, "_update_call_session_async", _session)
    await ca.record_call_outcome("call-1", "answered")
    await ca.record_call_outcome("call-2", "no-answer")
    await ca.record_call_outcome("call-3", "completed", duration_seconds=0)
    assert answered == ["call-1"]


def test_approval_and_rejection_move_the_first_response():
    import inspect

    import commands_api as ca

    assert "Stage.APPROVED" in inspect.getsource(ca.release_command)
    # Reject and a correction both cancel through withdraw_command (L05-07).
    assert "withdraw_command" in inspect.getsource(ca.reject_command)
    assert "Stage.CANCELLED" in inspect.getsource(ca.withdraw_command)


# ── A reply is stamped on the first response (0158, L04-03) ─────────────────

@sync
async def test_a_reply_with_no_client_or_contact_links_nothing():
    class _NoConn:
        def transaction(self):
            raise AssertionError("must not touch the database")

    assert await ol.record_reply(_NoConn(), tenant_id=TENANT, sms_message_id="m1", client_id=None,
                                 contact_id=None, received_at=datetime.now(timezone.utc)) is False


def test_the_reply_statement_is_bounded_and_never_answers_a_failed_response():
    sql = ol._REPLY_SQL
    # One row, chosen by a scalar subquery (evaluated once, not re-run per row).
    assert "WHERE id = (SELECT" in sql and "LIMIT 1" in sql and "IN (SELECT" not in sql
    assert "replied_at IS NULL" in sql.split("RETURNING")[0].rsplit("LIMIT 1", 1)[1]
    assert "disposition = ANY($6::text[])" in sql
    assert "failed" not in ol.ACCEPTED_DISPOSITIONS and "cancelled" not in ol.ACCEPTED_DISPOSITIONS
    assert "disposition" not in sql.split("WHERE id =")[0], "a reply never changes the disposition"
    assert ol.REPLY_WINDOW_DAYS == 7


def test_an_inbound_text_reaches_the_reply_after_its_own_commit():
    import inspect

    import messaging_data

    src = inspect.getsource(messaging_data.record_inbound_message)
    assert "replied = not opted_out" in src
    call = src.index("outreach_lifecycle.record_reply")
    agent_tx = src.index("async with tenant_tx(ctx)")
    platform_tx = src.index("async with tenant_tx(_platform_context())")
    assert agent_tx < platform_tx < call, "stamped in its own platform transaction, after the insert commits"
