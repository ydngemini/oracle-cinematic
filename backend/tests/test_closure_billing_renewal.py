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
    def __init__(self, lifecycle, running="op-1", latest="op-1"):
        self.lifecycle, self.executed = lifecycle, []
        self.running, self.latest = running, latest

    async def fetchrow(self, sql, *args):
        if "FROM tenants" in sql:
            return {"name": "Brokerage B", "lifecycle_state": self.lifecycle}
        if "INSERT INTO privacy_operations" in sql:
            return {"id": "op-1"}
        if "FROM privacy_operations" in sql:      # withdraw: the running closure
            assert "state='running'" in sql
            return {"id": self.running, "progress": json.dumps({"freeze": {}})} if self.running else None
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "FROM privacy_operations" in sql:      # withdraw: the latest closure, any state
            assert "state" not in sql.split("WHERE", 1)[1]
            return self.latest
        return self.lifecycle

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 0"


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
    conn = _ClosureConn("closing", running=None, latest="op-blocked")
    out, conn, queued, _ = _act(monkeypatch, "withdraw", lifecycle="closing", conn=conn,
                                billing={"state": "confirmed"})
    assert out["billing"] == "confirmed"
    [job] = queued
    assert job["idempotency_key"] == "privacy:billing:op-blocked:withdraw"
    assert job["payload"] == {"operation_id": "op-blocked"}
    marked = next(a for sql, a in conn.executed if "jsonb_set(progress, '{billing}'" in sql)
    assert marked[0] == "op-blocked"


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
