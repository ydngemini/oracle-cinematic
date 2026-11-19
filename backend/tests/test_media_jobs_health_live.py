"""Audit 30.9: the media_jobs component reads the voice and reconstruction
job tables (job_queue reads automation_jobs only). Real PostgreSQL, one
rolled-back transaction. Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_media_jobs_query_sees_a_backlog_and_jobs_that_stopped_moving():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import component_health as ch

    conn = await asyncpg.connect(DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        before = dict(await conn.fetchrow(ch._SQL_MEDIA_JOBS))
        tenant = str(uuid.uuid4())
        await conn.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Media jobs live')",
                           tenant, f"mj-{tenant[:8]}")
        # Only the job rows matter here, not their lead/listing parents.
        await conn.execute("SET LOCAL session_replication_role = replica")
        voice = ("INSERT INTO voice_walkthrough_jobs (tenant_id, lead_id, created_by, original_filename, suffix, "
                 "audio, audio_bytes, status, created_at, updated_at) VALUES ($1, gen_random_uuid(), 'a@x.test', "
                 "'w.m4a', '.m4a', '\\x00'::bytea, 1, $2, now() - make_interval(mins => $3), "
                 "now() - make_interval(mins => $3))")
        await conn.execute(voice, tenant, "queued", 20)
        await conn.execute(voice, tenant, "running", 45)
        recon = ("INSERT INTO reconstruction_jobs (tenant_id, lead_id, status, created_at, updated_at) "
                 "VALUES ($1, gen_random_uuid(), $2, now() - make_interval(mins => $3), now() - make_interval(mins => $3))")
        await conn.execute(recon, tenant, "queued", 200)
        await conn.execute(recon, tenant, "running", 15)
        after = dict(await conn.fetchrow(ch._SQL_MEDIA_JOBS))
        assert after["voice_hung"] == before["voice_hung"] + 1
        assert after["recon_silent"] == before["recon_silent"] + 1
        assert after["voice_oldest_queued_s"] >= 20 * 60
        assert after["recon_oldest_queued_s"] >= 200 * 60
        state = ch._media_jobs(after, worker_live=True)
        assert state["state"] == ch.DEGRADED and "voice walkthroughs waiting too long" in state["summary"]
        assert "3D reconstructions waiting too long" in state["summary"]
    finally:
        await tx.rollback()
        await conn.close()
