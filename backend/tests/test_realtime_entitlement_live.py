"""A paid realtime phone model opens only for a brokerage that is entitled and
open (external audit 21.4, P0), against real PostgreSQL.

The realtime admission (commands_api._realtime_refusal) read only the AI
budget, which says nothing about the subscription or the brokerage's
lifecycle: a cancelled or suspended brokerage whose phone routes were left
active still got a billable ElevenLabs / Qwen session on every inbound call.
Every bridge route admits through this one function before opening a model.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_no_realtime_model_for_a_cancelled_or_suspended_brokerage(monkeypatch):
    monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import ai_budget
    import commands_api
    from db import connection

    admitted_by_budget = []

    async def budget_ok(ctx, **_k):
        admitted_by_budget.append(ctx.tenant_id)   # the AI budget alone would admit every call

    monkeypatch.setattr(ai_budget, "admission", budget_ok)

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"rt-{s}", f"B {s}")
        await admin.execute("INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status) "
                            "VALUES ($1,$2,$3,'active')", T, f"cus_{s}", f"sub_{s}")

        assert await commands_api._realtime_refusal(T) is None                     # paying and open: admitted

        await admin.execute("UPDATE subscriptions SET status='canceled' WHERE tenant_id=$1", T)
        refused = await commands_api._realtime_refusal(T)
        assert refused is not None and refused.code == commands_api.REALTIME_NOT_ENTITLED_CODE

        await admin.execute("UPDATE subscriptions SET status='active' WHERE tenant_id=$1", T)
        await admin.execute("SET session_replication_role = replica")   # the lifecycle trigger guards transitions
        await admin.execute("UPDATE tenants SET lifecycle_state='suspended' WHERE id=$1", T)
        await admin.execute("SET session_replication_role = origin")
        refused = await commands_api._realtime_refusal(T)
        assert refused is not None and refused.code == commands_api.REALTIME_NOT_OPEN_CODE  # paid, but suspended

        # Unreadable status: refused, never a pass.
        import billing

        async def broken(_ctx):
            raise RuntimeError("billing status unavailable")

        monkeypatch.setattr(billing, "entitlement_refusal", broken)
        refused = await commands_api._realtime_refusal(T)
        assert refused is not None and refused.code == commands_api.REALTIME_ENTITLEMENT_UNKNOWN_CODE
        assert admitted_by_budget == [T]          # the budget was asked only for the admitted call
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            await admin.execute("DELETE FROM subscriptions WHERE tenant_id=$1", T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
