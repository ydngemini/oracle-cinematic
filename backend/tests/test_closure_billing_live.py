"""Closing and reopening a brokerage reach Stripe through a durable job and
report only what Stripe confirmed (external audit 22.1), against real
PostgreSQL.

The job row commits with the closure (and with the withdrawal), is claimable
while the brokerage is closing (privacy: jobs are), derives the wanted renewal
from tenants.lifecycle_state when it runs, confirms with a read-back, and on
its final attempt leaves `divergent` on the closure operation, which the
external_cleanup alert counts until a later confirmation settles it.

Stripe is a stub that records calls. Skipped unless ORACLE_LIVE_DB_ADMIN_DSN
is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


class _Stripe:
    def __init__(self):
        self.cancel, self.up, self.calls = False, False, []

    def modify(self, sub, *, cancel_at_period_end, max_network_retries):
        self.calls.append(("modify", sub, cancel_at_period_end))
        if not self.up:
            raise TimeoutError("api.stripe.com timed out")
        self.cancel = cancel_at_period_end
        return {"id": sub, "cancel_at_period_end": cancel_at_period_end}

    def retrieve(self, sub, *, max_network_retries):
        if not self.up:
            raise TimeoutError("api.stripe.com timed out")
        return {"id": sub, "cancel_at_period_end": self.cancel}


class _Reporter:
    def __init__(self, job: dict):
        self.job = job

    async def progress(self, *_a, **_k):
        return None


def test_closure_billing_against_real_postgres(monkeypatch):
    import recovery_mode
    import stripe

    fake = _Stripe()
    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    monkeypatch.setattr(stripe, "api_key", "sk_test_" + "stub")
    monkeypatch.setattr(stripe.Subscription, "modify", fake.modify)
    monkeypatch.setattr(stripe.Subscription, "retrieve", fake.retrieve)
    asyncio.run(_scenario(fake))


async def _scenario(fake):
    import asyncpg

    import component_health
    import privacy_lifecycle as pl
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner, name, sub = f"owner-{s}@live.test", f"Brokerage {s}", f"sub_live_{s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def jobs():
        return [dict(r) for r in await admin.fetch(
            "SELECT * FROM automation_jobs WHERE tenant_id=$1 AND job_type='privacy:billing_renewal' "
            "ORDER BY created_at", T)]

    async def run(job, attempt):
        job = {**job, "attempt_count": attempt}
        return await pl._billing_renewal_job(json.loads(job["payload"]), _Reporter(job))

    async def billing_of(op_id):
        return json.loads(await admin.fetchval(
            "SELECT progress->'billing' FROM privacy_operations WHERE id=$1", op_id))

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"bill-{s}", name)
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        await admin.execute("INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, "
                            "status) VALUES ($1,$2,$3,'active')", T, f"cus_{s}", sub)
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        # ── Stripe down at closure: closing, honest "pending", job committed
        closing = await pl.request_closure(octx, confirm_name=name, reason="closing")
        assert closing["state"] == "closing" and closing["billing"] == "pending"
        [close_job] = await jobs()
        assert close_job["state"] == "queued" and close_job["max_attempts"] == pl.BILLING_MAX_ATTEMPTS
        assert close_job["idempotency_key"] == f"privacy:billing:{closing['operation_id']}:close"
        # claimable although the brokerage is closing (privacy: jobs are)
        assert await admin.fetchval(
            "SELECT 1 FROM automation_jobs WHERE id=$1 AND (job_type LIKE 'privacy:%' OR NOT EXISTS ("
            "SELECT 1 FROM tenants t WHERE t.id = automation_jobs.tenant_id AND t.lifecycle_state <> 'active'))",
            close_job["id"])
        assert (await billing_of(closing["operation_id"]))["state"] == "pending"
        assert (await pl.tenant_lifecycle(octx))["billing_renewal"]["state"] == "pending"

        # ── the job, Stripe back: set, read back, confirmed ──────────────
        fake.up = True
        assert (await run(close_job, 2))["state"] == "confirmed"
        assert fake.cancel is True
        lifecycle = await pl.tenant_lifecycle(octx)
        assert lifecycle["billing_renewal"]["state"] == "confirmed"
        assert lifecycle["billing_renewal"]["desired"] == "stop_renewal"

        # ── withdraw with Stripe down; the CLOSE job runs again afterwards ─
        fake.up = False
        reopened = await pl.withdraw_closure(octx, reason="changed mind")
        assert reopened["state"] == "active" and reopened["billing"] == "pending"
        assert len(await jobs()) == 2
        fake.up = True
        assert (await run(close_job, 3))["desired"] == "renew"    # derived from 'active' now
        assert fake.cancel is False
        assert (await billing_of(closing["operation_id"]))["state"] == "confirmed"

        # ── final attempt with Stripe down: divergent + the alert ─────────
        fake.up = False
        again = await pl.request_closure(octx, confirm_name=name, reason="closing for good")
        third = (await jobs())[-1]
        out = await run(third, third["max_attempts"])
        assert out["state"] == "divergent" and out["_terminal_state"] == "partial"
        assert (await billing_of(again["operation_id"]))["state"] == "divergent"
        health = (await component_health._db_components())["external_cleanup"]
        assert health["state"] == component_health.DEGRADED and health["detail"]["billing_divergent"] >= 1

        # ── a later confirmation settles it ───────────────────────────────
        fake.up = True
        assert (await run(third, 1))["state"] == "confirmed" and fake.cancel is True
        assert (await billing_of(again["operation_id"]))["state"] == "confirmed"
        assert await admin.fetchval(
            "SELECT count(*) FROM privacy_operations WHERE tenant_id=$1 AND kind='closure' "
            "AND progress->'billing'->>'state' = 'divergent'", T) == 0

        # ── review of 22.1: withdraw and close again before anything confirms
        # The older closure's 'pending' can never be confirmed (every later
        # job writes the newest closure): it is superseded at once, and only
        # each brokerage's latest closure counts towards the alert.
        fake.up = False
        await pl.withdraw_closure(octx, reason="changed mind again")          # again: {renew, pending}
        assert (await billing_of(again["operation_id"]))["state"] == "pending"
        third_close = await pl.request_closure(octx, confirm_name=name, reason="closing, final answer")
        assert (await billing_of(again["operation_id"]))["state"] == "superseded"
        assert (await billing_of(third_close["operation_id"]))["state"] == "pending"

        async def stuck():
            return (await component_health._db_components())["external_cleanup"]["detail"]["stuck_pending"]

        base = await stuck()
        await admin.execute(
            "UPDATE privacy_operations SET progress = jsonb_set(progress, '{billing,state}', '\"pending\"'), "
            "updated_at = now() - interval '7 hours' WHERE id=$1", again["operation_id"])
        assert await stuck() == base                       # an older closure is history
        await admin.execute("UPDATE privacy_operations SET updated_at = now() - interval '7 hours' WHERE id=$1",
                            third_close["operation_id"])
        assert await stuck() == base + 1                   # the latest one, stuck, does page

        # ── review of 22.1: a closure blocked by a legal hold, then withdrawn
        # No running closure operation: the record goes to the latest one
        # (the blocked closure being withdrawn), which tenant_lifecycle reads.
        await admin.execute("INSERT INTO legal_holds (tenant_id, scope, reason, placed_by) "
                            "VALUES ($1,'tenant','litigation','counsel')", T)
        assert await pl.begin_erasure(T, requested_by="test") is None
        assert await admin.fetchval("SELECT state FROM privacy_operations WHERE id=$1",
                                    third_close["operation_id"]) == "blocked_legal_hold"
        fake.up = True
        withdrawn = await pl.withdraw_closure(octx, reason="settled")
        assert withdrawn["billing"] == "confirmed" and fake.cancel is False
        assert await billing_of(third_close["operation_id"]) == {
            **await billing_of(third_close["operation_id"]), "desired": "renew", "state": "confirmed"}
        lifecycle = await pl.tenant_lifecycle(octx)
        assert lifecycle["billing_renewal"]["desired"] == "renew"
        assert lifecycle["billing_renewal"]["state"] == "confirmed"
        assert (await jobs())[-1]["idempotency_key"] == f"privacy:billing:{third_close['operation_id']}:withdraw"
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in await admin.fetch(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                    "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
