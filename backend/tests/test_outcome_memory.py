"""Outcome Memory's honesty rules.

An outcome is a fact about the world that arrives after the decision it might
reward. These tests pin the properties that keep the join between them honest —
because the failure mode is not a crash, it is a rate that looks fitted and is
not.

1. **Recording never breaks the thing it records.** A closing has closed
   whether or not we managed to note it.
2. **The base rate is a row, not an absence.** An outcome that followed nothing
   Neoh did is written as examined-with-no-credit. Without that row,
   "unattributed" and "not yet looked at" are the same value and every interval
   the twin computes has a numerator and no denominator.
3. **Last touch, not fan-out.** One closing must not credit five cards.
4. **Valence is derived, never supplied.** A loss cannot be filed as a win.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

import outcome_memory as om
from tenancy import Role, TenantContext


TENANT = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT, role=Role.BROKER_OWNER)
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
SRC = "22222222-2222-4222-8222-222222222222"


class _Conn:
    """Records every statement so the tests can assert on what was written."""

    def __init__(self, *, fetchrow_results=None, raises=False):
        self.statements: list[tuple[str, tuple]] = []
        self._fetchrow_results = list(fetchrow_results or [])
        self._raises = raises
        self.savepoints = 0

    @asynccontextmanager
    async def transaction(self):
        self.savepoints += 1
        yield

    async def fetchrow(self, query, *args):
        if self._raises:
            raise RuntimeError("constraint violation")
        self.statements.append((query, args))
        if self._fetchrow_results:
            return self._fetchrow_results.pop(0)
        return None

    async def fetchval(self, query, *args):
        self.statements.append((query, args))
        return None

    async def fetch(self, query, *args):
        self.statements.append((query, args))
        return []

    async def execute(self, query, *args):
        self.statements.append((query, args))
        return "UPDATE 1"


def _run(coro):
    return asyncio.run(coro)


# ── vocabulary ──────────────────────────────────────────────────────────────

def test_valence_is_derived_from_kind_and_cannot_be_supplied():
    """Mirrors the GENERATED column. If this and the CHECK ever disagree, the
    write-back to agent_decisions would file a loss as a win."""
    assert om.valence_of("no_show") == -1
    assert om.valence_of("transaction_lost") == -1
    assert om.valence_of("contact_suppressed") == -1
    assert om.valence_of("transaction_closed") == 1
    assert om.valence_of("reply_received") == 1


def test_every_kind_has_an_attribution_window():
    """A kind without a window would silently fall back to a default that
    nobody chose for it."""
    assert set(om.ATTRIBUTION_WINDOWS) == set(om.OUTCOME_KINDS)
    for kind, window in om.ATTRIBUTION_WINDOWS.items():
        assert timedelta(days=1) <= window <= timedelta(days=120), kind


def test_slow_outcomes_get_longer_windows_than_fast_ones():
    """A closing takes months; a reply takes days. Using one window for both
    either credits last quarter's card for an organic sale or credits nothing
    for a slow deal."""
    assert om.ATTRIBUTION_WINDOWS["transaction_closed"] > om.ATTRIBUTION_WINDOWS["reply_received"] * 3


# ── recording ───────────────────────────────────────────────────────────────

def test_unknown_kind_is_refused_before_the_database():
    conn = _Conn()
    assert _run(om.record_outcome(
        CTX, outcome_kind="won_the_lottery", subject_type="client", subject_id="c1",
        source_table="t", source_id=SRC, occurred_at=NOW, conn=conn)) is None
    assert conn.statements == []


def test_recording_uses_a_savepoint_when_handed_the_callers_connection():
    """A failed bookkeeping statement would otherwise abort the caller's whole
    transaction — and the close that produced this outcome would fail to commit
    because its receipt did."""
    conn = _Conn(fetchrow_results=[{"id": SRC}])
    result = _run(om.record_outcome(
        CTX, outcome_kind="transaction_closed", subject_type="transaction",
        subject_id="t1", client_id="c1", source_table="transactions",
        source_id=SRC, occurred_at=NOW, outcome_value=485000, conn=conn))
    assert result == SRC
    assert conn.savepoints == 1


def test_recording_never_raises_into_the_caller():
    """The contract every emitter relies on. The exception is logged; the
    caller's transaction is untouched."""
    conn = _Conn(raises=True)
    result = _run(om.record_outcome(
        CTX, outcome_kind="reply_received", subject_type="client", subject_id="c1",
        source_table="interaction_logs", source_id=SRC, occurred_at=NOW, conn=conn))
    assert result is None


def test_duplicate_recording_returns_none_not_a_second_row():
    """ON CONFLICT DO NOTHING returns no row; the caller learns nothing and
    needs to learn nothing. Replaying a webhook must not double-count."""
    conn = _Conn(fetchrow_results=[None])
    assert _run(om.record_outcome(
        CTX, outcome_kind="showing_held", subject_type="client", subject_id="c1",
        source_table="showings", source_id=SRC, occurred_at=NOW, conn=conn)) is None
    insert, _ = conn.statements[-1]
    assert "ON CONFLICT" in insert and "DO NOTHING" in insert


def test_outcome_value_is_stored_raw():
    """purchase_price, not the commission on it. The rate is expected_value's
    business; storing a derived figure bakes today's rate into history."""
    conn = _Conn(fetchrow_results=[{"id": SRC}])
    _run(om.record_outcome(
        CTX, outcome_kind="transaction_closed", subject_type="transaction",
        subject_id="t1", client_id="c1", source_table="transactions",
        source_id=SRC, occurred_at=NOW, outcome_value=485000.0, conn=conn))
    _, args = conn.statements[-1]
    assert 485000.0 in args


def test_naive_occurred_at_is_treated_as_utc_not_rejected():
    conn = _Conn(fetchrow_results=[{"id": SRC}])
    _run(om.record_outcome(
        CTX, outcome_kind="no_show", subject_type="client", subject_id="c1",
        source_table="showings", source_id=SRC,
        occurred_at=datetime(2026, 9, 2, 12, 0), conn=conn))
    _, args = conn.statements[-1]
    stamped = next(a for a in args if isinstance(a, datetime))
    assert stamped.tzinfo is not None


# ── attribution ─────────────────────────────────────────────────────────────

def _outcome_row(kind="reply_received", subject_type="client", subject_id="c1", client_id=None):
    return {
        "id": SRC, "outcome_kind": kind, "subject_type": subject_type,
        "subject_id": subject_id, "client_id": client_id,
        "outcome_value": None, "occurred_at": NOW, "source_table": "interaction_logs",
    }


def test_an_outcome_that_followed_nothing_is_still_marked_examined():
    """The most important property in this file.

    That row IS the base rate. Skipping it would leave "unattributed" and "not
    yet looked at" as the same value, and every rate downstream would have a
    numerator and no denominator.
    """
    conn = _Conn()   # every lookup returns None: nothing matched
    result = _run(om._attribute_one(CTX, conn, _outcome_row()))
    assert result["attributed_to"] is None
    final = conn.statements[-1][0]
    assert "UPDATE outcome_events" in final
    assert "attributed_at = now()" in final
    assert "attributed_at IS NULL" in final, "must be enrich-in-place, never a rewrite"


def test_last_touch_prefers_the_later_of_decision_and_command():
    """One closing must not credit both. The closer cause wins."""
    earlier = NOW - timedelta(days=5)
    later = NOW - timedelta(days=1)
    conn = _Conn(fetchrow_results=[
        {"id": "dec-1", "decided_at": earlier},                       # decision
        {"trace_id": "tr-1", "approval_id": SRC, "decided_at": later},  # command
    ])
    result = _run(om._attribute_one(CTX, conn, _outcome_row()))
    assert result["attributed_to"] == "trace"
    # And the decision was NOT written to.
    assert not any("UPDATE agent_decisions" in q for q, _ in conn.statements)


def test_decision_write_back_is_guarded_and_carries_valence():
    conn = _Conn(fetchrow_results=[
        {"id": "dec-1", "decided_at": NOW - timedelta(days=1)},   # decision
        None,                                                     # no command
    ])
    _run(om._attribute_one(CTX, conn, _outcome_row(kind="no_show")))
    update = next(q for q, _ in conn.statements if "UPDATE agent_decisions" in q)
    assert "result_kind IS NULL" in update, "enrich in place, never rewrite"
    _, args = next((q, a) for q, a in conn.statements if "UPDATE agent_decisions" in q)
    assert -1 in args, "a no_show must reach the twin as a negative result"


def test_attribution_never_carries_a_tenant_predicate():
    """RLS scopes these reads. Repeating half the policy in the WHERE clause is
    the exact defect fixed in belief_store and intent_states this week."""
    import inspect

    for fn in (om._last_accepted_decision, om._last_approved_command):
        source = inspect.getsource(fn)
        assert "app_current_tenant()" not in source, fn.__name__
        assert "tenant_id = $" not in source, fn.__name__


def test_the_command_join_goes_through_the_approval_the_trace_keys_on():
    """ai_decision_traces.source_id is action_approvals.id — the only way to
    reach the person a command touched is trace → approval → command target."""
    conn = _Conn()
    _run(om._last_approved_command(conn, "client", "c1", None, NOW - timedelta(days=14), NOW))
    query, _ = conn.statements[-1]
    assert "c.approval_id = t.source_id" in query
    assert "c.state = 'succeeded'" in query, "a bounced message earned nothing"
    assert "t.revoked_at IS NULL" in query


def test_a_lead_outcome_is_retried_through_its_person():
    """A card accepted about the client can still have earned a lead's reply."""
    conn = _Conn(fetchrow_results=[
        None,                                                  # lead-scoped: nothing
        {"id": "dec-1", "decided_at": NOW - timedelta(days=2)},  # client-scoped: hit
        None,                                                  # command: nothing
    ])
    result = _run(om._attribute_one(
        CTX, conn, _outcome_row(subject_type="lead", subject_id="l1", client_id="c1")))
    assert result["attributed_to"] == "decision"


def test_sweep_isolates_one_bad_row_from_the_rest():
    """A single unparseable row must not stall a tenant's whole backlog."""
    class _BadConn(_Conn):
        async def fetch(self, query, *args):
            return [_outcome_row(), _outcome_row()]

        async def fetchrow(self, query, *args):
            raise RuntimeError("boom")

    class _Tx:
        def __init__(self, conn): self.conn = conn
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *a): return False

    conn = _BadConn()
    om.tenant_tx = lambda ctx: _Tx(conn)   # type: ignore[assignment]
    try:
        result = _run(om.attribute_pending(CTX, limit=10))
    finally:
        from db.connection import tenant_tx as real
        om.tenant_tx = real   # type: ignore[assignment]
    assert result["failed"] == 2
    assert result["examined"] == 0
