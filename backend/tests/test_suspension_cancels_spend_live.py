"""Suspending an agent cancels their queued spend (T03.16, L03-11), against
real PostgreSQL through the real suspend route (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Pins: a suspended agent's queued GPU reconstruction and voice walkthrough
are failed, and their queued studio video cancelled (0166, L03-13), with the
suspension reason; a colleague's queued jobs and the agent's own running or
finished jobs are untouched. The video's requester is recorded by the real
create route.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_suspension_cancels_the_agents_queued_spend(monkeypatch):
    import types

    import video_providers

    # A configured provider, so the real create route gets as far as the insert.
    ready = types.SimpleNamespace(available=lambda: (True, ""), check_seconds=lambda _s: None)
    monkeypatch.setattr(video_providers, "get_provider", lambda: ready)
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
        if table == "video_studio_jobs":
            return await admin.fetchval(
                "INSERT INTO video_studio_jobs (tenant_id, kind, source, size, status, created_by) "
                "VALUES ($1,'text','{}'::jsonb,'720x1280',$2,$3) RETURNING id", uuid.UUID(tenant), status, who)
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
        tables = ("reconstruction_jobs", "voice_walkthrough_jobs", "video_studio_jobs")
        mine = {t: await job(t, agent) for t in tables}
        theirs = {t: await job(t, colleague) for t in tables}
        running = await job("reconstruction_jobs", agent, status="running")
        generating = await job("video_studio_jobs", agent, status="generating")

        # The real create route records who asked for the video.
        import video_studio_api as vs

        agent_ctx = TenantContext(agent_id=agent, tenant_id=tenant, role=Role.AGENT)
        created = await vs.create_video_job(
            vs.VideoJobCreate(kind="text", size="720x1280", script="A short tour.", lead_id=lead), ctx=agent_ctx)
        assert await admin.fetchval("SELECT created_by FROM video_studio_jobs WHERE id=$1::uuid",
                                    str(created["id"])) == agent
        mine["video_studio_jobs (route)"] = uuid.UUID(str(created["id"]))

        await bo.suspend_member(users[agent], bo.MemberStatusChange(reason="left the brokerage"), ctx=owner)

        for name, jid in mine.items():
            table = name.split(" ")[0]
            expected = "cancelled" if table == "video_studio_jobs" else "failed"
            assert await status(table, jid) == expected, name
            error = await admin.fetchval(f"SELECT error FROM {table} WHERE id=$1", jid)
            assert "left the brokerage" in (error or ""), name
        for table, jid in theirs.items():
            assert await status(table, jid) == "queued", table
        assert await status("reconstruction_jobs", running) == "running"
        assert await status("video_studio_jobs", generating) == "generating"
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("reconstruction_jobs", "voice_walkthrough_jobs", "video_studio_jobs", "agent_routing_state", "telephony_routes",
                          "team_memberships", "leads", "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
