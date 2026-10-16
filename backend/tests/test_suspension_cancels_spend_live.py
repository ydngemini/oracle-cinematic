"""Suspending an agent cancels their queued spend (T03.16, L03-11), against
real PostgreSQL through the real suspend route (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Pins: a suspended agent's queued GPU reconstruction and voice walkthrough
are failed with the suspension reason; a colleague's queued jobs and the
agent's own running or finished jobs are untouched.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_suspension_cancels_the_agents_queued_spend():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import brokerage_onboarding as bo
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = str(uuid.uuid4())
    tag = tenant[:8]
    owner = TenantContext(agent_id=f"owner-{tag}@spend.test", tenant_id=tenant, role=Role.BROKER_OWNER)
    agent, colleague = f"agent-{tag}@spend.test", f"colleague-{tag}@spend.test"

    async def job(table, who, status="queued"):
        if table == "reconstruction_jobs":  # one active build per property: a lead each
            own_lead = await admin.fetchval(
                "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score) VALUES ($1,$2,'DE',0) RETURNING id",
                uuid.UUID(tenant), f"P-{uuid.uuid4().hex[:10]}")
            return await admin.fetchval(
                "INSERT INTO reconstruction_jobs (tenant_id, lead_id, status, created_by) VALUES ($1,$2,$3,$4) RETURNING id",
                uuid.UUID(tenant), own_lead, status, who)
        return await admin.fetchval(
            "INSERT INTO voice_walkthrough_jobs (tenant_id, lead_id, created_by, original_filename, suffix, "
            "audio_bytes, status) VALUES ($1,$2,$3,'walk.m4a','m4a',0,$4) RETURNING id",
            uuid.UUID(tenant), lead, who, status)

    async def status(table, jid):
        return await admin.fetchval(f"SELECT status FROM {table} WHERE id=$1", jid)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Spend')", uuid.UUID(tenant), f"spend-{tag}")
        users = {}
        for login, role in ((owner.agent_id, "broker_owner"), (agent, "agent"), (colleague, "agent")):
            users[login] = await admin.fetchval(
                "INSERT INTO users (tenant_id, agent_id, role, email, is_active) VALUES ($1,$2,$3,$2,true) RETURNING id",
                uuid.UUID(tenant), login, role)
        lead = await admin.fetchval(
            "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score) VALUES ($1,$2,'DE',0) RETURNING id",
            uuid.UUID(tenant), f"P-{tag}")
        mine = {t: await job(t, agent) for t in ("reconstruction_jobs", "voice_walkthrough_jobs")}
        theirs = {t: await job(t, colleague) for t in ("reconstruction_jobs", "voice_walkthrough_jobs")}
        running = await job("reconstruction_jobs", agent, status="running")

        await bo.suspend_member(users[agent], bo.MemberStatusChange(reason="left the brokerage"), ctx=owner)

        for table, jid in mine.items():
            assert await status(table, jid) == "failed", table
            error = await admin.fetchval(f"SELECT error FROM {table} WHERE id=$1", jid)
            assert "left the brokerage" in (error or "")
        for table, jid in theirs.items():
            assert await status(table, jid) == "queued", table
        assert await status("reconstruction_jobs", running) == "running"
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("reconstruction_jobs", "voice_walkthrough_jobs", "agent_routing_state", "telephony_routes",
                          "team_memberships", "leads", "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
