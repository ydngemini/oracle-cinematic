"""The capture layer that turns approval decisions into training signal.

These tests guard three properties that are easy to lose in a later refactor and
expensive to discover afterwards:

1. **Capture never breaks the decision.** An approval that a human granted must
   stand even if the trace insert explodes. The alternative — refusing to send
   an approved message because a training-corpus write failed — is strictly
   worse than losing one example.
2. **The signal is derived from what changed, not from what the caller says.**
   An "approve" that round-trips an unmodified draft through an edit box is an
   acceptance, not a preference pair.
3. **accepted_unchanged never carries a `final`.** That column is what lets
   dataset assembly separate human corrections from the model's own output; if
   an acceptance can carry a payload, the self-poisoning filter silently stops
   working while still appearing to run.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

import decision_traces as dt
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
SOURCE_ID = "22222222-2222-4222-8222-222222222222"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)

DRAFT = {"body": "Hi Dana, are you still considering selling?", "to": "+13025550100"}
CORRECTED = {"body": "Hi Dana - following up on your Dover property.", "to": "+13025550100"}


class _Conn:
    """Captures the INSERT arguments so the derived columns can be asserted."""

    def __init__(self, *, returns_row=True, raises=False):
        self.args: tuple = ()
        self.query = ""
        self._returns_row = returns_row
        self._raises = raises

    async def fetchrow(self, query, *args):
        if self._raises:
            raise RuntimeError("constraint violation")
        self.query = query
        self.args = args
        return {"id": SOURCE_ID} if self._returns_row else None

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return [{"id": SOURCE_ID}]


def _patch(monkeypatch, conn):
    @asynccontextmanager
    async def _tx(_ctx):
        yield conn

    monkeypatch.setattr(dt, "tenant_tx", _tx)
    return conn


def _record(conn, **overrides):
    kwargs = {
        "surface": dt.SURFACE_APPROVAL,
        "action_type": "send_sms",
        "source_table": "action_approvals",
        "source_id": SOURCE_ID,
        "proposal": DRAFT,
        "decision": "approved",
        "decided_at": datetime.now(timezone.utc),
    }
    kwargs.update(overrides)
    return asyncio.run(dt.record_decision(CTX, **kwargs))


def _column(conn, name):
    """Positional args follow the INSERT column order in migration 0074."""
    order = [
        "tenant_id", "agent_id", "surface", "action_type", "risk_class",
        "model_version", "source_table", "source_id",
        "proposal", "proposal_sha256", "final", "final_sha256",
        "signal", "decided_at", "decision_latency_ms", "consent_version",
    ]
    return conn.args[order.index(name)]


# ---------------------------------------------------------------------------
# Signal derivation
# ---------------------------------------------------------------------------

def test_an_approval_with_no_edit_is_an_acceptance(monkeypatch):
    conn = _patch(monkeypatch, _Conn())
    _record(conn)
    assert _column(conn, "signal") == dt.SIGNAL_ACCEPTED
    assert _column(conn, "final") is None
    assert _column(conn, "final_sha256") is None


def test_an_approval_with_a_real_edit_is_a_preference_pair(monkeypatch):
    conn = _patch(monkeypatch, _Conn())
    _record(conn, final=CORRECTED)

    assert _column(conn, "signal") == dt.SIGNAL_EDITED
    assert _column(conn, "final") is not None
    assert _column(conn, "final_sha256") != _column(conn, "proposal_sha256")


def test_an_edit_identical_to_the_draft_is_not_a_preference_pair(monkeypatch):
    """A UI that round-trips an untouched draft through an edit box must not
    manufacture training signal out of nothing — the digests decide, not the
    presence of an `edited_payload` argument."""
    conn = _patch(monkeypatch, _Conn())
    _record(conn, final=dict(DRAFT))

    assert _column(conn, "signal") == dt.SIGNAL_ACCEPTED
    assert _column(conn, "final") is None


def test_key_order_does_not_make_an_edit(monkeypatch):
    """Canonical JSON means a re-serialised draft hashes the same."""
    conn = _patch(monkeypatch, _Conn())
    _record(conn, final={"to": DRAFT["to"], "body": DRAFT["body"]})
    assert _column(conn, "signal") == dt.SIGNAL_ACCEPTED


def test_a_rejection_never_stores_a_final_payload(monkeypatch):
    """There is no "what they wanted instead" for a rejection, and the 0074
    CHECK constraint refuses one. Dropping it here keeps the insert valid."""
    conn = _patch(monkeypatch, _Conn())
    _record(conn, decision="rejected", final=CORRECTED)

    assert _column(conn, "signal") == dt.SIGNAL_REJECTED
    assert _column(conn, "final") is None
    assert _column(conn, "final_sha256") is None


def test_an_expired_approval_is_recorded_but_is_not_evidence(monkeypatch):
    """Expiry means the human never engaged. Recording it keeps coverage
    honest; exporting it as a negative would teach the model that drafts nobody
    read were bad ones."""
    conn = _patch(monkeypatch, _Conn())
    _record(conn, decision="expired")

    assert _column(conn, "signal") == dt.SIGNAL_EXPIRED
    assert dt.SIGNAL_EXPIRED in dt.NON_EVIDENTIAL_SIGNALS
    assert dt.SIGNAL_REJECTED not in dt.NON_EVIDENTIAL_SIGNALS


@pytest.mark.parametrize(
    "decision,final,expected",
    [
        ("approved", None, dt.SIGNAL_ACCEPTED),
        ("approved", "different", dt.SIGNAL_EDITED),
        ("rejected", None, dt.SIGNAL_REJECTED),
        ("rejected", "different", dt.SIGNAL_REJECTED),
        ("expired", None, dt.SIGNAL_EXPIRED),
    ],
)
def test_derive_signal_truth_table(decision, final, expected):
    assert dt.derive_signal(
        decision=decision,
        proposal_digest="a" * 64,
        final_digest=("b" * 64) if final else None,
    ) == expected


# ---------------------------------------------------------------------------
# Capture must never break the action that produced it
# ---------------------------------------------------------------------------

def test_a_failing_insert_returns_none_rather_than_raising(monkeypatch):
    """The decision has already been committed and audited by the time this
    runs. Raising here would surface a training-infrastructure fault as a
    failed approval."""
    _patch(monkeypatch, _Conn(raises=True))
    assert _record(_Conn(raises=True)) is None


def test_a_duplicate_trace_is_not_an_error(monkeypatch):
    """ON CONFLICT DO NOTHING returns no row. That is idempotency, not failure —
    a retried decision must not look like a capture bug."""
    conn = _patch(monkeypatch, _Conn(returns_row=False))
    assert _record(conn) is None
    assert "ON CONFLICT" in conn.query


# ---------------------------------------------------------------------------
# Late-binding reward
# ---------------------------------------------------------------------------

def test_attaching_an_outcome_targets_only_unrewarded_traces(monkeypatch):
    """`outcome_kind IS NULL` in the WHERE clause makes the attach idempotent
    and stops a later re-scoring from overwriting the first observed outcome."""
    conn = _patch(monkeypatch, _Conn())
    ok = asyncio.run(
        dt.attach_outcome(
            CTX,
            source_table="action_approvals",
            source_id=SOURCE_ID,
            outcome_kind="offer_accepted",
            outcome_at=datetime.now(timezone.utc),
            outcome_source="transactions",
            outcome_value=1.0,
        )
    )
    assert ok
    assert "outcome_kind IS NULL" in conn.query
    assert "UPDATE ai_decision_traces" in conn.query


def test_attach_outcome_does_not_touch_the_decision_columns(monkeypatch):
    """The 0074 trigger rejects any change to proposal/final/signal/decided_at.
    This asserts the query never tries, so the trigger stays a backstop rather
    than the thing enforcing correctness at runtime."""
    conn = _patch(monkeypatch, _Conn())
    asyncio.run(
        dt.attach_outcome(
            CTX,
            source_table="action_approvals",
            source_id=SOURCE_ID,
            outcome_kind="deal_closed",
            outcome_at=datetime.now(timezone.utc),
            outcome_source="transactions",
        )
    )
    mutated = conn.query.split("WHERE")[0]
    for immutable in ("proposal", "final", "signal=", "decided_at"):
        assert immutable not in mutated


def test_revocation_preserves_the_row(monkeypatch):
    """Deleting would make an already-trained model's provenance unauditable;
    dataset assembly filters on revoked_at instead."""
    conn = _patch(monkeypatch, _Conn())
    count = asyncio.run(dt.revoke_traces_for_agent(CTX, "agent@tenant.test"))

    assert count == 1
    assert "SET revoked_at=now()" in conn.query
    assert "DELETE" not in conn.query.upper()
    assert "revoked_at IS NULL" in conn.query


# ---------------------------------------------------------------------------
# Wiring into the real decision path
# ---------------------------------------------------------------------------

def test_decide_approval_records_a_trace(monkeypatch):
    """The hook is in `decide_approval`, not in each of its six callers — a
    per-caller hook would silently miss whichever one is added next."""
    import approval_service

    now = datetime.now(timezone.utc)
    existing = {
        "id": SOURCE_ID,
        "status": "pending",
        "risk_class": "outreach",
        "action_type": "send_sms",
        "draft_payload": DRAFT,
        "requested_by": "neoh",
        "requested_at": now - timedelta(minutes=3),
        "expires_at": now + timedelta(hours=1),
    }
    decided = {**existing, "status": "approved", "decided_at": now, "decided_by": CTX.agent_id}

    class _ApprovalConn:
        async def fetchrow(self, query, *args):
            return existing if "FOR UPDATE" in query else decided

    @asynccontextmanager
    async def _tx(_ctx):
        yield _ApprovalConn()

    monkeypatch.setattr(approval_service, "tenant_tx", _tx)

    async def _noop_record(*_a, **_k):
        return None

    monkeypatch.setattr(approval_service.ledger, "record", _noop_record)

    captured: dict = {}

    async def _capture(ctx, **kwargs):
        captured.update(kwargs)
        return "trace-id"

    monkeypatch.setattr(approval_service, "record_decision", _capture)

    asyncio.run(
        approval_service.decide_approval(
            CTX,
            SOURCE_ID,
            decision="approved",
            reason="looks right, sending as drafted",
            edited_payload=CORRECTED,
        )
    )

    assert captured["surface"] == dt.SURFACE_APPROVAL
    assert captured["action_type"] == "send_sms"
    assert captured["final"] == CORRECTED
    # Measured from requested_at, ~3 minutes.
    assert 170_000 < captured["decision_latency_ms"] < 190_000


def test_the_recorded_decision_is_the_row_status_not_the_request(monkeypatch):
    """An approval that had already expired is written 'expired' by the UPDATE
    above. Recording the *requested* decision would put an approval in the
    corpus that never actually happened."""
    import inspect

    import approval_service

    source = inspect.getsource(approval_service.decide_approval)
    assert 'decision=approval["status"]' in source, (
        "the trace must record the persisted status, not the requested decision"
    )
