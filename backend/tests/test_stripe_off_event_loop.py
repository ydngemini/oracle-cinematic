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

    def __init__(self, log):
        self.log = log
        self.claimed = False

    async def fetch(self, sql, *args):
        return []                                    # nothing to quarantine

    async def fetchrow(self, sql, *args):
        if "FOR UPDATE SKIP LOCKED" in sql and not self.claimed:
            self.claimed = True
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
        return "UPDATE 1"


@pytest.fixture
def drain_env(monkeypatch):
    log: list = []
    state = {"conn": _Conn(log)}

    @asynccontextmanager
    async def tx(_ctx):
        yield state["conn"]

    async def customer(_ctx):
        return "cus_1"

    monkeypatch.setattr(billing_usage, "tenant_tx", tx)
    monkeypatch.setattr(billing_usage, "STRIPE_METERED_PRICE_ID", "price_x")
    monkeypatch.setattr(billing_usage, "_stripe_customer_for", customer)
    return log


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
    # released keeping its uncertainty (it may still land): resent only inside
    # Stripe's window, under the same identifier and idempotency key
    release = [sql for sql in drain_env if isinstance(sql, str) and "report_claimed_until=NULL" in sql]
    assert release and "COALESCE(report_uncertain_since, now())" in release[0]
    assert ticks >= 1


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
    assert result == "renewal_cancelled" and calls == [("sub_123", {"cancel_at_period_end": True})]
    assert ticks >= 8, f"the loop froze for the Stripe call ({ticks} heartbeats in 0.4 s)"
