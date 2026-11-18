"""A slow Stripe call does not stall the event loop (external audit 24.7).

The usage drain runs as a coroutine in the durable job worker, next to job
heartbeats and lease renewals (and, all-in-one, live calls). It called the
Stripe SDK's synchronous client directly, so every slow meter-event request
froze that loop. These tests run a heartbeat coroutine beside a deliberately
slow (blocking) Stripe fake and count its ticks.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
import stripe

import billing_usage
from tenancy import Role, TenantContext

T = "11111111-1111-4111-8111-111111111111"
CTX = TenantContext(agent_id="usage-drain", tenant_id=T, role=Role.PLATFORM_ADMIN)


class _Conn:
    """Scripted drain conversation: one claimable row, then none."""

    def __init__(self, log, rows=1):
        self.log = log
        self.rows = rows

    async def fetch(self, sql, *args):
        return []                                    # nothing to quarantine

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE SKIP LOCKED" in sql and self.rows:
            self.rows -= 1
            return {"id": str(uuid.uuid4()), "metric": "lead_engaged", "quantity": 1,
                    "occurred_at": datetime.now(timezone.utc), "idempotency_key": "lead-engaged:k1",
                    "attempt": 1, "prior_uncertain_since": None}
        return None

    async def fetchval(self, sql, *args):
        if "SET reported_at=now()" in sql:
            self.log.append("acked")
            return args[0]
        return 0                                     # aging count

    async def execute(self, sql, *args):
        self.log.append(sql)
        self.log.args[sql] = args
        return "UPDATE 1"


@pytest.fixture
def drain_env(monkeypatch):
    log = _Log()
    state = {"conn": _Conn(log)}

    @asynccontextmanager
    async def tx(_ctx):
        yield state["conn"]

    async def customer(_ctx):
        return "cus_1"

    monkeypatch.setattr(billing_usage, "tenant_tx", tx)
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_x")
    monkeypatch.setattr(billing_usage, "_stripe_customer_for", customer)
    log.state = state
    return log


class _Log(list):
    def __init__(self):
        super().__init__()
        self.args: dict = {}


async def _with_heartbeat(coro, period=0.02):
    ticks = 0
    done = asyncio.Event()

    async def beat():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(period)
            ticks += 1

    task = asyncio.create_task(beat())
    try:
        result = await coro
    finally:
        done.set()
        await task
    return result, ticks


def test_a_slow_meter_event_does_not_freeze_the_heartbeat(drain_env, monkeypatch):
    def slow_create(**_kw):
        time.sleep(0.5)                              # a blocking HTTP call, as the SDK makes
        return type("Event", (), {"identifier": "lead-engaged:k1"})()

    monkeypatch.setattr(stripe.billing.MeterEvent, "create", slow_create)
    result, ticks = asyncio.run(_with_heartbeat(billing_usage.drain_usage_to_stripe(CTX)))
    assert result["reported"] == 1 and "acked" in drain_env
    assert ticks >= 10, f"the loop froze for the Stripe call ({ticks} heartbeats in 0.5 s)"


def test_a_stripe_call_past_the_timeout_is_an_unknown_outcome(drain_env, monkeypatch):
    def hung_create(**_kw):
        time.sleep(0.4)
        return type("Event", (), {"identifier": "lead-engaged:k1"})()

    monkeypatch.setattr(stripe.billing.MeterEvent, "create", hung_create)
    monkeypatch.setattr(billing_usage, "REPORT_SEND_TIMEOUT_SECONDS", 0.05)
    result, ticks = asyncio.run(_with_heartbeat(billing_usage.drain_usage_to_stripe(CTX)))
    assert result["reported"] == 0 and "acked" not in drain_env
    # released keeping its uncertainty (it may still land) AND its claim: the
    # thread may still be sending, so no other drain takes the row until the
    # lease lapses; then it is resent under the same identifier and key
    release = [sql for sql in drain_env if isinstance(sql, str) and "report_error=$2" in sql]
    assert release and "COALESCE(report_uncertain_since, now())" in release[0]
    assert "CASE WHEN $5::boolean THEN report_claimed_until END" in release[0]
    assert drain_env.args[release[0]][4] is True                # keep_claim
    assert ticks >= 2


def test_a_drain_stops_when_stripe_stops_answering(drain_env, monkeypatch):
    """24.7 review: with heartbeats alive a hung Stripe no longer kills the job,
    so the drain itself stops after a few unknown outcomes in a row instead of
    spending the timeout on every pending row."""
    drain_env.state["conn"] = _Conn(drain_env, rows=10)
    sent = []

    def down(**kw):
        sent.append(kw["identifier"])
        raise stripe.APIConnectionError("connection reset")

    monkeypatch.setattr(stripe.billing.MeterEvent, "create", down)
    result = asyncio.run(billing_usage.drain_usage_to_stripe(CTX))
    assert result["state"] == "stripe_unresponsive" and result["reported"] == 0
    assert len(sent) == billing_usage.REPORT_MAX_CONSECUTIVE_UNKNOWN


def test_idempotency_conflicts_do_not_stop_the_drain(drain_env, monkeypatch):
    """24.7 re-review: a brokerage whose Stripe customer changed answers every
    resend with a fast IdempotencyError. That is about those rows, not about
    Stripe: the drain carries on (and so does the sweep)."""
    drain_env.state["conn"] = _Conn(drain_env, rows=5)
    sent = []

    def conflict(**kw):
        sent.append(kw["identifier"])
        raise stripe.IdempotencyError("Keys for idempotent requests can only be used with the same parameters")

    monkeypatch.setattr(stripe.billing.MeterEvent, "create", conflict)
    result = asyncio.run(billing_usage.drain_usage_to_stripe(CTX))
    assert result["state"] == "ok" and len(sent) == 5


def test_a_stripe_5xx_counts_but_a_4xx_api_error_does_not():
    assert billing_usage._transport_failure(stripe.APIError("boom", http_status=503))
    assert billing_usage._transport_failure(stripe.APIConnectionError("reset"))
    assert billing_usage._transport_failure(TimeoutError())
    assert not billing_usage._transport_failure(stripe.APIError("odd", http_status=409))
    assert not billing_usage._transport_failure(stripe.IdempotencyError("conflict"))


def test_the_sweep_stops_when_stripe_stops_answering(monkeypatch):
    @asynccontextmanager
    async def tx(_ctx):
        class _C:
            async def fetch(self, *_a):
                return [{"tenant_id": T, "pending": 5}, {"tenant_id": str(uuid.uuid4()), "pending": 5}]
        yield _C()

    drained = []

    async def drain(ctx, **_k):
        drained.append(ctx.tenant_id)
        return {"state": "stripe_unresponsive", "reported": 0,
                "quarantined": {"expired": 2, "uncertain": 1}, "aging": 4}

    monkeypatch.setattr(billing_usage, "tenant_tx", tx)
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_x")
    monkeypatch.setattr(billing_usage, "drain_usage_to_stripe", drain)
    result = asyncio.run(billing_usage.drain_all_tenants())
    assert drained == [T] and result["by_state"] == {"stripe_unresponsive": 1}
    assert result["quarantined"] == {"expired": 2, "uncertain": 1} and result["aging"] == 4


def test_closing_a_brokerage_changes_its_subscription_off_the_event_loop(monkeypatch):
    import privacy_lifecycle as pl
    import recovery_mode

    class _SubConn:
        async def fetchval(self, *_a):
            return "sub_123"

    @asynccontextmanager
    async def tx(_ctx):
        yield _SubConn()

    calls = []

    def slow_modify(sub, **kw):
        time.sleep(0.4)
        calls.append((sub, kw))

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    monkeypatch.setattr(stripe, "api_key", "sk_test_x")
    monkeypatch.setattr(stripe.Subscription, "modify", slow_modify)
    result, ticks = asyncio.run(_with_heartbeat(pl._set_cancel_at_period_end(T, True)))
    assert result == "renewal_cancelled"
    assert calls == [("sub_123", {"cancel_at_period_end": True, "max_network_retries": 0})]
    assert ticks >= 8, f"the loop froze for the Stripe call ({ticks} heartbeats in 0.4 s)"


def test_a_timed_out_close_cannot_overtake_a_later_withdrawal(monkeypatch):
    """24.7 review: a cancel-at-period-end that timed out is still running in
    its thread; the owner's withdrawal right after must reach Stripe AFTER it,
    or the late cancel silently stops an active brokerage's renewal."""
    import privacy_lifecycle as pl
    import recovery_mode

    class _SubConn:
        async def fetchval(self, *_a):
            return "sub_order"

    @asynccontextmanager
    async def tx(_ctx):
        yield _SubConn()

    landed = []

    def modify(sub, **kw):
        if kw["cancel_at_period_end"]:
            time.sleep(0.3)                          # the close is slow
        landed.append(kw["cancel_at_period_end"])

    monkeypatch.setattr(pl, "tenant_tx", tx)
    monkeypatch.setattr(pl, "STRIPE_SUBSCRIPTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    monkeypatch.setattr(stripe, "api_key", "sk_test_x")
    monkeypatch.setattr(stripe.Subscription, "modify", modify)

    async def close_then_withdraw():
        closed = await pl._set_cancel_at_period_end(T, True)
        monkeypatch.setattr(pl, "STRIPE_SUBSCRIPTION_TIMEOUT_SECONDS", 5)
        withdrawn = await pl._set_cancel_at_period_end(T, False)
        return closed, withdrawn

    closed, withdrawn = asyncio.run(close_then_withdraw())
    assert closed == "failed:TimeoutError"          # reported to the owner as a failure
    assert withdrawn == "renewal_resumed"
    assert landed == [True, False]                  # Stripe ends on the owner's last word
