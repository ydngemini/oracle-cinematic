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
  * A bounded payload-normalization job upgrades legacy public lead rows to the
    provenance-first schema without fetching or fabricating any new facts.

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
from datetime import datetime, timedelta, timezone
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
            # A national cleanup or a slow public portal can legitimately run
            # longer than the durable-job lease. Keep the lease alive while the
            # task is executing so a second worker cannot duplicate it midway.
            lease_seconds = max(30, int(os.getenv("ORACLE_JOB_LEASE_SECONDS", "120")))
            heartbeat_seconds = max(10, min(60, lease_seconds // 3))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(heartbeat_seconds)
                    await reporter.progress(5, f"{task.name}: working")

            heartbeat_task = asyncio.create_task(
                heartbeat(), name=f"periodic-heartbeat-{task.name}"
            )
            work_task = asyncio.create_task(
                task.run(), name=f"periodic-work-{task.name}"
            )
            try:
                done, _pending = await asyncio.wait(
                    {work_task, heartbeat_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat_task in done:
                    heartbeat_error = heartbeat_task.exception()
                    if heartbeat_error is not None:
                        work_task.cancel()
                        try:
                            await work_task
                        except asyncio.CancelledError:
                            pass
                        raise heartbeat_error
                    raise RuntimeError(f"{task.name}: heartbeat ended unexpectedly")
                result = await work_task
            finally:
                if not work_task.done():
                    work_task.cancel()
                    try:
                        await work_task
                    except asyncio.CancelledError:
                        pass
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
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
            except asyncio.CancelledError:
                pass  # We cancelled it a line ago; this is the expected path.
            except Exception as exc:  # noqa: BLE001
                # Reached only when the loop had ALREADY died of its own
                # accord before stop() was called — awaiting the task re-raises
                # that original crash here. Catching it alongside
                # CancelledError erased it at shutdown, so the enrichment
                # scheduler could be dead for a whole process lifetime with
                # nothing ever saying why.
                logger.exception("PeriodicScheduler loop had already crashed: %s", exc)
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
    outcome = {
        "states": len(REGISTRY),
        "inserted": totals.get("inserted", 0),
        "errors": totals.get("errors", 0),
        "failed_states": totals.get("failed_states", []),
        "zero_result_states": totals.get("zero_result_states", []),
        "elapsed_s": totals.get("elapsed_s"),
    }
    if int(totals.get("errors") or 0):
        outcome["_terminal_state"] = "partial"
    return outcome


async def _source_health_probe_task() -> dict:
    """Run one polite sample fetch per jurisdiction to verify source reachability.

    This is intentionally separate from the full harvest cadence: every source
    receives at most one small, rate-limited read per probe interval. Probe
    reads never persist property rows and never consume a production cursor.
    """
    tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant:
        return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}
    from harvesters.firehose import MultiStateFirehose, REGISTRY

    result = await MultiStateFirehose(
        tenant,
        agent_id="source-health-probe",
        mode="probe",
    ).run(max_records_per_state=1, concurrency=4)
    totals = result.get("totals", {})
    outcome = {
        "probed_states": len(REGISTRY),
        "failed_states": totals.get("failed_states", []),
        "zero_result_states": totals.get("zero_result_states", []),
        "errors": totals.get("errors", 0),
    }
    if int(totals.get("errors") or 0):
        outcome["_terminal_state"] = "partial"
    return outcome


async def _property_coordinate_backfill_task() -> dict:
    """Resolve coordinates for public records that have an address and no point.

    Sibling of the characteristics backfill below, and constrained the same way:
    it writes only to the shared public catalog and never invents a fact. The
    difference is why it must be periodic rather than a one-off script.

    Only 4.3% of 8.59M records carried a coordinate, so the map had nothing to
    plot and the radius comps search — which migration 0076 built an index for —
    could see 4% of the corpus. The Census batch geocoder is keyless and free,
    and it is also intermittent: it returned 89.9% on Delaware, refused every
    request minutes later, then recovered. A bulk run therefore stops partway
    through by design rather than walking its cursor over rows it never asked
    about. Running on a schedule is what turns "stops on an outage" from
    something an operator has to notice into something that resolves itself.

    Each pass is deliberately small. The point is to converge over days without
    ever being the reason a Census endpoint is unhappy.
    """
    batches = max(1, min(20, int(os.getenv("ORACLE_GEOCODE_BATCHES_PER_RUN", "4"))))
    size = max(100, min(10_000, int(os.getenv("ORACLE_GEOCODE_BATCH_SIZE", "2000"))))
    try:
        from backfill_property_coordinates import run as geocode_run
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"backfill import failed: {exc}"}

    code = await geocode_run(
        state=os.getenv("ORACLE_GEOCODE_STATE") or None,
        batch_size=size,
        dry_run=False,
        limit_batches=batches,
        # The scheduler is the retry. A preflight failure here would just be a
        # slower version of the outage stop that run() already handles, and it
        # would cost an extra request to a service that is already struggling.
        skip_preflight=True,
    )
    outcome = {"exit_code": code, "batches": batches, "batch_size": size}
    if code != 0:
        # Not an error worth alerting on: an outage or a fully-geocoded corpus
        # both land here, and the next pass settles which.
        outcome["_terminal_state"] = "partial"
    return outcome


async def _property_characteristics_backfill_task() -> dict:
    """Revisit public sources to populate source-published property facts.

    This has an independent cursor namespace from the ordinary parcel harvest
    and writes only to the shared public catalog. It cannot create private CRM
    leads and it never fills a missing fact with a model-generated estimate.
    """
    tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant:
        return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}
    try:
        from harvesters.firehose import MultiStateFirehose, REGISTRY
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"firehose import failed: {exc}"}

    cap = max(
        100,
        min(25_000, int(os.getenv("ORACLE_PROPERTY_FACT_BACKFILL_MAX_PER_STATE", "1000"))),
    )
    concurrency = max(
        1,
        min(8, int(os.getenv("ORACLE_PROPERTY_FACT_BACKFILL_CONCURRENCY", "2"))),
    )
    result = await MultiStateFirehose(
        tenant,
        agent_id="property-characteristics-backfill",
        mode="catalog_backfill",
    ).run(max_records_per_state=cap, concurrency=concurrency)
    totals = result.get("totals", {})
    outcome = {
        "jurisdictions": len(REGISTRY),
        "catalog_rows_refreshed": int(totals.get("inserted") or 0),
        "errors": int(totals.get("errors") or 0),
        "failed_states": totals.get("failed_states", []),
        "zero_result_states": totals.get("zero_result_states", []),
        "checkpointed_states": int(totals.get("checkpointed_states") or 0),
        "elapsed_s": totals.get("elapsed_s"),
        "source_backed_only": True,
    }
    if outcome["errors"]:
        outcome["_terminal_state"] = "partial"
    logger.info(
        "ORACLE_METRIC property_characteristics_backfill refreshed=%d errors=%d",
        outcome["catalog_rows_refreshed"],
        outcome["errors"],
    )
    return outcome


async def _lead_payload_normalization_task() -> dict:
    """Clean legacy national-harvest payloads without rescraping a source.

    This is a database-only, idempotent migration through the durable worker.
    It touches only records tagged by the public-property pipeline, preserving
    manual CRM leads and any calculated underwriting fields.
    """
    tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant:
        return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}
    from harvesters.base import normalize_public_leads

    batch_size = int(os.getenv("ORACLE_LEAD_NORMALIZE_BATCH_SIZE", "500"))
    max_rows = int(os.getenv("ORACLE_LEAD_NORMALIZE_MAX_ROWS", "10000"))
    result = await normalize_public_leads(
        tenant,
        batch_size=batch_size,
        max_rows=max_rows,
    )
    logger.info(
        "ORACLE_METRIC lead_payload_normalization examined=%d normalized=%d fallback=%d",
        int(result.get("examined") or 0),
        int(result.get("normalized") or 0),
        int(result.get("normalized_from_fallback") or 0),
    )
    # Drain a large historical backfill in serialized, bounded jobs rather than
    # leaving the next 10k records for tomorrow's cadence. The short delay lets
    # this job commit and release its lease before another worker can claim the
    # follow-up batch.
    catchup_enabled = os.getenv("ORACLE_LEAD_NORMALIZE_CATCHUP_ENABLED", "1") == "1"
    if catchup_enabled and int(result.get("normalized") or 0) >= max_rows:
        from automation_jobs import enqueue_job
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id="lead-payload-normalizer",
            tenant_id=tenant,
            role=Role.PLATFORM_ADMIN,
        )
        followup, created = await enqueue_job(
            ctx,
            job_type="periodic:lead_payload_normalization",
            payload={"task": "lead_payload_normalization", "catchup": True},
            idempotency_key=f"lead-payload-normalization-catchup:{time.time_ns()}",
            created_by="lead-payload-normalizer",
            priority=40,
            scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        result["catchup_queued"] = created
        result["catchup_job_id"] = followup["id"]
    return result


async def _new_listings_task() -> dict:
    """Fast-moving MLS delta across every authorized RESO board.

    Each board owns an independent durable cursor and failure boundary. One
    unavailable MLS therefore degrades the roll-up instead of blocking all
    listing updates.
    """
    try:
        from data_integrations.listings_feed import RESOListingsAggregator
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"listings_feed import failed: {exc}"}
    if not RESOListingsAggregator.is_configured():
        return {
            "skipped": (
                "no RESO feed configured "
                "(set ORACLE_RESO_FEEDS_JSON or ORACLE_RESO_URL/TOKEN/MLS_ID)"
            )
        }
    return await RESOListingsAggregator().sync_once()


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


async def _market_research_task() -> dict:
    """Refresh redistributable aggregate market metrics for all jurisdictions.

    This is deliberately separate from the property/listing pipelines: Zillow
    Research, Redfin Data Center, HUD, and FHFA observations describe markets,
    not individual homes.
    """
    try:
        from market_research_agent import run_market_research_sync
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"market research agent import failed: {exc}"}
    result = await run_market_research_sync()

    # Project what just landed into state_market_stats, which is the table the
    # compliance/market routes and the get_market_trends tool actually read.
    # Without this the sync refreshed public_market_metrics while every caller
    # continued to be served migration 0025's 2024-10-01 seed.
    try:
        import os as _os

        from db.connection import tenant_tx
        from state_market_projection import project_state_market_stats
        from tenancy import Role, TenantContext

        # Same platform identity the market sync writes under — both tables are
        # platform-wide reference data, not tenant rows.
        ctx = TenantContext(
            agent_id="state-market-projection",
            tenant_id=_os.getenv("ORACLE_INGEST_TENANT_ID")
            or _os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(ctx) as conn:
            result["state_market_stats"] = await project_state_market_stats(conn)
    except Exception as exc:  # noqa: BLE001 — the sync's own result is still worth returning
        result["state_market_stats"] = {"error": str(exc)[:200]}
    return result


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
        oauth_states_deleted = await conn.fetchval("SELECT purge_expired_oauth_states()")
    if isinstance(result, str):
        result = json.loads(result)
    clean = dict(result or {})
    clean["oauth_states_deleted"] = int(oauth_states_deleted or 0)
    logger.info(
        "ORACLE_METRIC retention_cleanup source_payloads=%d transcripts=%d cache_rows=%d oauth_states=%d",
        int(clean.get("source_payloads_purged") or 0),
        int(clean.get("transcripts_purged") or 0),
        int(clean.get("cache_rows_deleted") or 0),
        clean["oauth_states_deleted"],
    )
    return clean


async def _source_health_task() -> dict:
    """Persist and emit one bounded health marker per enabled source."""
    from db.connection import tenant_tx
    from harvest_health import classify_health, safe_health_detail
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
            SELECT id,source_key,circuit_state,failure_count,schedule_seconds,
                   last_succeeded_at,last_error
              FROM harvest_sources
             WHERE enabled
             ORDER BY source_key
            """
        )
    sources: list[dict[str, str]] = []
    updates = []
    for row in rows:
        source_key = str(row["source_key"])
        status = classify_health(
            last_succeeded_at=row["last_succeeded_at"],
            schedule_seconds=row["schedule_seconds"],
            circuit_state=row["circuit_state"],
            failure_count=row["failure_count"],
        )
        detail = safe_health_detail(row["last_error"]) if status in {"degraded", "failed"} else None
        updates.append((status, detail, row["id"]))
        if status != "fresh":
            sources.append({"source_key": source_key, "status": status})
            logger.warning(
                "ORACLE_METRIC harvest_source_health source=%s status=%s circuit=%s failures=%d",
                source_key,
                status,
                str(row["circuit_state"]),
                int(row["failure_count"] or 0),
            )
    if updates:
        async with tenant_tx(ctx) as conn:
            await conn.executemany(
                """
                UPDATE harvest_sources
                   SET health_status=$1,health_detail=$2,last_health_checked_at=now(),updated_at=now()
                 WHERE id=$3::uuid
                """,
                updates,
            )
    return {"non_fresh_sources": len(sources), "sources": sources}


async def _client_ai_catchup_task() -> dict:
    """Reconcile clients missed by event hooks or changed outside the CRM API."""
    from client_ai_automation import enqueue_stale_clients

    return await enqueue_stale_clients(
        limit=max(1, min(5000, int(os.getenv("ORACLE_CLIENT_AI_SWEEP_LIMIT", "500"))))
    )


async def _mission_tick_task() -> dict:
    """Advance every running mission by one step.

    Default OFF. Missions pursue an outcome across many contacts unattended,
    so the scheduler does not start doing that because a deploy happened —
    the same posture as the parcel harvest and speed-to-lead.
    """
    from missions.executor import sweep_all_tenants

    return await sweep_all_tenants()


async def _outcome_attribution_task() -> dict:
    """Bind recorded outcomes to the decisions that earned them.

    ``outcome_memory.record_outcome`` writes facts at the moment they happen;
    this is the other clock, where a reply from Tuesday finds the text from
    last week. Rows that match nothing are marked examined too — that is the
    base rate, and skipping them would leave every rate without a denominator.
    """
    from outcome_memory import sweep_all_tenants

    return await sweep_all_tenants(
        per_tenant_limit=max(
            1, min(1000, int(os.getenv("ORACLE_OUTCOME_ATTRIBUTION_BATCH", "200")))
        ),
    )


async def _usage_meter_drain_task() -> dict:
    """Push locally-recorded usage to Stripe's meter.

    ``billing_usage`` records unconditionally and reports optionally; the report
    half needs something on a timer or configured metering silently never bills.
    A no-op (and cheap) when ``STRIPE_METERED_PRICE_ID`` is unset.
    """
    from billing_usage import drain_all_tenants

    return await drain_all_tenants(
        per_tenant_limit=max(
            1, min(1000, int(os.getenv("ORACLE_USAGE_DRAIN_BATCH", "200")))
        )
    )


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
    probe_interval_h = max(6.0, float(os.getenv("ORACLE_SOURCE_HEALTH_PROBE_INTERVAL_HOURS", "24")))
    sched.register(PeriodicTask(
        name="source_health_probe",
        interval_s=probe_interval_h * 3600,
        run=_source_health_probe_task,
        enabled=(
            municipal_enabled
            and os.getenv("ORACLE_SOURCE_HEALTH_PROBES_ENABLED", "1") == "1"
        ),
    ))
    fact_interval_h = max(
        6.0,
        float(os.getenv("ORACLE_PROPERTY_FACT_BACKFILL_INTERVAL_HOURS", "6")),
    )
    # Hourly by default: small passes that converge over days, and that pick
    # themselves up after a Census outage without anyone watching.
    sched.register(PeriodicTask(
        name="property_coordinate_backfill",
        interval_s=float(os.getenv("ORACLE_GEOCODE_INTERVAL_H", "1")) * 3600,
        run=_property_coordinate_backfill_task,
        enabled=os.getenv("ORACLE_GEOCODE_BACKFILL_ENABLED", "1") == "1",
    ))
    sched.register(PeriodicTask(
        name="property_characteristics_backfill",
        interval_s=fact_interval_h * 3600,
        run=_property_characteristics_backfill_task,
        enabled=(
            municipal_enabled
            and os.getenv("ORACLE_PROPERTY_FACT_BACKFILL_ENABLED", "1") == "1"
        ),
    ))
    sched.register(PeriodicTask(
        name="lead_payload_normalization",
        interval_s=24 * 3600,
        run=_lead_payload_normalization_task,
        enabled=municipal_enabled,
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
    market_interval_h = max(
        6.0, float(os.getenv("ORACLE_MARKET_RESEARCH_INTERVAL_HOURS", "24"))
    )
    sched.register(PeriodicTask(
        name="market_research",
        interval_s=market_interval_h * 3600,
        run=_market_research_task,
        enabled=os.getenv("ORACLE_MARKET_RESEARCH_ENABLED", "1") == "1",
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
    sched.register(PeriodicTask(
        name="client_ai_catchup",
        interval_s=max(
            3600,
            float(os.getenv("ORACLE_CLIENT_AI_SWEEP_INTERVAL_HOURS", "24")) * 3600,
        ),
        run=_client_ai_catchup_task,
        enabled=os.getenv("ORACLE_FEATURE_CLIENT_AI_AUTOMATION", "true").strip().lower()
        in {"1", "true", "yes", "on"},
    ))
    # Hourly by default: a meter event carries its own timestamp, so cadence
    # affects only how fresh Stripe's view is, not what the customer is billed.
    # Left registered even when metering is unconfigured — the task short-circuits
    # on that, and the scheduler status page is more useful showing it than hiding it.
    sched.register(PeriodicTask(
        name="usage_meter_drain",
        interval_s=max(
            300.0,
            float(os.getenv("ORACLE_USAGE_DRAIN_INTERVAL_MIN", "60")) * 60,
        ),
        run=_usage_meter_drain_task,
        enabled=os.getenv("ORACLE_USAGE_DRAIN_ENABLED", "1") == "1",
    ))
    # Fifteen minutes by default. Attribution is deferred rather than inline
    # because the reply→decision window is fourteen days and a synchronous
    # join on every inbound message is the wrong cost; a quarter-hour of lag on
    # a fact that took days to arrive is invisible. The sweep is idempotent —
    # every write is guarded by IS NULL — so overlap between replicas is safe.
    sched.register(PeriodicTask(
        name="outcome_attribution",
        interval_s=max(
            60.0,
            float(os.getenv("ORACLE_OUTCOME_ATTRIBUTION_INTERVAL_MIN", "15")) * 60,
        ),
        run=_outcome_attribution_task,
        enabled=os.getenv("ORACLE_OUTCOME_ATTRIBUTION_ENABLED", "1") == "1",
    ))
    # Ten minutes, and OFF unless explicitly enabled. Two independent switches
    # have to be thrown before a mission acts: this one, and Feature.MISSIONS,
    # which the executor checks on every tick. That redundancy is deliberate —
    # the credential check is NOT a third switch, because a machine with no
    # credential rows can still have Twilio in its environment.
    sched.register(PeriodicTask(
        name="mission_tick",
        interval_s=max(
            60.0, float(os.getenv("ORACLE_MISSION_TICK_INTERVAL_MIN", "10")) * 60,
        ),
        run=_mission_tick_task,
        enabled=os.getenv("ORACLE_MISSIONS_ENABLED", "0") == "1",
    ))
    return sched


# Process-wide singleton + lifecycle hooks (mirrors voice_intel workers).
scheduler: PeriodicScheduler = build_default_scheduler()


async def start_periodic_scheduler() -> None:
    await scheduler.start()


async def stop_periodic_scheduler() -> None:
    await scheduler.stop()
