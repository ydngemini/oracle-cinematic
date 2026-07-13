"""
data_integrations/periodic.py — interval scheduler (the "auto-update" heartbeat).

A single async loop wakes on a heartbeat tick (default hourly) and runs each
registered task only when it is DUE per its own cadence. That is how state +
city data stays auto-updating without a brute-force hourly full re-scrape:

  * Heavy parcel harvests refresh on a slow cadence (matching the 7-day assessor
    TTL in cache.py) and pull a BOUNDED batch per state per cycle — polite to the
    source portals, never a full national reload every hour.
  * Cheap tasks (coverage snapshot) run every tick, so there is always a fresh
    freshness signal within the hour.

Per-source rate limiting + circuit breakers already live in harvesters/base.py
and data_integrations/scheduler.py — this module only decides WHEN to run.

The parcel task is REGISTRY-driven (harvesters.firehose.REGISTRY), so states the
harvest-states pipeline adds are picked up automatically on the next cycle.

Enable in production (it is OFF by default so dev boots never scrape unexpectedly):

    ORACLE_SCHEDULER_ENABLED=1
    ORACLE_INGEST_TENANT_ID=<uuid>        # required for the parcel harvest task
    ORACLE_SCHED_TICK_SECONDS=3600        # heartbeat, default 1h
    ORACLE_HARVEST_INTERVAL_HOURS=24      # parcel refresh cadence, default 24h
    ORACLE_HARVEST_MAX_PER_STATE=5000     # bounded batch per state per cycle
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("oracle.di.periodic")

TICK_SECONDS = int(os.getenv("ORACLE_SCHED_TICK_SECONDS", "3600"))
SCHEDULER_ENABLED = os.getenv("ORACLE_SCHEDULER_ENABLED", "0") == "1"


@dataclass
class PeriodicTask:
    """One recurring job: a coroutine factory plus its cadence."""
    name: str
    interval_s: float
    run: Callable[[], Awaitable[dict]]
    enabled: bool = True
    # mutable runtime state
    next_due: float = 0.0           # monotonic deadline; 0 = due immediately
    last_run_wall: Optional[float] = None   # epoch seconds (for status display)
    last_status: str = "pending"
    last_result: dict = field(default_factory=dict)
    runs: int = 0
    failures: int = 0

    def is_due(self, now: float) -> bool:
        return self.enabled and now >= self.next_due

    def schedule_next(self, now: float) -> None:
        self.next_due = now + self.interval_s


class PeriodicScheduler:
    """Heartbeat loop that enqueues due work into the durable job queue.

    Every ECS replica may run this lightweight producer.  The interval-bucket
    idempotency key collapses all replicas onto one PostgreSQL job, and leased
    workers perform the actual scrape exactly once per successful attempt.
    """

    def __init__(self, tick_s: int = TICK_SECONDS):
        self.tick_s = max(5, tick_s)
        self._tasks: dict[str, PeriodicTask] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._ticks = 0

    def register(self, task: PeriodicTask) -> None:
        if task.interval_s < 6 * 3600 and "harvest" in task.name:
            logger.warning(
                "Task '%s' cadence is %.1fh (<6h) — aggressive for parcel portals; "
                "ensure a bounded per-state cap so the source isn't hammered.",
                task.name, task.interval_s / 3600,
            )
        self._tasks[task.name] = task
        self._register_job_handler(task)

    @staticmethod
    def _register_job_handler(task: PeriodicTask) -> None:
        from automation_jobs import register_handler

        async def handler(payload: dict, reporter) -> dict:
            await reporter.progress(5, f"{task.name}: starting")
            result = await task.run()
            await reporter.progress(95, f"{task.name}: finalizing")
            return result if isinstance(result, dict) else {"result": str(result)}

        register_handler(f"periodic:{task.name}", handler)

    async def start(self) -> None:
        if not SCHEDULER_ENABLED:
            logger.info("PeriodicScheduler disabled (set ORACLE_SCHEDULER_ENABLED=1 to enable).")
            return
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._loop(), name="di-periodic")
        logger.info(
            "PeriodicScheduler started — tick=%ds, tasks=%s",
            self.tick_s, ", ".join(self._tasks) or "(none)",
        )

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None

    async def _loop(self) -> None:
        while self._running:
            self._ticks += 1
            now = time.monotonic()
            due = [t for t in self._tasks.values() if t.is_due(now)]
            for task in due:
                await self._run_task(task)
            try:
                await asyncio.sleep(self.tick_s)
            except asyncio.CancelledError:
                break

    async def _run_task(self, task: PeriodicTask) -> None:
        """Enqueue one interval-bucketed task, isolating producer failures."""
        t0 = time.monotonic()
        try:
            from automation_jobs import enqueue_job, interval_idempotency_key
            from tenancy import Role, TenantContext

            tenant_id = os.getenv("ORACLE_INGEST_TENANT_ID") or os.getenv(
                "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
            )
            now = datetime.now(timezone.utc)
            bucket_number = int(now.timestamp() // max(1, int(task.interval_s)))
            bucket = f"{int(task.interval_s)}:{bucket_number}"
            ctx = TenantContext(
                agent_id="periodic-scheduler",
                tenant_id=tenant_id,
                role=Role.PLATFORM_ADMIN,
            )
            job, created = await enqueue_job(
                ctx,
                job_type=f"periodic:{task.name}",
                payload={"task": task.name, "scheduled_bucket": bucket},
                idempotency_key=interval_idempotency_key(task.name, tenant_id, bucket),
                created_by="periodic-scheduler",
                priority=40,
            )
            task.last_status = "queued" if created else "deduplicated"
            task.last_result = {
                "job_id": job["id"],
                "created": created,
                "state": job["state"],
                "scheduled_bucket": bucket,
            }
            task.runs += 1
        except Exception as exc:  # noqa: BLE001 — one task must not sink the loop
            task.last_status = f"error: {str(exc)[:160]}"
            task.failures += 1
            logger.error("[periodic] task '%s' failed: %s", task.name, exc)
        finally:
            now = time.monotonic()
            task.last_run_wall = time.time()
            task.schedule_next(now)
            logger.info("[periodic] '%s' -> %s in %.1fs (next in %.0fmin)",
                        task.name, task.last_status, now - t0, task.interval_s / 60)

    async def run_now(self, name: str) -> dict:
        """Queue a controlled manual rerun through the same durable path."""
        task = self._tasks.get(name)
        if not task:
            raise KeyError(f"No such task: {name}. Known: {list(self._tasks)}")
        from automation_jobs import enqueue_job
        from tenancy import Role, TenantContext

        tenant_id = os.getenv("ORACLE_INGEST_TENANT_ID") or os.getenv(
            "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        )
        request_id = str(time.time_ns())
        ctx = TenantContext(
            agent_id="manual-harvest",
            tenant_id=tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        job, _ = await enqueue_job(
            ctx,
            job_type=f"periodic:{task.name}",
            payload={"task": task.name, "manual": True, "request_id": request_id},
            idempotency_key=f"manual:{task.name}:{request_id}",
            created_by="manual-harvest",
            priority=20,
        )
        return {"job_id": job["id"], "state": job["state"], "manual": True}

    def status(self) -> dict:
        return {
            "enabled": SCHEDULER_ENABLED,
            "running": self._running,
            "tick_seconds": self.tick_s,
            "ticks": self._ticks,
            "tasks": [
                {
                    "name": t.name,
                    "interval_hours": round(t.interval_s / 3600, 2),
                    "enabled": t.enabled,
                    "status": t.last_status,
                    "runs": t.runs,
                    "failures": t.failures,
                    "last_run_epoch": t.last_run_wall,
                    "last_result": t.last_result,
                }
                for t in self._tasks.values()
            ],
        }


# --------------------------------------------------------------------------- #
# Default tasks.
# --------------------------------------------------------------------------- #
async def _parcel_harvest_task() -> dict:
    """Refresh property/parcel data for every wired state via the firehose.

    Bounded per-state batch keeps each cycle polite. Requires an ingest tenant;
    skips cleanly (no error) if unset so a misconfigured prod still boots.
    """
    tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant:
        logger.warning("parcel harvest skipped — ORACLE_INGEST_TENANT_ID unset.")
        return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}
    try:
        from harvesters.firehose import MultiStateFirehose, REGISTRY
    except Exception as exc:  # noqa: BLE001 — playwright/import env issues isolated
        logger.warning("parcel harvest unavailable: %s", exc)
        return {"skipped": f"firehose import failed: {exc}"}

    cap = int(os.getenv("ORACLE_HARVEST_MAX_PER_STATE", "5000")) or None
    firehose = MultiStateFirehose(tenant, agent_id="periodic-harvest")
    result = await firehose.run(max_records_per_state=cap, concurrency=2)
    totals = result.get("totals", {})
    return {"states": len(REGISTRY), "inserted": totals.get("inserted", 0),
            "errors": totals.get("errors", 0), "elapsed_s": totals.get("elapsed_s")}


async def _new_listings_task() -> dict:
    """Fast-moving MLS delta: pull listings modified since the last sync (RESO
    Web API) and upsert into oracle_mls_listings. Skips cleanly if no feed is
    configured. This is the hourly counterpart to the slow parcel harvest."""
    try:
        from data_integrations.listings_feed import RESOListingsFeed
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"listings_feed import failed: {exc}"}
    if not RESOListingsFeed.is_configured():
        return {"skipped": "no RESO feed configured (set ORACLE_RESO_URL/TOKEN/MLS_ID)"}
    return await RESOListingsFeed().sync_once()


async def _distress_scrape_task() -> dict:
    """Fast-moving ACTIVE SCRAPE: keyless Socrata code-violation feeds (NYC HPD,
    Chicago) → distress leads, idempotent via leads UNIQUE(tenant_id,parcel_id).
    Per-source rate limiting + circuit breakers live in harvesters/base.py, so a
    flaky/down portal backs off instead of flooding. Skips cleanly with no tenant.
    Uses NO API keys — safe to run on a tight (e.g. 30-min) cadence."""
    tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant:
        return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}
    cap = int(os.getenv("ORACLE_DISTRESS_MAX_PER_SOURCE", "500")) or None
    try:
        from harvesters.ny_hpd_violations import NYCHPDViolationsHarvester
        from harvesters.il_chicago_violations import ChicagoBuildingViolationsHarvester
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"distress harvester import failed: {exc}"}
    out: dict = {}
    for label, cls in (("nyc_hpd", NYCHPDViolationsHarvester),
                       ("chicago_violations", ChicagoBuildingViolationsHarvester)):
        try:
            res = await cls(tenant, agent_id="periodic-distress").harvest(
                max_records=cap, persist=True)
            out[label] = res if isinstance(res, dict) else {"result": str(res)}
        except Exception as exc:  # noqa: BLE001 — one source must not sink the rest
            out[label] = {"error": str(exc)[:160]}
    return out


async def _coverage_snapshot_task() -> dict:
    """Cheap every-tick freshness signal: recompute national coverage summary."""
    try:
        import sys
        from pathlib import Path
        backend = str(Path(__file__).resolve().parent.parent)
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from data_coverage import summary
        return summary()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:160]}


async def _retention_cleanup_task() -> dict:
    """Redact expired raw evidence/transcripts and evict stale cache rows.

    The database function is SECURITY DEFINER and independently verifies the
    platform-admin RLS context. Evidence hashes and audit facts remain intact.
    """
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    tenant_id = os.getenv(
        "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
    )
    ctx = TenantContext(
        agent_id="retention-janitor",
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )
    raw_days = max(1, min(3650, int(os.getenv("ORACLE_RAW_SOURCE_RETENTION_DAYS", "730"))))
    transcript_days = max(
        1,
        min(3650, int(os.getenv("ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS", "365"))),
    )
    async with tenant_tx(ctx) as conn:
        result = await conn.fetchval(
            "SELECT purge_expired_platform_data($1, $2)",
            raw_days,
            transcript_days,
        )
    if isinstance(result, str):
        result = json.loads(result)
    clean = dict(result or {})
    logger.info(
        "ORACLE_METRIC retention_cleanup source_payloads=%d transcripts=%d cache_rows=%d",
        int(clean.get("source_payloads_purged") or 0),
        int(clean.get("transcripts_purged") or 0),
        int(clean.get("cache_rows_deleted") or 0),
    )
    return clean


async def _source_health_task() -> dict:
    """Emit one metric marker per enabled source that is stale or open."""
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    tenant_id = os.getenv(
        "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
    )
    ctx = TenantContext(
        agent_id="source-health-monitor",
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT source_key,circuit_state,failure_count,
                   EXTRACT(EPOCH FROM (now()-last_succeeded_at)) AS age_seconds
              FROM harvest_sources
             WHERE enabled
               AND (
                   circuit_state='open'
                   OR last_succeeded_at IS NULL
                   OR now()-last_succeeded_at > make_interval(
                       secs => GREATEST(schedule_seconds*2, 21600)::double precision
                   )
               )
             ORDER BY source_key
            """
        )
    sources = []
    for row in rows:
        source_key = str(row["source_key"])
        sources.append(source_key)
        logger.warning(
            "ORACLE_METRIC stale_harvest_source source=%s circuit=%s failures=%d",
            source_key,
            str(row["circuit_state"]),
            int(row["failure_count"] or 0),
        )
    return {"stale_sources": len(sources), "source_keys": sources}


def build_default_scheduler() -> PeriodicScheduler:
    sched = PeriodicScheduler()
    municipal_enabled = os.getenv(
        "ORACLE_FEATURE_MUNICIPAL_HARVESTS", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    harvest_interval_h = float(os.getenv("ORACLE_HARVEST_INTERVAL_HOURS", "24"))
    sched.register(PeriodicTask(
        name="parcel_harvest",
        interval_s=harvest_interval_h * 3600,
        run=_parcel_harvest_task,
        # Heavy 49-state national pass. Default OFF so enabling the scheduler +
        # ingest tenant (for the fast distress scrape) doesn't kick off a full
        # parcel run; flip ORACLE_PARCEL_HARVEST_ENABLED=1 when you want it.
        enabled=(
            municipal_enabled
            and os.getenv("ORACLE_PARCEL_HARVEST_ENABLED", "0") == "1"
        ),
    ))
    # Fast-moving keyless distress scrape (NYC HPD + Chicago violations). Polite
    # to Socrata; default 30-min cadence = the "active web scrape" heartbeat.
    distress_interval_min = float(os.getenv("ORACLE_DISTRESS_INTERVAL_MIN", "30"))
    sched.register(PeriodicTask(
        name="distress_scrape",
        interval_s=distress_interval_min * 60,
        run=_distress_scrape_task,
        enabled=municipal_enabled,
    ))
    listings_interval_h = float(os.getenv("ORACLE_LISTINGS_INTERVAL_HOURS", "1"))
    sched.register(PeriodicTask(
        name="new_listings",
        interval_s=listings_interval_h * 3600,   # fast-moving — hourly by default
        run=_new_listings_task,
    ))
    sched.register(PeriodicTask(
        name="coverage_snapshot",
        interval_s=TICK_SECONDS,   # every heartbeat
        run=_coverage_snapshot_task,
    ))
    sched.register(PeriodicTask(
        name="platform_retention_cleanup",
        interval_s=24 * 3600,
        run=_retention_cleanup_task,
    ))
    sched.register(PeriodicTask(
        name="platform_source_health",
        interval_s=max(3600, TICK_SECONDS),
        run=_source_health_task,
    ))
    return sched


# Process-wide singleton + lifecycle hooks (mirrors voice_intel workers).
scheduler: PeriodicScheduler = build_default_scheduler()


async def start_periodic_scheduler() -> None:
    await scheduler.start()


async def stop_periodic_scheduler() -> None:
    await scheduler.stop()
