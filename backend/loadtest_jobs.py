"""A no-op durable-job handler that exists ONLY in a load-test environment.

The job-queue capacity test (performance/jobs_load.py) needs a job type with
no side effects — every real handler sends, writes or calls something. This
registers `loadtest:noop` when, and only when, ORACLE_ENV=loadtest; in any
other environment importing this module does nothing, so a production worker
can never run it (and a stray `loadtest:noop` row there fails as
NO_JOB_HANDLER instead of executing).

Each execution records its logical key in Valkey (SADD). A second execution of
the same logical job — the thing FOR UPDATE SKIP LOCKED and the lease token
exist to prevent — increments `perf:noop:duplicates`, which the test reads.
"""

from __future__ import annotations

import asyncio

import config
from automation_jobs import register_handler

JOB_TYPE = "loadtest:noop"


async def _noop(payload: dict, reporter) -> dict:
    await asyncio.sleep(min(float(payload.get("ms", 50)), 5_000) / 1000)
    from rate_limit_middleware import get_redis_client

    redis = await get_redis_client()
    if redis is not None:
        if not await redis.sadd("perf:noop:executed", str(payload.get("key"))):
            await redis.incr("perf:noop:duplicates")
    return {"ok": True}


if config.ORACLE_ENV == "loadtest":
    register_handler(JOB_TYPE, _noop)
