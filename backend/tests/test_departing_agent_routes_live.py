"""A departing agent's number stops answering (external audit 22.5), against
real PostgreSQL.

When the successor already had their own route, offboarding only cleared the
departing route's forwarding: the number stayed active under the deactivated
agent, still taking calls and texts and filing leads.

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


def test_a_departing_agents_number_is_parked(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "d" * 64)
    asyncio.run(_scenario())


async def _set_state(admin, tenant, state):
    # The lifecycle trigger reserves transitions for the platform; bypassed for setup.
    await admin.execute("SET session_replication_role = replica")
    try:
        await admin.execute("UPDATE tenants SET lifecycle_state=$2 WHERE id=$1", tenant, state)
    finally:
        await admin.execute("SET session_replication_role = origin")


def _did() -> str:
    return "+1302" + str(secrets.randbelow(10 ** 7)).zfill(7)


async def _scenario():
    import asyncpg

    import inbound_voice
    import messaging_data
    import privacy_lifecycle as pl
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner, a, b = (f"{who}-{s}@live.test" for who in ("owner", "a", "b"))
    account = "AC" + secrets.token_hex(16)
    routes = {}
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                T, f"routes-{s}", f"Brokerage {s}")
            for agent, role in ((owner, "broker_owner"), (a, "agent"), (b, "agent")):
                await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                    "VALUES ($1,$2,$3,$2,'scrypt$x$y')", T, agent, role)
            for agent in (a, b):
                did = _did()
                routes[agent] = await admin.fetchrow(
                    "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, active, provider, "
                    "twilio_account_sid, provider_account_id, voice_caller_id_e164, voice_caller_id_verified, "
                    "agent_forward_e164, outbound_verification_status) "
                    "VALUES ($1,$2,$3,true,'twilio',$4,$5,$6,true,'+13025550188','verified') "
                    "RETURNING id, endpoint_key, inbound_did",
                    T, agent, did, account, account, did)
                await admin.execute(
                    "INSERT INTO messaging_routes (tenant_id, agent_id, active, provider, hosted_order_status) "
                    "VALUES ($1,$2,true,'telnyx','active')", T, agent)

        async def call_route(agent):
            r = routes[agent]
            return await inbound_voice.resolve_inbound_route(str(r["endpoint_key"]), r["inbound_did"], account)

        async def text_route(agent):
            return await messaging_data.resolve_messaging_route_by_number(routes[agent]["inbound_did"])

        # Both agents active: both numbers answer.
        assert (await call_route(a))["agent_id"] == a and (await text_route(a))["agent_id"] == a

        # Offboarding is refused while the brokerage is not active (a closure's
        # freeze list would bring a parked route back on withdrawal).
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
        await _set_state(admin, T, "suspended")
        with pytest.raises(pl.LifecycleError, match="suspended"):
            await pl.offboard_agent(octx, departing_agent_id=a, successor_agent_id=b,
                                    reason="leaving", preview=True)
        await _set_state(admin, T, "active")

        # Offboard A to B, who has their own number: A's is parked, not left live.
        preview = await pl.offboard_agent(octx, departing_agent_id=a, successor_agent_id=b,
                                          reason="leaving", preview=True)
        assert preview["telephony"] == "parked_successor_has_own_route"
        assert await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1", routes[a]["id"])
        done = await pl.offboard_agent(octx, departing_agent_id=a, successor_agent_id=b,
                                       reason="leaving", preview=False)
        assert done["telephony"] == "parked_successor_has_own_route"
        row = await admin.fetchrow("SELECT active, agent_id, voice_caller_id_verified, outbound_verification_status "
                                   "FROM telephony_routes WHERE id=$1", routes[a]["id"])
        assert row["active"] is False and row["agent_id"] == a
        # verification cleared: the verified-number index spans inactive rows
        assert row["voice_caller_id_verified"] is False and row["outbound_verification_status"] == "unverified"
        assert await admin.fetchval("SELECT active FROM messaging_routes WHERE tenant_id=$1 AND agent_id=$2",
                                    T, a) is False
        assert await call_route(a) is None and await text_route(a) is None
        # The successor's own number is untouched.
        assert (await call_route(b))["agent_id"] == b and (await text_route(b))["agent_id"] == b
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
