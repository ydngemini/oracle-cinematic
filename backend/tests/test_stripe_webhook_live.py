"""Attack the Stripe webhook against real PostgreSQL: early, late, reordered
and concurrent duplicate events (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Events are signed exactly as Stripe signs them, so signature verification is
the real code path."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
SECRET = "whsec_" + "x" * 32


def _signed(event: dict) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


def test_webhook_ordering_and_duplicates(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg
    import httpx
    from fastapi import FastAPI

    import billing
    from db import connection

    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.include_router(billing.router)
    path = next(r.path for r in billing.router.routes if "webhook" in r.path)
    T = str(uuid.uuid4())
    sub, cus = f"sub_live_{uuid.uuid4().hex[:10]}", f"cus_live_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=6)
    sent_ids = []

    def event(kind: str, created: int, obj: dict, eid: str | None = None) -> dict:
        eid = eid or f"evt_Live{uuid.uuid4().hex[:12]}"
        sent_ids.append(eid)
        return {"id": eid, "object": "event", "type": kind, "created": created, "data": {"object": obj}}

    async def post(client, ev):
        body, headers = _signed(ev)
        return await client.post(path, content=body, headers=headers)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Stripe drill')", T, f"stripe-{T[:8]}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            # 1. invoice.paid arrives BEFORE checkout.completed → redeliver, not lost
            early = event("invoice.paid", 100, {"subscription": sub, "period_end": int(time.time()) + 86400})
            r = await post(client, early)
            assert r.status_code == 503, r.text
            assert await admin.fetchval("SELECT count(*) FROM stripe_webhook_events WHERE event_id=$1", early["id"]) == 0
            # 2. checkout completes
            r = await post(client, event("checkout.session.completed", 90, {
                "metadata": {"tenant_id": T}, "subscription": sub, "customer": cus, "payment_status": "paid"}))
            assert r.status_code == 200
            # 3. Stripe redelivers the early event → now applied
            r = await post(client, early)
            assert r.status_code == 200
            assert await admin.fetchval("SELECT status FROM subscriptions WHERE stripe_subscription_id=$1", sub) == "active"
            # 4. newer update, then an OLDER one arrives late: the older must not revert it
            assert (await post(client, event("customer.subscription.updated", 300,
                                             {"id": sub, "status": "past_due"}))).status_code == 200
            assert (await post(client, event("customer.subscription.updated", 200,
                                             {"id": sub, "status": "active"}))).status_code == 200
            assert await admin.fetchval("SELECT status FROM subscriptions WHERE stripe_subscription_id=$1", sub) == "past_due"
            # 5. the same event delivered 12 times concurrently → one effect, no errors
            dup = event("customer.subscription.updated", 400, {"id": sub, "status": "active"}, eid=f"evt_dup{T[:8]}")
            results = await asyncio.gather(*(post(client, dup) for _ in range(12)))
            assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
            assert await admin.fetchval("SELECT count(*) FROM stripe_webhook_events WHERE event_id=$1", dup["id"]) == 1
            assert sum(1 for r in results if r.json().get("duplicate")) == 11
            # 6. deletion is terminal even if it is "older"; nothing older resurrects it
            assert (await post(client, event("customer.subscription.deleted", 350, {"id": sub}))).status_code == 200
            assert (await post(client, event("customer.subscription.updated", 380,
                                             {"id": sub, "status": "active"}))).status_code == 200
            assert await admin.fetchval("SELECT status FROM subscriptions WHERE stripe_subscription_id=$1", sub) == "canceled"
            # 7. a forged signature is refused, never processed
            body, headers = _signed(event("customer.subscription.updated", 999, {"id": sub, "status": "active"}))
            headers["stripe-signature"] = headers["stripe-signature"][:-4] + "0000"
            assert (await client.post(path, content=body, headers=headers)).status_code == 400
            # 8. a correctly signed event whose id the ledger cannot hold is a
            # permanent 400, not a 500 that Stripe would redeliver for days
            odd = event("customer.subscription.updated", 1000, {"id": sub, "status": "active"},
                        eid="evt_has_underscores")
            assert (await post(client, odd)).status_code == 400
            assert await admin.fetchval("SELECT status FROM subscriptions WHERE stripe_subscription_id=$1", sub) == "canceled"
    finally:
        await admin.execute("DELETE FROM stripe_webhook_events WHERE event_id = ANY($1::text[])", sent_ids)
        await admin.execute("DELETE FROM subscriptions WHERE tenant_id=$1", T)
        await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        await admin.close()
        await connection.close_pool()
