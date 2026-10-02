"""Worker crash mid-job: does every durable job complete once — or fail clearly?

  enqueue   <N> loadtest:noop jobs (3 s each; no side effects; perf only)
  status    print counts by state + Valkey executed/duplicate counters

The driver (run_worker_crash_drill.sh) enqueues, waits until jobs are
running, `docker kill`s the worker, restarts it, and polls `status` until the
queue drains. Expected: every job succeeds; jobs that were mid-run are
re-claimed when their 120 s lease lapses; a duplicate is possible only in the
instant between the handler finishing and its completion being written
(at-least-once delivery — which is why every real side-effecting handler
records intent and is reconciled, not blindly rerun).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, "/app")
RUN = os.getenv("DRILL_RUN", "crash")


async def main(cmd: str, n: int) -> None:
    from automation_jobs import enqueue_job
    from db import connection
    from db.connection import tenant_tx
    from rate_limit_middleware import get_redis_client
    from tenancy import Role, TenantContext

    ctx = TenantContext(agent_id="crash-drill", tenant_id="00000000-0000-0000-0000-000000000000",
                        role=Role.PLATFORM_ADMIN)
    await connection.init_pool(min_size=1, max_size=3)
    try:
        if cmd == "enqueue":
            redis = await get_redis_client()
            await redis.delete("perf:noop:executed", "perf:noop:duplicates")
            for i in range(n):
                await enqueue_job(ctx, job_type="loadtest:noop", payload={"ms": 3000, "key": f"{RUN}-{i}"},
                                  idempotency_key=f"crash-drill:{RUN}:{i}", created_by="crash-drill",
                                  max_attempts=5)
            print(json.dumps({"enqueued": n}))
            return
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                "SELECT state, count(*) AS n, sum(attempt_count) AS attempts FROM automation_jobs "
                "WHERE job_type='loadtest:noop' AND idempotency_key LIKE $1 GROUP BY state",
                f"crash-drill:{RUN}:%")
        redis = await get_redis_client()
        executed = await redis.scard("perf:noop:executed")
        dups = int(await redis.get("perf:noop:duplicates") or 0)
        print(json.dumps({"states": {r["state"]: int(r["n"]) for r in rows},
                          "attempts": sum(int(r["attempts"] or 0) for r in rows),
                          "executed_distinct": executed, "duplicates": dups}))
    finally:
        await connection.close_pool()


if __name__ == "__main__":
    if os.getenv("NEOH_LOAD_TEST_ALLOWED") != "1":
        sys.exit("refusing: local perf topology only")
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0))
