"""Suspending an agent silences their business line (owner decision
D-2026-11-16-01; external audit 22.5), against real PostgreSQL.

Inbound routing never checked that a route's agent was still active, so a
suspended agent's number kept answering calls and filing texts. Now new
inbound calls and texts resolve only for an active agent; reinstating the
agent brings the line back, and a STOP to the silent number is still owned.

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


def test_a_suspended_agents_line_is_silent_until_reinstated(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "b" * 64)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import brokerage_onboarding
    import inbound_voice
    import messaging_data
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner, agent = f"owner-{s}@live.test", f"agent-{s}@live.test"
    account = "AC" + secrets.token_hex(16)
    did = "+1302" + str(secrets.randbelow(10 ** 7)).zfill(7)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                T, f"line-{s}", f"Brokerage {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            agent_id = await admin.fetchval(
                "INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                "VALUES ($1,$2,'agent',$2,'scrypt$x$y') RETURNING id", T, agent)
            route = await admin.fetchrow(
                "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, active, provider, "
                "twilio_account_sid, provider_account_id, voice_caller_id_e164, voice_caller_id_verified, "
                "agent_forward_e164, outbound_verification_status) "
                "VALUES ($1,$2,$3,true,'twilio',$4,$5,$6,true,'+13025550188','verified') "
                "RETURNING endpoint_key", T, agent, did, account, account, did)
            await admin.execute(
                "INSERT INTO messaging_routes (tenant_id, agent_id, active, provider, hosted_order_status) "
                "VALUES ($1,$2,true,'telnyx','active')", T, agent)

        async def answers() -> tuple[bool, bool]:
            call = await inbound_voice.resolve_inbound_route(str(route["endpoint_key"]), did, account)
            text = await messaging_data.resolve_messaging_route_by_number(did)
            return call is not None, text is not None

        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
        body = brokerage_onboarding.MemberStatusChange(reason="test")
        assert await answers() == (True, True)

        await brokerage_onboarding.suspend_member(agent_id, body, octx)
        assert await answers() == (False, False)                       # silent
        owners = await messaging_data.resolve_opt_out_owners_by_number(did)
        assert any(o["agent_id"] == agent for o in owners)             # a STOP is still owned

        await brokerage_onboarding.reinstate_member(agent_id, body, octx)
        assert await answers() == (True, True)                         # back on reinstatement
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
