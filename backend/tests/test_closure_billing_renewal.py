"""Closing or reopening a brokerage reaches Stripe durably and says only what
Stripe confirmed (external audit 22.1).

Closure used to commit `closing`, then ask Stripe once: a timeout, recovery
mode, a worker without STRIPE_SECRET_KEY, or a crash between the commit and
the call left the subscription renewing while the owner read "Your
subscription will not renew"; a failed withdrawal left an active brokerage
cancelling at period end. Now each queues privacy:billing_renewal in its own
transaction; the job derives the wanted renewal from tenants.lifecycle_state
when it runs, sets it, confirms it with Subscription.retrieve, retries, and on
its final attempt records `divergent` for the external_cleanup alert. Stripe
is a stub throughout; no real call is made.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
import stripe

import automation_jobs
import privacy_lifecycle as pl
import recovery_mode
from tenancy import Role, TenantContext

T = "00000000-0000-0000-0000-0000000000bb"
OWNER = TenantContext(agent_id="owner@x.test", tenant_id=T, role=Role.BROKER_OWNER)


def _events(caplog, name):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "oracle.privacy.events" and json.loads(r.getMessage())["event"] == name]


# ── request_closure / withdraw_closure: the job joins the transaction ──────

class _ClosureConn:
    """The tenants row and the brokerage's LATEST closure operation (its
    state, freeze list and error). A read of privacy_operations filtered on
    state='running' sees that operation only while it is running; any other
    state filter is refused, so a changed query has to change this fake.
    `held` are the freeze-list members the owner switched off since the
    closure (what _SWITCHED_OFF_SINCE_SQL finds); `route_agents` maps a route
    id to the agent holding it now."""

    REQUESTED_AT = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def __init__(self, lifecycle, op_id="op-1", op_state="running", freeze=None, error=None,
                 held=(), route_agents=None):
        self.lifecycle, self.executed = lifecycle, []
        self.op_id, self.op_state, self.error = op_id, op_state, error
        self.freeze = freeze if freeze is not None else {}
        self.held, self.route_agents = list(held), dict(route_agents or {})
        self.routes_on: list[str] = []
        self.held_reads = 0

    def _op(self, sql):
        where = sql.split("WHERE", 1)[1]
        assert "tenant_id=$1" in where and "kind='closure'" in where and "requested_at DESC" in where
        if "state" in where:
            assert "state='running'" in where, sql
            if self.op_state != "running":
                return None
        return self.op_id and {"id": self.op_id, "state": self.op_state, "error": self.error,
                               "requested_at": self.REQUESTED_AT,
                               "progress": json.dumps({"freeze": self.freeze})}

    async def fetchrow(self, sql, *args):
        if "FROM tenants" in sql:
            return {"name": "Brokerage B", "lifecycle_state": self.lifecycle}
        if "INSERT INTO privacy_operations" in sql:
            return {"id": "op-1"}
        if "FROM privacy_operations" in sql:      # withdraw: the closure being withdrawn
            return self._op(sql)
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "FROM privacy_operations" in sql:      # withdraw: the latest closure's id
            op = self._op(sql)
            return op["id"] if op else None
        return self.lifecycle

    async def fetch(self, sql, *args):
        if "team_memberships" in sql:              # _switched_off_since
            assert args[0] == T and args[2] == self.REQUESTED_AT
            self.held_reads += 1
            return [dict(h) for h in self.held if h["id"] in args[1]]
        if "lower(agent_id) = ANY($3::text[])" in sql:     # the routes those members hold
            return [(r,) for r in args[1] if self.route_agents.get(r, "").lower() in args[2]]
        return []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "SET is_active=true" in sql:
            return f"UPDATE {len(args[1])}"
        if "SET active=true" in sql:
            held = args[2] if len(args) > 2 else []
            on = [r for r in args[1] if self.route_agents.get(r, "").lower() not in held]
            self.routes_on += on
            return f"UPDATE {len(on)}"
        return "UPDATE 0"

    def restored(self, table):
        [args] = [a for sql, a in self.executed if f"UPDATE {table} SET" in sql and "=true" in sql]
        assert args[0] == T
        return list(args[1])

    def finished(self):
        """(state, result, error) of every _finish_operation call."""
        return [(a[0], a[1], json.loads(a[2]), a[4]) for sql, a in self.executed
                if "SET state=$2, result=result ||" in sql]


def _act(monkeypatch, action, *, lifecycle, billing=None, enqueue_error=None, fast_error=None, conn=None):
    conn, state = conn or _ClosureConn(lifecycle), {"in_tx": False}

    @asynccontextmanager
    async def tx(_ctx):
        state["in_tx"] = True
        try:
            yield conn
        finally:
            state["in_tx"] = False

    queued, fast = [], []

    async def enqueue(c, ctx, **kw):
        if enqueue_error:
            raise enqueue_error
        queued.append({"in_tx": state["in_tx"], "conn": c, **kw})
        return {"id": "job"}, True

    async def reconcile(tenant_id, **kw):
        fast.append((tenant_id, kw))
        if fast_error:
            raise fast_error
        return billing

    async def audit(*_a, **_k):
        return None

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(automation_jobs, "enqueue_job_in_tx", enqueue)
    monkeypatch.setattr(pl, "reconcile_billing_renewal", reconcile)
    monkeypatch.setattr(pl, "_audit", audit)
    if action == "close":
        out = asyncio.run(pl.request_closure(OWNER, confirm_name="Brokerage B", reason="closing"))
    else:
        out = asyncio.run(pl.withdraw_closure(OWNER, reason="changed my mind"))
    return out, conn, queued, fast


def test_closure_queues_the_billing_job_in_its_transaction_and_says_pending(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    before = datetime.now(timezone.utc)
    out, conn, queued, fast = _act(monkeypatch, "close", lifecycle="active",
                                   billing={"state": "pending", "detail": "failed:TimeoutError"})
    assert out["state"] == "closing" and out["billing"] == "pending"     # never "will not renew"
    [job] = queued
    assert job["in_tx"] and job["job_type"] == "privacy:billing_renewal" == pl.JOB_BILLING_RENEWAL
    assert job["idempotency_key"] == "privacy:billing:op-1:close"
    assert job["payload"] == {"operation_id": "op-1"}
    assert job["max_attempts"] == pl.BILLING_MAX_ATTEMPTS and job["scheduled_at"] > before
    progress = next(json.loads(a[1]) for sql, a in conn.executed if "SET progress=$2::jsonb" in sql)
    assert progress["billing"] == {"desired": "stop_renewal", "state": "pending"}
    assert fast == [(T, {"timeout": pl.INLINE_STRIPE_TIMEOUT_SECONDS})]
    assert _events(caplog, "deletion.scheduled")[0]["billing_pending"] is True


def test_closure_says_confirmed_only_when_stripe_confirmed(monkeypatch):
    out, *_ = _act(monkeypatch, "close", lifecycle="active", billing={"state": "confirmed"})
    assert out["billing"] == "confirmed"
    out, *_ = _act(monkeypatch, "close", lifecycle="active", billing={"state": "no_subscription"})
    assert out["billing"] == "no_subscription"


def test_an_inline_attempt_that_raises_is_pending(monkeypatch):
    out, _c, queued, _f = _act(monkeypatch, "close", lifecycle="active", fast_error=ConnectionError("db"))
    assert out["billing"] == "pending" and len(queued) == 1


def test_a_closure_whose_job_cannot_be_queued_does_not_happen(monkeypatch):
    with pytest.raises(RuntimeError, match="queue down"):
        _act(monkeypatch, "close", lifecycle="active", enqueue_error=RuntimeError("queue down"))


def test_withdrawal_queues_its_own_job_and_records_renew_pending(monkeypatch):
    out, conn, queued, _ = _act(monkeypatch, "withdraw", lifecycle="closing",
                                billing={"state": "pending", "detail": "stripe_not_configured"})
    assert out["state"] == "active" and out["billing"] == "pending"
    [job] = queued
    assert job["in_tx"] and job["idempotency_key"] == "privacy:billing:op-1:withdraw"
    marked = next(a for sql, a in conn.executed if "jsonb_set(progress, '{billing}'" in sql)
    assert marked[0] == "op-1" and json.loads(marked[1]) == {"desired": "renew", "state": "pending"}


def test_withdrawing_a_closure_blocked_by_a_legal_hold_records_on_that_closure(monkeypatch):
    """No running closure (an erasure left it 'blocked_legal_hold'): the
    pending mark and the job key go to the latest closure operation, the one
    being withdrawn and the one tenant_lifecycle reads (review of 22.1)."""
    conn = _ClosureConn("closing", op_id="op-blocked", op_state="blocked_legal_hold", error="legal hold h-1")
    out, conn, queued, _ = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                                billing={"state": "confirmed"})
    assert out["billing"] == "confirmed"
    [job] = queued
    assert job["idempotency_key"] == "privacy:billing:op-blocked:withdraw"
    assert job["payload"] == {"operation_id": "op-blocked"}
    marked = next(a for sql, a in conn.executed if "jsonb_set(progress, '{billing}'" in sql)
    assert marked[0] == "op-blocked"


# ── withdrawal restores exactly what the closure froze (L17-58) ────────────
# A closure an erasure left 'blocked_legal_hold' (a hold stopped the erasure;
# the brokerage stays 'closing') used to be withdrawn with nothing restored:
# withdraw_closure read the freeze list only from a running closure, so the
# brokerage came back 'active' with its agents still signed out and its
# numbers still off, and the operation stayed 'blocked_legal_hold'.

_FREEZE = {"signed_out_user_ids": ["u-1", "u-2"],
           "routes": {"telephony_routes": ["tr-1"], "messaging_routes": ["mr-1", "mr-2"]}}


def test_withdrawing_a_closure_blocked_by_a_legal_hold_restores_what_it_froze(monkeypatch):
    conn = _ClosureConn("closing", op_id="op-blocked", op_state="blocked_legal_hold",
                        freeze=_FREEZE, error="legal hold h-1")
    out, conn, queued, _ = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                                billing={"state": "confirmed"})
    assert out["state"] == "active"
    assert conn.restored("users") == ["u-1", "u-2"]
    assert conn.restored("telephony_routes") == ["tr-1"]
    assert conn.restored("messaging_routes") == ["mr-1", "mr-2"]
    # Finished as a withdrawn closure is ('cancelled'), the hold still on record.
    [(op_id, state, result, error)] = conn.finished()
    assert (op_id, state) == ("op-blocked", "cancelled")
    assert result == {"withdrawn_by": OWNER.agent_id, "withdraw_reason": "changed my mind",
                      "withdrawn_from": "blocked_legal_hold"}
    assert error == "legal hold h-1"
    # The hold itself is not lifted: only an operator releases it.
    assert not any("legal_holds" in sql for sql, _a in conn.executed)
    # Only this closure is rewritten (by id), never every blocked one.
    writes = [" ".join(sql.split()) for sql, _a in conn.executed if "UPDATE privacy_operations" in sql]
    assert writes and all("WHERE id=$1" in sql for sql in writes), writes
    # #183's billing record and job are unchanged.
    [job] = queued
    assert job["idempotency_key"] == "privacy:billing:op-blocked:withdraw"
    assert out["restored_users"] == 2 and out["restored_routes"] == 3


def test_withdrawing_a_running_closure_still_restores_and_cancels_it(monkeypatch):
    conn = _ClosureConn("closing", op_id="op-1", op_state="running", freeze=_FREEZE)
    out, conn, _q, _f = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                             billing={"state": "confirmed"})
    assert conn.restored("users") == ["u-1", "u-2"]
    [(op_id, state, result, error)] = conn.finished()
    assert (op_id, state, error) == ("op-1", "cancelled", None)
    assert "withdrawn_from" not in result
    assert out["restored_users"] == 2 and out["restored_routes"] == 3


@pytest.mark.parametrize("stale", ["succeeded", "failed", "cancelled"])
def test_a_latest_closure_that_is_not_open_restores_nothing(monkeypatch, stale):
    """Only request_closure moves a brokerage to 'closing', and it creates the
    running closure in the same transaction, so this is reachable only by hand.
    Its freeze list is not this closure's, so nothing comes back from it, and
    it is not rewritten; the brokerage still reopens (refusing would leave it
    to be erased against the owner's word) and the counts say what happened."""
    conn = _ClosureConn("closing", op_id="op-old", op_state=stale, freeze=_FREEZE)
    out, conn, queued, _ = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                                billing={"state": "confirmed"})
    assert out["state"] == "active"
    assert conn.restored("users") == [] and conn.restored("telephony_routes") == []
    assert conn.restored("messaging_routes") == []
    assert conn.finished() == []
    [job] = queued                        # the renewal record still goes to the latest closure (#183)
    assert job["idempotency_key"] == "privacy:billing:op-old:withdraw"
    assert out["restored_users"] == 0 and out["restored_routes"] == 0


def test_no_closure_operation_restores_nothing(monkeypatch):
    conn = _ClosureConn("closing", op_id=None)
    out, conn, queued, _ = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                                billing={"state": "confirmed"})
    assert conn.finished() == []
    [job] = queued
    assert job["payload"] == {"operation_id": None}
    assert out["restored_users"] == 0 and out["restored_routes"] == 0


@pytest.mark.parametrize("lifecycle", ["erasing", "erased", "active", "suspended"])
def test_a_withdrawal_outside_the_grace_period_changes_nothing(monkeypatch, lifecycle):
    """Once begin_erasure has moved the brokerage to 'erasing' (it locks the
    same tenants row), or after a first withdrawal ('active'), a withdrawal
    is refused before anything is restored, finished or queued."""
    conn = _ClosureConn(lifecycle, op_id="op-1", op_state="running", freeze=_FREEZE)
    with pytest.raises(pl.LifecycleError, match="grace period"):
        _act(monkeypatch, "withdraw", lifecycle=lifecycle, conn=conn)
    assert conn.executed == []


# Review of PR #185: an owner may reinstate a frozen agent while the brokerage
# is closing and then suspend them; after an older withdrawal that restored
# nothing, they may have offboarded one (which parks the number). The
# withdrawal used to switch every freeze-list member and route back on.

_TWO = {"signed_out_user_ids": ["u-1", "u-2"],
        "routes": {"telephony_routes": ["tr-1", "tr-2"], "messaging_routes": ["mr-1", "mr-2"]}}
_AGENTS = {"tr-1": "one@x.test", "mr-1": "one@x.test", "tr-2": "Two@x.test", "mr-2": "two@x.test"}


@pytest.mark.parametrize("op_state", ["running", "blocked_legal_hold"])
@pytest.mark.parametrize("why", ["suspended", "offboarded"])
def test_a_member_switched_off_since_the_closure_stays_off_with_their_numbers(monkeypatch, op_state, why):
    conn = _ClosureConn("closing", op_state=op_state, freeze=_TWO, route_agents=_AGENTS,
                        held=[{"id": "u-2", "agent_id": "two@X.test", "reason": why}])
    out, conn, _q, _f = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                             billing={"state": "confirmed"})
    assert conn.restored("users") == ["u-1"]                       # u-2 stays off
    assert sorted(conn.routes_on) == ["mr-1", "tr-1"]              # and the numbers u-2 holds
    assert out["skipped_users"] == [{"id": "u-2", "reason": why}]
    assert sorted(out["skipped_routes"]) == ["mr-2", "tr-2"]
    [(_id, state, result, _e)] = conn.finished()                   # also on the operation's record
    assert state == "cancelled" and result["skipped_users"] == out["skipped_users"]
    assert sorted(result["skipped_routes"]) == ["mr-2", "tr-2"]
    assert out["restored_users"] == 1 and out["restored_routes"] == 2


def test_with_nobody_switched_off_since_everything_frozen_comes_back(monkeypatch):
    conn = _ClosureConn("closing", freeze=_TWO, route_agents=_AGENTS)
    out, conn, _q, _f = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                             billing={"state": "confirmed"})
    assert conn.held_reads == 1                                    # asked, found nobody
    assert conn.restored("users") == ["u-1", "u-2"]
    assert sorted(conn.routes_on) == ["mr-1", "mr-2", "tr-1", "tr-2"]
    assert out["skipped_users"] == [] and out["skipped_routes"] == []
    [(_id, _s, result, _e)] = conn.finished()
    assert "skipped_users" not in result


# ── an erasure finishes the closure it erases, blocked or not (L17-58) ─────
# After a hold was released, begin_erasure finished only a 'running' closure
# as 'succeeded', so a closure an earlier attempt had left 'blocked_legal_hold'
# stayed blocked while its brokerage was erased.

class _EraseConn:
    """begin_erasure's reads and writes, with the latest closure's state
    modelled: _finish_operation (by id) and 0112123's tenant-wide
    "SET state='succeeded' ... WHERE state='running'" both change it."""

    def __init__(self, closure_state="blocked_legal_hold", *, hold=None, existing=None,
                 error="legal hold h-1"):
        self.lifecycle, self.hold, self.existing = "closing", hold, existing
        self.closure = ({"id": "op-c", "state": closure_state, "error": error, "result": {}}
                        if closure_state else None)
        self.created, self.closure_at_create = [], None

    async def fetchval(self, sql, *args):
        if "FROM tenants" in sql:
            return self.lifecycle
        if "kind='erasure'" in sql:
            return self.existing
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "FROM legal_holds" in sql:
            return self.hold
        if "INSERT INTO privacy_operations" in sql:
            self.created.append(args[1])
            self.closure_at_create = self.closure and self.closure["state"]
            return {"id": "op-e"}
        if "FROM privacy_operations" in sql and "kind='closure'" in sql:
            where = sql.split("WHERE", 1)[1]
            assert "requested_at DESC" in where and "state" not in where, sql
            return self.closure and {k: self.closure[k] for k in ("id", "state", "error")}
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        flat = " ".join(sql.split())
        c = self.closure
        if "SET state=$2, result=result ||" in flat:                # _finish_operation
            assert c is not None and args[0] == c["id"], args
            c.update(state=args[1], result={**c["result"], **json.loads(args[2])}, error=args[4])
        elif "SET state='succeeded'" in flat and "state='running'" in flat:
            if c and c["state"] == "running":
                c["state"] = "succeeded"
        elif "SET state='blocked_legal_hold'" in flat:
            where = flat.split("WHERE", 1)[1]
            hits = ("running", "blocked_legal_hold") if "blocked_legal_hold" in where else ("running",)
            if c and c["state"] in hits:
                c.update(state="blocked_legal_hold", error="legal hold " + args[1])
        elif flat.startswith("UPDATE tenants SET lifecycle_state='erasing'"):
            self.lifecycle = "erasing"
        else:
            raise AssertionError(sql)
        return "UPDATE 1"


def _erase(monkeypatch, conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    queued = []

    async def enqueue(_ctx, **kw):
        queued.append(kw)
        return {"id": "job"}, True

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(automation_jobs, "enqueue_job", enqueue)
    return asyncio.run(pl.begin_erasure(T, requested_by="retention-scheduler")), queued


def test_an_erasure_after_a_released_hold_finishes_the_closure_it_blocked(monkeypatch):
    conn = _EraseConn("blocked_legal_hold")
    op_id, queued = _erase(monkeypatch, conn)
    assert conn.closure["state"] == "succeeded"
    assert conn.closure["result"] == {"erasure_started_from": "blocked_legal_hold"}
    assert conn.closure["error"] == "legal hold h-1"            # the hold stays on record
    assert conn.closure_at_create == "succeeded" and conn.created == ["erasure"]
    assert op_id == "op-e" and conn.lifecycle == "erasing"
    [job] = queued
    assert job["payload"] == {"operation_id": "op-e"}


def test_an_erasure_finishes_a_running_closure_as_before(monkeypatch):
    conn = _EraseConn("running", error=None)
    op_id, _ = _erase(monkeypatch, conn)
    assert conn.closure_at_create == "succeeded"                # before the erasure exists
    assert conn.closure["result"] == {} and conn.closure["error"] is None
    assert op_id == "op-e" and conn.lifecycle == "erasing"


@pytest.mark.parametrize("stale", ["succeeded", "failed", "cancelled"])
def test_an_erasure_never_rewrites_a_closure_that_is_not_current(monkeypatch, stale):
    conn = _EraseConn(stale, error="earlier")
    op_id, _ = _erase(monkeypatch, conn)
    assert conn.closure == {"id": "op-c", "state": stale, "error": "earlier", "result": {}}
    assert op_id == "op-e"


def test_an_erasure_under_a_hold_blocks_the_closure_and_starts_nothing(monkeypatch):
    conn = _EraseConn("running", hold={"id": "h-2"}, error=None)
    op_id, queued = _erase(monkeypatch, conn)
    assert op_id is None and queued == [] and conn.created == []
    assert conn.closure["state"] == "blocked_legal_hold" and conn.closure["error"] == "legal hold h-2"
    assert conn.lifecycle == "closing"


def test_a_second_erasure_pass_under_a_standing_hold_changes_nothing(monkeypatch):
    """The scheduler comes back while the hold still stands: the closure an
    earlier pass blocked stays exactly as it is, and nothing starts."""
    conn = _EraseConn("blocked_legal_hold", hold={"id": "h-2"})
    op_id, queued = _erase(monkeypatch, conn)
    assert op_id is None and queued == [] and conn.created == []
    assert conn.closure == {"id": "op-c", "state": "blocked_legal_hold", "error": "legal hold h-1",
                            "result": {}}
    assert conn.lifecycle == "closing"


def test_the_scheduler_erases_the_others_when_one_brokerage_is_no_longer_closing(monkeypatch, caplog):
    """Withdrawn between the scheduler's read and begin_erasure's lock: that
    brokerage raises LifecycleError ('tenant is active'); the pass used to
    stop there and leave every later brokerage for the next run."""
    class _Due:
        async def fetch(self, sql, *args):
            assert "lifecycle_state='closing'" in sql and "erase_after <= now()" in sql
            return [("t-1",), ("t-2",), ("t-3",)]

    @asynccontextmanager
    async def tx(_ctx):
        yield _Due()

    seen = []

    async def begin(tenant_id, *, requested_by):
        seen.append((tenant_id, requested_by))
        if tenant_id == "t-1":
            raise pl.LifecycleError("tenant is active; only a closing tenant is erased")
        return {"t-2": "op-2", "t-3": None}[tenant_id]

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "begin_erasure", begin)
    with caplog.at_level(logging.WARNING, logger=pl.log.name):
        started = asyncio.run(pl.start_due_erasures())
    assert started == ["op-2"]
    assert [t for t, _ in seen] == ["t-1", "t-2", "t-3"]
    assert any("t-1" in r.getMessage() and "tenant is active" in r.getMessage() for r in caplog.records)


def test_a_resumed_erasure_finishes_a_closure_older_code_left_blocked(monkeypatch):
    conn = _EraseConn("blocked_legal_hold", existing="op-e0")
    op_id, queued = _erase(monkeypatch, conn)
    assert op_id == "op-e0" and conn.created == []               # no second erasure
    assert conn.closure["state"] == "succeeded"
    assert conn.closure["result"] == {"erasure_started_from": "blocked_legal_hold"}
    assert queued[0]["payload"] == {"operation_id": "op-e0"}


# ── the convergence itself, against a Stripe stub ───────────────────────────

class _Stripe:
    def __init__(self, cancel_at_period_end=False):
        self.cancel = cancel_at_period_end
        self.up, self.lie, self.calls = True, False, []

    def modify(self, sub, *, cancel_at_period_end, max_network_retries):
        self.calls.append(("modify", sub, cancel_at_period_end))
        if not self.up:
            raise stripe.APIConnectionError("stripe unreachable")
        if not self.lie:
            self.cancel = cancel_at_period_end
        return {"id": sub, "cancel_at_period_end": cancel_at_period_end}

    def retrieve(self, sub, *, max_network_retries):
        self.calls.append(("retrieve", sub))
        if not self.up:
            raise stripe.APIConnectionError("stripe unreachable")
        return {"id": sub, "cancel_at_period_end": self.cancel}


class _Db:
    """tenants.lifecycle_state, the subscription row and closure operations."""

    def __init__(self, lifecycle, sub="sub_live"):
        self.lifecycle, self.sub = lifecycle, sub
        self.billing: dict[str, dict] = {}
        self.superseded = 0
        self.superseded_states: list[list[str]] = []
        self.ctxs: list = []

    async def fetchval(self, sql, *args):
        if "lifecycle_state FROM tenants" in sql:
            return self.lifecycle
        if "FROM subscriptions" in sql:
            return self.sub
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        assert "FROM privacy_operations" in sql and "kind='closure'" in sql
        return {"id": "op-latest", "desired": (self.billing.get("op-latest") or {}).get("desired")}

    async def execute(self, sql, *args):
        if "'\"superseded\"'" in sql:
            self.superseded += 1
            self.superseded_states.append(list(args[2]))
        elif "jsonb_set(progress, '{billing}'" in sql:
            self.billing[args[0]] = json.loads(args[1])
        return "UPDATE 1"


@pytest.fixture
def world(monkeypatch):
    db, fake = _Db("closing"), _Stripe()

    @asynccontextmanager
    async def tx(ctx):
        db.ctxs.append(ctx)
        yield db

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    monkeypatch.setattr(stripe, "api_key", "sk_test_" + "stub")
    monkeypatch.setattr(stripe.Subscription, "modify", fake.modify)
    monkeypatch.setattr(stripe.Subscription, "retrieve", fake.retrieve)
    return db, fake


class _Reporter:
    def __init__(self, attempt=1, max_attempts=12):
        self.job = {"id": "j", "tenant_id": T, "attempt_count": attempt, "max_attempts": max_attempts}

    async def progress(self, *_a, **_k):
        return None


def _run_job(attempt=1, max_attempts=12):
    return asyncio.run(pl._billing_renewal_job({"operation_id": "op-1"}, _Reporter(attempt, max_attempts)))


def test_the_job_stops_renewal_for_a_closing_brokerage_and_confirms_it(world):
    db, fake = world
    out = _run_job()
    assert out == {"state": "confirmed", "desired": "stop_renewal", "operation_id": "op-1"}
    assert fake.cancel is True
    assert fake.calls == [("modify", "sub_live", True), ("retrieve", "sub_live")]   # read back
    assert db.billing["op-latest"]["state"] == "confirmed" and db.billing["op-latest"]["desired"] == "stop_renewal"


def test_stripe_down_is_pending_and_retried_until_it_confirms(world):
    db, fake = world
    fake.up = False
    with pytest.raises(pl.BillingRenewalPending):
        _run_job(attempt=1)
    assert db.billing["op-latest"]["state"] == "pending"
    assert db.billing["op-latest"]["detail"].startswith("failed:")
    fake.up = True
    assert _run_job(attempt=2)["state"] == "confirmed" and fake.cancel is True


def test_stripe_not_configured_with_a_subscription_is_never_success(world, monkeypatch):
    db, fake = world
    monkeypatch.setattr(stripe, "api_key", None)
    with pytest.raises(pl.BillingRenewalPending, match="stripe_not_configured"):
        _run_job(attempt=1)
    assert fake.calls == [] and db.billing["op-latest"]["state"] == "pending"


def test_a_read_back_that_disagrees_is_not_confirmed(world):
    db, fake = world
    fake.lie = True              # Stripe accepted the call, the subscription did not change
    with pytest.raises(pl.BillingRenewalPending, match="unconfirmed"):
        _run_job()
    assert db.billing["op-latest"]["detail"] == "failed:unconfirmed"


def test_recovery_mode_is_retried_not_dead_lettered(world, monkeypatch):
    def refuse(*_a, **_k):
        raise recovery_mode.RecoveryModeBlocked("recovery mode")

    monkeypatch.setattr(recovery_mode, "guard", refuse)
    with pytest.raises(pl.BillingRenewalPending) as exc:
        _run_job()
    # the worker dead-letters RecoveryModeBlocked at once; renewal must converge later instead
    assert not isinstance(exc.value, recovery_mode.RecoveryModeBlocked)


@pytest.mark.parametrize("order", [("close", "withdraw"), ("withdraw", "close")])
def test_close_then_withdraw_converges_on_the_last_word_in_any_job_order(world, order):
    """Both jobs run after the withdrawal: each reads `active` and renews,
    whichever runs first — even after a late cancel landed at Stripe."""
    db, fake = world
    db.lifecycle = "active"
    fake.cancel = True
    for _job in order:
        assert _run_job()["desired"] == "renew"
    assert fake.cancel is False
    assert db.billing["op-latest"] == {**db.billing["op-latest"], "desired": "renew", "state": "confirmed"}


def test_the_final_attempt_records_divergent_and_raises_the_alert_event(world, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    db, fake = world
    fake.up = False
    out = _run_job(attempt=12)
    assert out["state"] == "divergent" and out["_terminal_state"] == "partial"
    assert db.billing["op-latest"]["state"] == "divergent"    # what external_cleanup counts
    [event] = _events(caplog, "closure.billing_divergent")
    assert event["tenant_id"] == T and event["kind"] == "stop_renewal"


def test_a_final_attempt_that_raises_still_records_divergent(world, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    db, _fake = world

    async def boom(*_a, **_k):
        raise ConnectionError("db")

    monkeypatch.setattr(pl, "_converge_subscription", boom)
    with pytest.raises(ConnectionError):
        _run_job(attempt=12)
    assert db.billing["op-latest"]["state"] == "divergent"
    assert _events(caplog, "closure.billing_divergent")


def test_no_subscription_is_settled_without_calling_stripe(world):
    db, fake = world
    db.sub = None
    assert _run_job()["state"] == "no_subscription" and fake.calls == []


def test_a_suspended_brokerage_is_left_alone(world):
    db, fake = world
    db.lifecycle = "suspended"
    assert _run_job()["state"] == "skipped" and fake.calls == []


def test_a_confirmation_settles_an_older_closures_divergence(world):
    db, _fake = world
    _run_job()
    assert db.superseded_states == [["pending", "divergent"]]
    db.superseded_states = []
    db.sub = None
    _run_job()                      # no_subscription settles too
    assert db.superseded_states == [["pending", "divergent"]]


def test_an_older_closures_pending_is_superseded_even_while_the_latest_retries(world):
    """Review of 22.1: close (Stripe down), withdraw (Stripe down), close
    again — every later job writes the newest closure, so the older
    'pending' could never be confirmed and paged forever once 6 h old."""
    db, fake = world
    fake.up = False
    with pytest.raises(pl.BillingRenewalPending):
        _run_job()
    assert db.billing["op-latest"]["state"] == "pending"
    assert db.superseded_states == [["pending"]]          # an older divergence stays until confirmed


def test_billing_reads_and_writes_bound_to_the_brokerage(world):
    """The tenants and subscriptions reads run under platform power bound to
    the brokerage (as the revoke job and _phase_providers do), so the row is
    readable even on the request pool."""
    db, _fake = world
    _run_job()
    assert db.ctxs and all(c.tenant_id == T and c.is_platform_admin for c in db.ctxs)


def test_an_unreadable_tenant_row_is_pending_never_success(world):
    db, fake = world
    db.lifecycle = None
    with pytest.raises(pl.BillingRenewalPending, match="tenant_unreadable"):
        _run_job(attempt=1)
    assert fake.calls == [] and db.billing["op-latest"]["state"] == "pending"
    out = _run_job(attempt=12)
    assert out["state"] == "divergent" and out["_terminal_state"] == "partial"


def test_a_final_attempt_that_raises_keeps_which_way_it_diverges(world, monkeypatch):
    """Review of 22.1: it recorded desired None, so the owner's banner
    (shown for stop_renewal) said nothing."""
    db, fake = world
    fake.up = False
    with pytest.raises(pl.BillingRenewalPending):
        _run_job(attempt=1)                               # records {stop_renewal, pending}

    async def boom(*_a, **_k):
        raise ConnectionError("db")

    monkeypatch.setattr(pl, "_converge_subscription", boom)
    with pytest.raises(ConnectionError):
        _run_job(attempt=12)
    assert db.billing["op-latest"]["state"] == "divergent"
    assert db.billing["op-latest"]["desired"] == "stop_renewal"   # from the previous record
    # with no previous record, from the brokerage's state
    db.billing.clear()
    db.lifecycle = "active"
    with pytest.raises(ConnectionError):
        _run_job(attempt=12)
    assert db.billing["op-latest"]["desired"] == "renew"


def test_the_handler_is_registered_as_a_privacy_job():
    pl.register()
    assert pl.JOB_BILLING_RENEWAL in automation_jobs.registered_handlers()
    assert pl.JOB_BILLING_RENEWAL.startswith("privacy:")   # runs for a closing tenant, never cancelled


# ── what the owner reads ────────────────────────────────────────────────────

def test_tenant_lifecycle_reports_the_renewal_state_not_a_promise(monkeypatch):
    class Conn:
        async def fetchrow(self, sql, *args):
            if "FROM tenants" in sql:
                return {"id": T, "name": "B", "lifecycle_state": "closing", "closure_requested_at": None,
                        "closure_requested_by": None, "erase_after": None, "erased_at": None}
            return None      # no legal hold

        async def fetchval(self, sql, *args):
            assert "progress->'billing'" in sql and "kind='closure'" in sql
            return json.dumps({"desired": "stop_renewal", "state": "pending", "detail": "failed:TimeoutError",
                               "checked_at": "2026-11-21T00:00:00+00:00"})

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    monkeypatch.setattr(pl, "tenant_tx", tx)
    out = asyncio.run(pl.tenant_lifecycle(OWNER))
    assert out["billing_renewal"] == {"desired": "stop_renewal", "state": "pending",
                                      "checked_at": "2026-11-21T00:00:00+00:00"}   # no provider detail


def test_the_erasure_reports_a_confirmed_stop_as_renewal_cancelled(monkeypatch):
    class Conn:
        async def fetch(self, *_a):
            return []

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def nothing(*_a, **_k):
        return []

    async def confirmed(_tenant, cancel, **_kw):
        return "confirmed"

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "revoke_personal_credentials", nothing)
    monkeypatch.setattr(pl, "_converge_subscription", confirmed)
    out = asyncio.run(pl._phase_providers(pl._platform_ctx(), "op", T, {}))
    assert out == {"steps": [{"provider": "stripe", "action": "stop_renewal", "status": "renewal_cancelled",
                              "note": "billing records are retained by Neoh as controller (7 years)"}],
                   "failed": 0}
