"""Job-claim aging against real PostgreSQL (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Strict priority starved lower-priority work for as long as higher-priority
jobs kept arriving: 5+ minutes in the first-10-brokerage drill, including
privacy exports and erasures. Now a job ready for longer than the starvation
bound is claimed first, oldest first. A retry counts as fresh until it has
itself waited that long, and fresh work still goes in priority order.

The jobs live on a throwaway queue name, so no running worker can take them.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_starved_job_is_claimed_before_fresh_higher_priority_work():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import automation_jobs
    from db import connection

    queue = f"aging-test-{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    try:
        tenant = await admin.fetchval("SELECT id FROM tenants WHERE lifecycle_state='active' ORDER BY created_at LIMIT 1")

        async def job(name, priority, scheduled_ago_s, retry_ago_s=None):
            return await admin.fetchval(
                """INSERT INTO automation_jobs (tenant_id, job_type, state, payload, idempotency_key, created_by,
                                                max_attempts, queue_name, priority, scheduled_at, next_retry_at)
                   VALUES ($1, 'loadtest:noop', 'queued', '{}'::jsonb, $2, 'aging-test', 3, $3, $4,
                           now() - make_interval(secs => $5),
                           CASE WHEN $6::float8 IS NULL THEN NULL ELSE now() - make_interval(secs => $6::float8) END)
                   RETURNING id""",
                tenant, f"{queue}:{name}", queue, priority, float(scheduled_ago_s), retry_ago_s)

        starved = await job("starved", 90, 300)
        retry_just_ready = await job("retry", 95, 900, retry_ago_s=5)
        fresh_urgent = await job("fresh", 10, 1)

        order = []
        for _ in range(3):
            row = await automation_jobs.claim_next_job("aging-test-worker", queue_name=queue)
            assert row is not None
            order.append(str(row["id"]))
        assert order == [str(starved), str(fresh_urgent), str(retry_just_ready)], order
        assert await automation_jobs.claim_next_job("aging-test-worker", queue_name=queue) is None
    finally:
        await admin.execute("DELETE FROM automation_job_attempts WHERE job_id IN "
                            "(SELECT id FROM automation_jobs WHERE queue_name=$1)", queue)
        await admin.execute("DELETE FROM automation_jobs WHERE queue_name=$1", queue)
        await admin.close()
        await connection.close_pool()
