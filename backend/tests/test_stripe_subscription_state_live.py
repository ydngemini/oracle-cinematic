"""A cancelled plan stays cancelled, no Stripe event is lost to ordering, and
one brokerage gets one checkout — against real PostgreSQL (T18.05, AC18.03,
AC18.07; skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Events are signed exactly as Stripe signs them and posted through the ASGI app,
so signature verification and the event ledger are the real code path. Stripe
itself is never called: Checkout Session creation is replaced by an offline
stand-in that, like Stripe, answers a repeated idempotency key with the session
it already made. Nothing here charges, cancels or refunds anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
SECRET = "whsec_" + "x" * 32


def _signed(event: dict) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


class _OfflineCheckout:
    """stripe.checkout.Session.create without the network: one session per
    idempotency key, replayed for a repeated key the way Stripe replays it."""

    def __init__(self, answer_after_s: float = 0.0):
        self.calls: list[tuple[str, dict]] = []
        self.by_key: dict[str, SimpleNamespace] = {}
        self.lose_next_response = False
        # Stripe takes a moment to answer; runs in the worker thread, as the SDK does.
        self.answer_after_s = answer_after_s

    def create(self, **kwargs):
        key = kwargs.pop("idempotency_key")
        self.calls.append((key, kwargs))
        if self.answer_after_s:
            time.sleep(self.answer_after_s)
        if key not in self.by_key:
            sid = f"cs_test_{uuid.uuid4().hex[:20]}"
            self.by_key[key] = SimpleNamespace(
                id=sid, url=f"https://checkout.stripe.com/c/pay/{sid}",
                expires_at=int(time.time()) + 24 * 3600)
        if self.lose_next_response:
            # Stripe made the session; the answer never reached us.
            self.lose_next_response = False
            import stripe
            raise stripe.error.APIConnectionError("connection reset by peer")
        return self.by_key[key]


class _World:
    def __init__(self, post, admin):
        self.post, self.admin = post, admin
        self.event_ids: list[str] = []
        self.tenants: list[str] = []

    def event(self, kind: str, created: int, obj: dict) -> dict:
        eid = f"evt_State{uuid.uuid4().hex[:16]}"
        self.event_ids.append(eid)
        return {"id": eid, "object": "event", "type": kind, "created": created, "data": {"object": obj}}

    def checkout_event(self, created: int, tenant: str, sub: str, cus: str, *, paid: bool = True,
                       session_id: str | None = None) -> dict:
        return self.event("checkout.session.completed", created, {
            "id": session_id or f"cs_test_{uuid.uuid4().hex[:20]}", "object": "checkout.session",
            "metadata": {"tenant_id": tenant}, "subscription": sub, "customer": cus,
            "payment_status": "paid" if paid else "unpaid"})

    async def tenant(self) -> str:
        t = str(uuid.uuid4())
        await self.admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Stripe state drill')",
                                 t, f"stripe-state-{t[:8]}")
        self.tenants.append(t)
        return t

    async def status(self, sub: str) -> str | None:
        return await self.admin.fetchval(
            "SELECT status FROM subscriptions WHERE stripe_subscription_id=$1", sub)


def _ids() -> tuple[str, str]:
    return f"sub_state{uuid.uuid4().hex[:12]}", f"cus_state{uuid.uuid4().hex[:12]}"


def _owner(tenant: str):
    from tenancy import Role, TenantContext

    return TenantContext(agent_id=f"owner-{tenant[:8]}@drill.test", tenant_id=tenant, role=Role.BROKER_OWNER)


def _run(monkeypatch, scenario) -> None:
    asyncio.run(_harness(monkeypatch, scenario))


async def _harness(monkeypatch, scenario) -> None:
    import asyncpg
    import httpx
    from fastapi import FastAPI

    import billing
    from db import connection

    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(billing, "STRIPE_PRICE_ID", "price_test_statedrill")
    monkeypatch.setattr(billing, "STRIPE_METERED_PRICE_ID", "")
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)
    app = FastAPI()
    app.include_router(billing.router)
    path = next(r.path for r in billing.router.routes if "webhook" in r.path)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=6)
    world = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            async def post(ev):
                body, headers = _signed(ev)
                return await client.post(path, content=body, headers=headers)

            world = _World(post, admin)
            await scenario(world)
    finally:
        if world is not None:
            await admin.execute("DELETE FROM stripe_webhook_events WHERE event_id = ANY($1::text[])",
                                world.event_ids)
            for t in world.tenants:
                await admin.execute("DELETE FROM subscriptions WHERE tenant_id=$1", t)
                await admin.execute("DELETE FROM tenants WHERE id=$1", t)
        await admin.close()
        await connection.close_pool()


# ── a cancelled subscription is terminal ─────────────────────────────────────

def test_deleted_before_checkout_is_redelivered_not_lost(monkeypatch):
    """The deletion of a subscription we have not stored yet used to commit its
    event id and answer 200, so Stripe never sent it again; the late checkout
    then created the row 'active' for good."""
    async def scenario(w):
        T = await w.tenant()
        sub, cus = _ids()
        deleted = w.event("customer.subscription.deleted", 200, {"id": sub, "status": "canceled"})
        first = await w.post(deleted)
        assert first.status_code == 503, first.text
        assert first.headers.get("retry-after")
        assert await w.admin.fetchval(
            "SELECT count(*) FROM stripe_webhook_events WHERE event_id=$1", deleted["id"]) == 0
        assert (await w.post(w.checkout_event(100, T, sub, cus))).status_code == 200
        again = await w.post(deleted)  # Stripe's redelivery
        assert again.status_code == 200 and not again.json().get("duplicate")
        assert await w.status(sub) == "canceled"
        assert await w.admin.fetchval(
            "SELECT last_event_created FROM subscriptions WHERE stripe_subscription_id=$1", sub) == 200
    _run(monkeypatch, scenario)


def test_same_second_invoice_paid_cannot_undo_a_deletion(monkeypatch):
    """Cancel with invoice_now: the deletion and the final invoice.paid share a second."""
    async def scenario(w):
        T = await w.tenant()
        sub, cus = _ids()
        assert (await w.post(w.checkout_event(100, T, sub, cus))).status_code == 200
        assert (await w.post(w.event("customer.subscription.deleted", 200,
                                     {"id": sub, "status": "canceled"}))).status_code == 200
        r = await w.post(w.event("invoice.paid", 200, {"subscription": sub, "period_end": 200}))
        assert r.status_code == 200, r.text
        assert await w.status(sub) == "canceled"
    _run(monkeypatch, scenario)


def test_later_invoice_paid_cannot_resurrect_a_canceled_subscription(monkeypatch):
    """Dunning cancelled the plan and left the invoice open; the customer pays it
    weeks later. The payment settles a debt — it does not reopen the plan, and
    Stripe will never bill this subscription again."""
    async def scenario(w):
        import billing
        from fastapi import HTTPException

        T = await w.tenant()
        sub, cus = _ids()
        assert (await w.post(w.checkout_event(100, T, sub, cus))).status_code == 200
        assert (await w.post(w.event("customer.subscription.deleted", 200,
                                     {"id": sub, "status": "canceled"}))).status_code == 200
        late = w.event("invoice.paid", 260, {"subscription": sub, "period_end": int(time.time()) + 86400})
        r = await w.post(late)
        # Processed (not retried for three days), and recorded in the ledger.
        assert r.status_code == 200, r.text
        assert await w.admin.fetchval(
            "SELECT count(*) FROM stripe_webhook_events WHERE event_id=$1", late["id"]) == 1
        assert await w.status(sub) == "canceled"
        # AC18.07: a cancelled brokerage cannot start new billable actions.
        monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
        with pytest.raises(HTTPException) as refused:
            await billing.require_active_subscription(_owner(T))
        assert refused.value.status_code == 402
    _run(monkeypatch, scenario)


def test_same_second_update_cannot_undo_a_deletion(monkeypatch):
    async def scenario(w):
        T = await w.tenant()
        sub, cus = _ids()
        assert (await w.post(w.checkout_event(100, T, sub, cus))).status_code == 200
        assert (await w.post(w.event("customer.subscription.deleted", 200,
                                     {"id": sub, "status": "canceled"}))).status_code == 200
        assert (await w.post(w.event("customer.subscription.updated", 200,
                                     {"id": sub, "status": "past_due"}))).status_code == 200
        assert (await w.post(w.event("customer.subscription.updated", 300,
                                     {"id": sub, "status": "active"}))).status_code == 200
        assert await w.status(sub) == "canceled"
    _run(monkeypatch, scenario)


def test_a_late_checkout_cannot_reopen_a_canceled_subscription(monkeypatch):
    """Same second or later, a checkout.session.completed naming a cancelled
    subscription leaves it cancelled."""
    async def scenario(w):
        T = await w.tenant()
        sub, cus = _ids()
        assert (await w.post(w.checkout_event(100, T, sub, cus))).status_code == 200
        assert (await w.post(w.event("customer.subscription.deleted", 200,
                                     {"id": sub, "status": "canceled"}))).status_code == 200
        for created in (200, 260):
            assert (await w.post(w.checkout_event(created, T, sub, cus))).status_code == 200
            assert await w.status(sub) == "canceled", created
    _run(monkeypatch, scenario)


def test_incomplete_expired_is_terminal_too(monkeypatch):
    """Stripe never revives an incomplete_expired subscription either."""
    async def scenario(w):
        T = await w.tenant()
        sub, cus = _ids()
        assert (await w.post(w.checkout_event(100, T, sub, cus, paid=False))).status_code == 200
        assert await w.status(sub) == "incomplete"
        assert (await w.post(w.event("customer.subscription.updated", 150,
                                     {"id": sub, "status": "incomplete_expired"}))).status_code == 200
        assert (await w.post(w.event("invoice.paid", 300, {"subscription": sub, "period_end": 999}))).status_code == 200
        assert (await w.post(w.event("customer.subscription.updated", 300,
                                     {"id": sub, "status": "active"}))).status_code == 200
        assert await w.status(sub) == "incomplete_expired"
    _run(monkeypatch, scenario)


# ── one brokerage, one plan ──────────────────────────────────────────────────

def test_a_second_live_subscription_is_recorded_and_flagged_for_a_human(monkeypatch):
    """The first checkout's webhook was late and the owner bought again. Both
    are real Stripe subscriptions, so both are recorded as Stripe reports them;
    the second is flagged for a person to cancel and refund. Nothing is
    cancelled or refunded automatically."""
    import stripe

    def _never(*_a, **_k):
        raise AssertionError("the webhook must never cancel, modify or refund on its own")

    for target, name in ((stripe.Subscription, "cancel"), (stripe.Subscription, "modify"),
                         (stripe.Subscription, "delete"), (stripe.Refund, "create")):
        monkeypatch.setattr(target, name, staticmethod(_never), raising=False)

    async def scenario(w):
        T = await w.tenant()
        sub1, cus = _ids()
        sub2, _ = _ids()
        assert (await w.post(w.checkout_event(100, T, sub1, cus))).status_code == 200
        assert await w.admin.fetchval(
            "SELECT count(*) FROM audit_anomaly_alerts WHERE tenant_id=$1", T) == 0
        second = w.checkout_event(900, T, sub2, cus)
        assert (await w.post(second)).status_code == 200
        rows = await w.admin.fetch(
            "SELECT stripe_subscription_id, status FROM subscriptions WHERE tenant_id=$1", T)
        assert {r["stripe_subscription_id"]: r["status"] for r in rows} == {sub1: "active", sub2: "active"}
        alerts = await w.admin.fetch(
            "SELECT anomaly_type, severity, route, evidence FROM audit_anomaly_alerts WHERE tenant_id=$1", T)
        assert len(alerts) == 1, alerts
        alert = alerts[0]
        assert alert["anomaly_type"] == "second_live_subscription"
        assert alert["severity"] == "high"
        evidence = json.loads(alert["evidence"])
        assert evidence["subscription"] == sub2
        assert evidence["other_live_subscriptions"] == [sub1]
        assert "nothing was cancelled or refunded" in evidence["action"]
        # A redelivery of the same event, and another event for the same
        # subscription, do not raise the flag again.
        assert (await w.post(second)).json().get("duplicate") is True
        assert (await w.post(w.checkout_event(950, T, sub2, cus))).status_code == 200
        assert await w.admin.fetchval(
            "SELECT count(*) FROM audit_anomaly_alerts WHERE tenant_id=$1", T) == 1
    _run(monkeypatch, scenario)


def test_a_second_live_subscription_stays_on_billing_exceptions_until_a_person_resolves_it(monkeypatch):
    """The flag went only to the anomaly list, newest first and shared with
    routine access-denial alerts of the same severity: a burst of those pushed
    it out of the console while both plans billed. Billing exceptions list the
    duplicate from the subscriptions themselves until one is cancelled."""
    async def scenario(w):
        import admin_ops
        import operator_api
        from tenancy import Role, TenantContext

        ops = TenantContext(agent_id="ops@drill.test", tenant_id="00000000-0000-0000-0000-000000000000",
                            role=Role.PLATFORM_ADMIN)
        T = await w.tenant()
        sub1, cus = _ids()
        sub2, _ = _ids()
        assert (await w.post(w.checkout_event(100, T, sub1, cus))).status_code == 200
        assert (await w.post(w.checkout_event(900, T, sub2, cus))).status_code == 200
        for n in range(60):
            await w.admin.execute(
                "INSERT INTO audit_anomaly_alerts (tenant_id, anomaly_type, severity, fingerprint, route, evidence) "
                "VALUES ($1, 'repeated_access_denial', 'high', $2, '/api/x', '{}'::jsonb)",
                T, hashlib.sha256(f"denial-{T}-{n}".encode()).hexdigest())
        newest = await admin_ops.anomaly_alerts(severity=None, limit=50, ctx=ops)
        assert "second_live_subscription" not in {a["anomaly_type"] for a in newest["alerts"]}

        out = await operator_api.billing_exceptions(ctx=ops)
        mine = [d for d in out["duplicate_subscriptions"] if d["tenant_id"] == T]
        assert len(mine) == 1, out["duplicate_subscriptions"]
        assert {s["stripe_subscription_ref"] for s in mine[0]["subscriptions"]} == {sub1, sub2}
        assert mine[0]["flagged_at"] is not None
        assert "duplicate_subscriptions" not in out["unavailable"]

        # A person cancels one in Stripe; its deletion takes the brokerage off the list.
        assert (await w.post(w.event("customer.subscription.deleted", 950, {"id": sub2}))).status_code == 200
        out = await operator_api.billing_exceptions(ctx=ops)
        assert not [d for d in out["duplicate_subscriptions"] if d["tenant_id"] == T]
    _run(monkeypatch, scenario)


class _ParkAfterLiveRead:
    """Holds the first checkout event's transaction just after it reads the
    brokerage's live subscriptions, until a second event has read them too (or
    `wait_s` passes).

    asyncio.gather over ASGITransport alone does not interleave the two
    transactions, so a test without this passed with the brokerage's lock
    removed. Here, without the lock, both reads run before either insert
    commits and neither sees the other; with it, the second waits at the lock
    and never reads while the first is parked."""

    NEEDLE = "SELECT stripe_subscription_id FROM subscriptions WHERE tenant_id"

    def __init__(self, wait_s: float = 1.0):
        self.wait_s = wait_s
        self.reads = 0
        self.overlapped = False
        self._second_read = asyncio.Event()

    def install(self, monkeypatch) -> None:
        import billing

        real_tx, park = billing.tenant_tx, self

        class _Conn:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            async def fetch(self, query, *args):
                rows = await self._conn.fetch(query, *args)
                if park.NEEDLE in query:
                    park.reads += 1
                    if park.reads == 1:
                        try:
                            await asyncio.wait_for(park._second_read.wait(), park.wait_s)
                            park.overlapped = True
                        except asyncio.TimeoutError:
                            pass
                    else:
                        park._second_read.set()
                return rows

        @asynccontextmanager
        async def parked_tx(ctx):
            async with real_tx(ctx) as conn:
                yield _Conn(conn)

        monkeypatch.setattr(billing, "tenant_tx", parked_tx)


def test_two_checkouts_landing_together_still_raise_the_flag(monkeypatch):
    """Delivered concurrently, each checkout event could miss the other's
    uncommitted row; the brokerage's checkout events are serialised. The first
    is parked right after its read, so this fails if that lock is removed."""
    park = _ParkAfterLiveRead()
    park.install(monkeypatch)

    async def scenario(w):
        T = await w.tenant()
        (sub1, cus), (sub2, _) = _ids(), _ids()
        results = await asyncio.gather(w.post(w.checkout_event(100, T, sub1, cus)),
                                       w.post(w.checkout_event(100, T, sub2, cus)))
        assert [r.status_code for r in results] == [200, 200]
        assert await w.admin.fetchval(
            "SELECT count(*) FROM subscriptions WHERE tenant_id=$1 AND status='active'", T) == 2
        flagged = await w.admin.fetch(
            "SELECT evidence FROM audit_anomaly_alerts WHERE tenant_id=$1 "
            "AND anomaly_type='second_live_subscription'", T)
        assert len(flagged) == 1, flagged
        evidence = json.loads(flagged[0]["evidence"])
        assert {evidence["subscription"], *evidence["other_live_subscriptions"]} == {sub1, sub2}
        # The interleaving really was attempted, and the lock held the second back.
        assert park.reads == 2
        assert park.overlapped is False, "the second event read while the first was still open"
    _run(monkeypatch, scenario)


def test_entitlement_reads_any_live_subscription_not_just_the_newest_row(monkeypatch):
    """Cancelling the newer of two live subscriptions locked the brokerage out
    (402) while the older one kept billing, and status and the portal read the
    cancelled one."""
    import stripe

    portal_customers: list[str] = []

    def _portal(**kwargs):
        portal_customers.append(kwargs["customer"])
        return SimpleNamespace(url="https://billing.stripe.com/p/session/test_drill")

    monkeypatch.setattr(stripe.billing_portal.Session, "create", staticmethod(_portal))

    async def scenario(w):
        import billing

        T = await w.tenant()
        sub1, cus1 = _ids()
        sub2, cus2 = _ids()
        assert (await w.post(w.checkout_event(100, T, sub1, cus1))).status_code == 200
        await asyncio.sleep(0.01)  # distinct created_at: sub2 is the newer row
        assert (await w.post(w.checkout_event(900, T, sub2, cus2))).status_code == 200
        assert (await w.post(w.event("customer.subscription.deleted", 950, {"id": sub2}))).status_code == 200
        monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
        ctx = _owner(T)
        assert await billing.require_active_subscription(ctx) is ctx
        status = await billing.subscription_status(T, ctx=ctx)
        assert status.active is True and status.status == "active"
        await billing.create_portal_session(billing.PortalRequest(tenant_id=T), ctx=ctx)
        assert portal_customers == [cus1]
    _run(monkeypatch, scenario)


def test_checkout_reuses_the_brokerages_open_session_across_clicks_tabs_and_time(monkeypatch):
    """The key was a ten-minute wall-clock bucket: a double click across a
    boundary, or a second tab later, opened a second payable session."""
    import stripe

    offline = _OfflineCheckout()
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(offline.create))

    async def scenario(w):
        import billing

        T = await w.tenant()
        ctx = _owner(T)
        request = billing.CheckoutRequest(tenant_id=T)
        with monkeypatch.context() as m:
            m.setattr(time, "time", lambda: 1_800_000_599.6)
            first = await billing.create_checkout_session(request, ctx=ctx)
        with monkeypatch.context() as m:
            m.setattr(time, "time", lambda: 1_800_000_600.2)
            second = await billing.create_checkout_session(request, ctx=ctx)
        # Two tabs at once.
        more = await asyncio.gather(*(billing.create_checkout_session(request, ctx=ctx) for _ in range(3)))
        assert {r.session_id for r in [first, second, *more]} == {first.session_id}
        assert {r.url for r in [first, second, *more]} == {first.url}
        assert len(offline.calls) == 1, [k for k, _ in offline.calls]
        key = offline.calls[0][0]
        assert T in key and "1800000" not in key and "3000000" not in key
        assert await w.admin.fetchval(
            "SELECT stripe_session_id FROM billing_checkout_sessions WHERE tenant_id=$1", T) == first.session_id
    _run(monkeypatch, scenario)


def test_first_clicks_together_for_a_new_brokerage_reach_stripe_once(monkeypatch):
    """A brand-new brokerage, three clicks at once, and a Stripe that takes a
    moment to answer. Each click used to find no stored session and call
    Stripe itself (Stripe refuses a key already in flight, so the other tabs
    got an error); behind the brokerage's checkout lock the later clicks wait,
    then hand back the session the first one stored."""
    import stripe

    offline = _OfflineCheckout(answer_after_s=0.3)
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(offline.create))

    async def scenario(w):
        import billing

        T = await w.tenant()
        ctx = _owner(T)
        request = billing.CheckoutRequest(tenant_id=T)
        clicks = await asyncio.gather(*(billing.create_checkout_session(request, ctx=ctx) for _ in range(3)))
        assert len(offline.calls) == 1, [k for k, _ in offline.calls]
        assert {c.session_id for c in clicks} == {next(iter(offline.by_key.values())).id}
        assert await w.admin.fetchval(
            "SELECT count(*) FROM billing_checkout_sessions WHERE tenant_id=$1", T) == 1
    _run(monkeypatch, scenario)


def test_a_lost_stripe_answer_is_replayed_not_duplicated(monkeypatch):
    """Stripe created the session but the answer was lost: the retry carries the
    same key, so Stripe hands back that session instead of a second one."""
    import stripe
    from fastapi import HTTPException

    offline = _OfflineCheckout()
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(offline.create))

    async def scenario(w):
        import billing

        T = await w.tenant()
        ctx = _owner(T)
        offline.lose_next_response = True
        with pytest.raises(HTTPException) as lost:
            await billing.create_checkout_session(billing.CheckoutRequest(tenant_id=T), ctx=ctx)
        assert lost.value.status_code == 502
        retry = await billing.create_checkout_session(billing.CheckoutRequest(tenant_id=T), ctx=ctx)
        assert len(offline.by_key) == 1
        assert retry.session_id == next(iter(offline.by_key.values())).id
        assert offline.calls[0][0] == offline.calls[1][0]
    _run(monkeypatch, scenario)


def test_after_a_completed_and_cancelled_plan_checkout_opens_a_new_session_on_the_same_customer(monkeypatch):
    import stripe
    from fastapi import HTTPException

    offline = _OfflineCheckout()
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(offline.create))

    async def scenario(w):
        import billing

        T = await w.tenant()
        ctx = _owner(T)
        sub, cus = _ids()
        request = billing.CheckoutRequest(tenant_id=T)
        first = await billing.create_checkout_session(request, ctx=ctx)
        assert "customer" not in offline.calls[0][1]  # a first purchase has no customer yet
        assert (await w.post(w.checkout_event(100, T, sub, cus, session_id=first.session_id))).status_code == 200
        assert await w.admin.fetchval(
            "SELECT completed_at IS NOT NULL FROM billing_checkout_sessions WHERE tenant_id=$1", T) is True
        with pytest.raises(HTTPException) as live:
            await billing.create_checkout_session(request, ctx=ctx)
        assert live.value.status_code == 409
        assert (await w.post(w.event("customer.subscription.deleted", 500, {"id": sub}))).status_code == 200
        again = await billing.create_checkout_session(request, ctx=ctx)
        assert again.session_id != first.session_id
        assert len(offline.calls) == 2
        key, kwargs = offline.calls[1]
        assert key != offline.calls[0][0]
        # The brokerage's existing Stripe customer, so both plans and every
        # invoice sit on one customer and one portal.
        assert kwargs["customer"] == cus
        assert kwargs["customer_update"] == {"address": "auto", "name": "auto"}
    _run(monkeypatch, scenario)


def test_checkout_is_refused_while_a_first_payment_is_still_pending(monkeypatch):
    """A delayed-payment checkout leaves the plan 'incomplete'; a second purchase
    then would be a second subscription."""
    import stripe
    from fastapi import HTTPException

    offline = _OfflineCheckout()
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(offline.create))

    async def scenario(w):
        import billing

        T = await w.tenant()
        sub, cus = _ids()
        assert (await w.post(w.checkout_event(100, T, sub, cus, paid=False))).status_code == 200
        with pytest.raises(HTTPException) as pending:
            await billing.create_checkout_session(billing.CheckoutRequest(tenant_id=T), ctx=_owner(T))
        assert pending.value.status_code == 409
        assert offline.calls == []
    _run(monkeypatch, scenario)


def test_an_unpaid_plan_is_refused_checkout_but_its_owner_can_reach_the_portal(monkeypatch):
    """Dunning ends with the plan 'unpaid' (the account's failed-payment
    setting). Access is locked and a second checkout is refused, so the
    refusal must name the overlay's Manage billing action, and that action
    must open the portal on the unpaid plan's customer."""
    import stripe
    from fastapi import HTTPException

    offline = _OfflineCheckout()
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(offline.create))
    portal_customers: list[str] = []

    def _portal(**kwargs):
        portal_customers.append(kwargs["customer"])
        return SimpleNamespace(url="https://billing.stripe.com/p/session/test_unpaid")

    monkeypatch.setattr(stripe.billing_portal.Session, "create", staticmethod(_portal))

    async def scenario(w):
        import billing

        T = await w.tenant()
        sub, cus = _ids()
        ctx = _owner(T)
        assert (await w.post(w.checkout_event(100, T, sub, cus))).status_code == 200
        assert (await w.post(w.event("customer.subscription.updated", 400,
                                     {"id": sub, "status": "unpaid"}))).status_code == 200
        monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
        with pytest.raises(HTTPException) as locked:
            await billing.require_active_subscription(ctx)
        assert locked.value.status_code == 402
        status = await billing.subscription_status(T, ctx=ctx)
        assert (status.active, status.status) == (False, "unpaid")
        with pytest.raises(HTTPException) as refused:
            await billing.create_checkout_session(billing.CheckoutRequest(tenant_id=T), ctx=ctx)
        assert refused.value.status_code == 409
        assert "Manage billing" in refused.value.detail
        assert offline.calls == []
        opened = await billing.create_portal_session(billing.PortalRequest(tenant_id=T), ctx=ctx)
        assert json.loads(opened.body)["url"].startswith("https://billing.stripe.com/")
        assert portal_customers == [cus]
    _run(monkeypatch, scenario)
