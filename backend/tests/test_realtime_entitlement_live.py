"""A paid realtime phone model opens only for a brokerage that is entitled and
open (external audit 21.4, P0), against real PostgreSQL.

The realtime admission (commands_api._realtime_refusal) read only the AI
budget, which says nothing about the subscription or the brokerage's
lifecycle: a cancelled or suspended brokerage whose phone routes were left
active still got a billable ElevenLabs / Qwen session on every inbound call.
Every bridge route admits through this one function before opening a model.
The turn-based paid replies (_generate_voice_reply: Twilio <Gather>, custom
call, ACS speech) read the same entitlement every turn, and the inbound
webhook resolves no route for a brokerage that is not open, so nothing is
written for it.

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


def test_turn_replies_and_inbound_routes_stop_for_a_brokerage_that_cannot_spend(monkeypatch):
    monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
    asyncio.run(_turns_and_routes(monkeypatch))


async def _turns_and_routes(monkeypatch):
    import asyncpg

    import ai_chat_agent
    import inbound_voice
    import llm_gateway
    from db import connection

    asked = []

    async def complete(_prompt, **_kwargs):
        asked.append(1)
        return "Happy to help."

    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers",
                        lambda: [llm_gateway.Provider(name="fireworks", model="fireworks_ai/m")])
    monkeypatch.setattr(llm_gateway, "complete", complete)

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    agent = f"agent-{s}@live.test"
    account = "AC" + secrets.token_hex(16)
    did = "+1302" + str(secrets.randbelow(10 ** 7)).zfill(7)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def lifecycle(state):
        await admin.execute("SET session_replication_role = replica")   # the lifecycle trigger guards transitions
        try:
            await admin.execute("UPDATE tenants SET lifecycle_state=$2, erase_after=now() + interval '30 days', "
                                "closure_requested_at=now() WHERE id=$1", T, state)
        finally:
            await admin.execute("SET session_replication_role = origin")

    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"rt2-{s}", f"B {s}")
            await admin.execute("INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, "
                                "status) VALUES ($1,$2,$3,'active')", T, f"cus_{s}", f"sub_{s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'agent',$2,'scrypt$x$y')", T, agent)
            route = await admin.fetchrow(
                "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, active, provider, "
                "twilio_account_sid, provider_account_id, voice_caller_id_e164, voice_caller_id_verified, "
                "agent_forward_e164, outbound_verification_status) "
                "VALUES ($1,$2,$3,true,'twilio',$4,$5,$6,true,'+13025550188','verified') "
                "RETURNING endpoint_key", T, agent, did, account, account, did)

        async def answers() -> bool:
            return await inbound_voice.resolve_inbound_route(str(route["endpoint_key"]), did, account) is not None

        # Paying and open: the line answers and a spoken turn is answered.
        assert await answers()
        assert await ai_chat_agent._generate_voice_reply("+13025550100", "hi", tenant_id=T) == "Happy to help."
        assert asked == [1]

        # Cancelled: the turn is refused before any model.
        await admin.execute("UPDATE subscriptions SET status='canceled' WHERE tenant_id=$1", T)
        with pytest.raises(llm_gateway.SpendRefused) as refused:
            await ai_chat_agent._generate_voice_reply("+13025550100", "hi", tenant_id=T)
        assert refused.value.code == "BROKERAGE_NOT_ENTITLED" and asked == [1]
        await admin.execute("UPDATE subscriptions SET status='active' WHERE tenant_id=$1", T)

        # Suspended or closing: no route (nothing is written) and no reply.
        for state in ("suspended", "closing"):
            await lifecycle(state)
            assert not await answers(), f"a {state} brokerage's line still resolved"
            with pytest.raises(llm_gateway.SpendRefused) as refused:
                await ai_chat_agent._generate_voice_reply("+13025550100", "hi", tenant_id=T)
            assert refused.value.code == "BROKERAGE_NOT_OPEN" and asked == [1]
        await lifecycle("active")
        assert await answers()                                       # back when it reopens
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("telephony_routes", "users", "subscriptions"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
