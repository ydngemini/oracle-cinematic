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
import logging
import os
import time
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
    """Heartbeat loop running due PeriodicTasks. One per process."""

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
        """Run one task, isolating its failure from the loop and siblings."""
        t0 = time.monotonic()
        try:
            logger.info("[periodic] running '%s'", task.name)
            result = await task.run()
            task.last_status = "ok"
            task.last_result = result if isinstance(result, dict) else {"result": str(result)}
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
        """Manually trigger a task immediately (admin / debugging)."""
        task = self._tasks.get(name)
        if not task:
            raise KeyError(f"No such task: {name}. Known: {list(self._tasks)}")
        await self._run_task(task)
        return task.last_result

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


def build_default_scheduler() -> PeriodicScheduler:
    sched = PeriodicScheduler()
    harvest_interval_h = float(os.getenv("ORACLE_HARVEST_INTERVAL_HOURS", "24"))
    sched.register(PeriodicTask(
        name="parcel_harvest",
        interval_s=harvest_interval_h * 3600,
        run=_parcel_harvest_task,
        # Heavy 49-state national pass. Default OFF so enabling the scheduler +
        # ingest tenant (for the fast distress scrape) doesn't kick off a full
        # parcel run; flip ORACLE_PARCEL_HARVEST_ENABLED=1 when you want it.
        enabled=os.getenv("ORACLE_PARCEL_HARVEST_ENABLED", "0") == "1",
    ))
    # Fast-moving keyless distress scrape (NYC HPD + Chicago violations). Polite
    # to Socrata; default 30-min cadence = the "active web scrape" heartbeat.
    distress_interval_min = float(os.getenv("ORACLE_DISTRESS_INTERVAL_MIN", "30"))
    sched.register(PeriodicTask(
        name="distress_scrape",
        interval_s=distress_interval_min * 60,
        run=_distress_scrape_task,
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
    return sched


# Process-wide singleton + lifecycle hooks (mirrors voice_intel workers).
scheduler: PeriodicScheduler = build_default_scheduler()


async def start_periodic_scheduler() -> None:
    await scheduler.start()


async def stop_periodic_scheduler() -> None:
    await scheduler.stop()
