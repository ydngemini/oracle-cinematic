"""A background-work process announces that it is alive, and which build it is.

Why this exists: the release smoke test could only see the API. A worker that
crashed on boot left every background job silently unrun — MLS syncs, mission
ticks, the usage drain — while the release reported healthy, because nothing a
healthy worker does is guaranteed to be visible inside a release window. Job
leases heartbeat only while a job runs, and the scheduler ticks hourly.

So each process that runs background work upserts one row into
`process_heartbeats` every INTERVAL seconds. `GET /health/workers` reads it,
and `smoke-test.sh` requires a fresh heartbeat carrying the release's own
git SHA — which proves the worker is alive AND that it rolled over to the new
image, rather than a previous-release worker still running jobs against a
schema the new API has already changed.

A failed beat is logged and retried, never raised: a heartbeat that can take
the worker down would turn a database blip into the very outage it reports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional

import config

log = logging.getLogger("oracle.process_heartbeat")

#: Seconds between beats. The freshness threshold readers apply is a multiple
#: of this, so one missed beat (a slow query, a pool hiccup) never reads as a
#: dead worker.
INTERVAL = int(os.environ.get("ORACLE_HEARTBEAT_SECONDS", "30"))

#: A heartbeat older than this is treated as a dead process. Four missed beats.
STALE_AFTER = INTERVAL * 4

#: One id per process lifetime. A restart is a new process and a new row, so
#: `started_at` genuinely means "this process started", which is what makes a
#: crash loop visible (many rows, each short-lived) rather than hidden behind one
#: row that keeps getting refreshed.
PROCESS_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

#: When this PROCESS started — recorded explicitly rather than left to the
#: column default. The default is the time of the first SUCCESSFUL write, so a
#: worker whose first beats failed (database not up yet, table not migrated)
#: would report a shorter uptime than it really had, and uptime is what makes a
#: crash loop visible. Found on the first local run: the table did not exist
#: for the first beat, and started_at silently became "when the migration ran".
STARTED_AT = datetime.now(timezone.utc)

_UPSERT = """
    INSERT INTO process_heartbeats (process_id, role, git_sha, hostname, started_at)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (process_id) DO UPDATE
       SET last_seen_at = now(), git_sha = EXCLUDED.git_sha
"""

_PRUNE = "DELETE FROM process_heartbeats WHERE last_seen_at < now() - interval '1 day'"

_task: Optional[asyncio.Task] = None


def git_sha() -> str:
    """The build this process is. Same source as GET /version, deliberately:
    the image's own ENV, never a value the platform injects at runtime."""
    return os.environ.get("ORACLE_GIT_SHA", "unknown")


async def beat_once() -> bool:
    """Write one heartbeat. True on success. Never raises."""
    from db.connection import get_pool

    pool = get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _UPSERT, PROCESS_ID, config.PROCESS_ROLE, git_sha(),
                socket.gethostname(), STARTED_AT,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — a heartbeat must never take its process down
        log.warning("heartbeat not written (%s); will retry in %ss", exc, INTERVAL)
        return False


async def _loop() -> None:
    beats = 0
    while True:
        await beat_once()
        beats += 1
        # Prune occasionally, not every beat: it is housekeeping, not liveness.
        if beats % 120 == 1:
            from db.connection import get_pool

            pool = get_pool()
            if pool is not None:
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(_PRUNE)
                except Exception as exc:  # noqa: BLE001
                    log.debug("heartbeat prune skipped: %s", exc)
        await asyncio.sleep(INTERVAL)


async def start_heartbeat() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="process-heartbeat")
        log.info("process heartbeat started: %s (role=%s, sha=%s)",
                 PROCESS_ID, config.PROCESS_ROLE, git_sha())


async def stop_heartbeat() -> None:
    """Stop beating and, on a CLEAN shutdown, remove this process's row.

    Without the delete, a worker that exited normally during a rollout kept
    looking alive until its row went stale — for up to STALE_AFTER seconds the
    smoke test saw two releases running and had to wait it out. Deleting on a
    clean exit means the only rows that linger are processes that did NOT shut
    down cleanly, which is precisely the signal worth lingering.
    """
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None

    from db.connection import get_pool

    pool = get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM process_heartbeats WHERE process_id = $1", PROCESS_ID)
    except Exception as exc:  # noqa: BLE001 — shutdown must not fail on this
        log.debug("heartbeat row not removed on shutdown: %s", exc)


READ_WORKERS_SQL = """
    SELECT role, git_sha,
           EXTRACT(EPOCH FROM (now() - last_seen_at))::int AS age_seconds,
           EXTRACT(EPOCH FROM (now() - started_at))::int  AS uptime_seconds
      FROM process_heartbeats
     WHERE role IN ('worker', 'all')
       AND last_seen_at > now() - interval '1 hour'
     ORDER BY last_seen_at DESC
"""


def summarize(rows: list[dict]) -> dict:
    """Turn heartbeat rows into the answer a release check needs.

    Pure, so it can be tested without a database. `healthy` means at least one
    background-work process beat within STALE_AFTER seconds.
    """
    live = [r for r in rows if r["age_seconds"] <= STALE_AFTER]
    return {
        "healthy": bool(live),
        "stale_after_seconds": STALE_AFTER,
        "live_workers": len(live),
        # Distinct SHAs among LIVE workers. More than one means a rollout is
        # mid-flight — or stuck — with two builds running jobs at once.
        "live_git_shas": sorted({r["git_sha"] for r in live}),
        "workers": [
            {
                "role": r["role"],
                "git_sha": r["git_sha"],
                "age_seconds": r["age_seconds"],
                "uptime_seconds": r["uptime_seconds"],
            }
            for r in rows
        ],
    }
