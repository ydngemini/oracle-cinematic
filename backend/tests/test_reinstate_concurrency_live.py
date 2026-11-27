"""Two owners' reinstatements in one brokerage at the same moment both succeed
(L17-59, re-review of PR #190), against real PostgreSQL.

_reinstate locks the tenants row before it switches the member back on. It
used to take it FOR SHARE; the users UPDATE then fires users_seat_limit
(0161), which takes the same row FOR UPDATE on every activation, so two
concurrent reinstatements each waited on the other's share lock and one died
with a deadlock (a 500 to the owner). Also probed: a reinstatement racing an
offboarding, which takes the row FOR SHARE but never activates anyone.

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

ROUNDS = 12


def test_concurrent_reinstatements_in_one_brokerage_never_deadlock(monkeypatch):
    import recovery_mode

    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import brokerage_onboarding as bo
    import privacy_lifecycle as pl
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=2, max_size=6)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"rrc-{s}", f"B {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
        why = bo.MemberStatusChange(reason="owner decision")

        async def agent(tag):
            who = f"{tag}-{s}@live.test"
            uid = await admin.fetchval(
                "INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                "VALUES ($1,$2,'agent',$2,'scrypt$x$y') RETURNING id", T, who)
            await admin.execute("INSERT INTO team_memberships (tenant_id, user_id, team_name, status) "
                                "VALUES ($1,$2,'Team','active')", T, uid)
            return uid, who

        (a, _), (b, _) = await agent("a"), await agent("b")

        async def active(uid):
            return await admin.fetchval("SELECT is_active FROM users WHERE id=$1", uid)

        # two reinstatements at once, repeatedly: neither raises, both active
        failures = []
        for _ in range(ROUNDS):
            for uid in (a, b):
                await bo.suspend_member(uid, why, ctx=octx)
            results = await asyncio.gather(bo.reinstate_member(a, why, ctx=octx),
                                           bo.reinstate_member(b, why, ctx=octx),
                                           return_exceptions=True)
            failures += [type(r).__name__ for r in results if isinstance(r, BaseException)]
            assert await active(a) and await active(b), results
        assert failures == [], failures

        # a reinstatement racing an offboarding (tenants FOR SHARE, then only
        # deactivations): neither raises
        for i in range(3):
            await bo.suspend_member(a, why, ctx=octx)
            _, leaving = await agent(f"c{i}")
            results = await asyncio.gather(
                bo.reinstate_member(a, why, ctx=octx),
                pl.offboard_agent(octx, departing_agent_id=leaving, successor_agent_id=owner,
                                  reason="left", preview=False),
                return_exceptions=True)
            assert not any(isinstance(r, BaseException) for r in results), results
            assert await active(a)
            assert await admin.fetchval("SELECT is_active FROM users WHERE agent_id=$1", leaving) is False
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
