"""One health model for every dependency (docs/dependency-resilience-map.md).

States (normalized, provider-neutral):
    HEALTHY      working
    DEGRADED     working with reduced function (fallback in use, backlog, …)
    UNAVAILABLE  not working
    RATE_LIMITED the provider is throttling us
    AUTH_FAILED  credentials rejected — an operator must act; not "down"
    STALE        data or heartbeat older than it should be
    RECOVERING   reconnecting after a failure
    UNKNOWN      not determinable right now (never reported as HEALTHY)
    NOT_CONFIGURED optional dependency that this deployment does not use

Every probe is bounded (a few seconds) and none raises: a health check that
throws during an outage tells an operator nothing. Details carry counts and
ages only — never provider error text, customer data or hostnames.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable

HEALTHY, DEGRADED, UNAVAILABLE = "HEALTHY", "DEGRADED", "UNAVAILABLE"
RATE_LIMITED, AUTH_FAILED, STALE = "RATE_LIMITED", "AUTH_FAILED", "STALE"
RECOVERING, UNKNOWN, NOT_CONFIGURED = "RECOVERING", "UNKNOWN", "NOT_CONFIGURED"

# Which states page someone (ops_alerts) — UNKNOWN does not: it usually means
# the database is down, which has its own alert.
ALERTING = {UNAVAILABLE, AUTH_FAILED, STALE, DEGRADED, RATE_LIMITED, RECOVERING}

# Customer-facing effect of each component, for the impact matrix and the UI.
ROLE = {
    "database": "core", "realtime_fanout": "core", "workers": "core", "scheduler": "core",
    "job_queue": "core", "valkey": "important", "mls": "important", "ai": "important",
    "outbound_side_effects": "important", "email_outbox": "important",
}


def _c(state: str, summary: str, **detail: Any) -> dict:
    return {"state": state, "summary": summary, "detail": detail}


async def _bounded(fn: Callable[[], Awaitable[dict]], seconds: float = 3.0) -> dict:
    try:
        return await asyncio.wait_for(fn(), timeout=seconds)
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        return _c(UNKNOWN, "health probe did not answer", reason=type(exc).__name__)


async def _database() -> dict:
    from db.connection import get_pool, pool_stats

    pool = get_pool()
    if pool is None:
        return _c(UNAVAILABLE, "database pool not initialised")
    try:
        async with pool.acquire(timeout=2) as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        return _c(UNAVAILABLE, "database is not answering", reason=type(exc).__name__)
    stats = pool_stats()
    if stats and stats.get("idle", 1) == 0 and stats.get("size", 0) >= stats.get("max_size", 1):
        return _c(DEGRADED, "connection pool saturated", **stats)
    return _c(HEALTHY, "database answering", **{k: stats.get(k) for k in ("size", "idle", "max_size")})


async def _realtime() -> dict:
    import ws_hub

    status = ws_hub.listener_status()
    summary = {"HEALTHY": "cross-replica updates flowing",
               "RECOVERING": "reconnecting cross-replica updates",
               "UNAVAILABLE": "cross-replica updates off"}.get(status["state"], "")
    return _c(status["state"], summary, reconnects=status["reconnects"], down_seconds=status["down_seconds"])


_valkey_client: Any = None


async def _valkey() -> dict:
    """Ping Valkey directly with a short-timeout client of our own: reading the
    rate limiter's client said nothing in a process that never opened one."""
    global _valkey_client
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return _c(NOT_CONFIGURED, "no Valkey configured; limits use the database")
    try:
        if _valkey_client is None:
            import redis.asyncio as redis

            _valkey_client = redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        await asyncio.wait_for(_valkey_client.ping(), timeout=1.5)
    except Exception as exc:  # noqa: BLE001
        return _c(DEGRADED, "Valkey unreachable: rate limits on the database fallback, calling unavailable",
                  reason=type(exc).__name__)
    return _c(HEALTHY, "Valkey answering")


async def _db_components() -> dict[str, dict]:
    """Everything read from Postgres in one bounded connection."""
    from db.connection import get_pool

    pool = get_pool()
    out: dict[str, dict] = {}
    if pool is None:
        return {k: _c(UNKNOWN, "database unavailable") for k in
                ("workers", "scheduler", "job_queue", "mls", "ai", "outbound_side_effects", "email_outbox")}
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    # Platform-wide counts need the platform login (RLS admits a platform
    # session only on that pool) — never customer rows, only aggregates.
    ctx = TenantContext(agent_id="component-health", role=Role.PLATFORM_ADMIN,
                        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"))
    async with tenant_tx(ctx) as conn:
        await conn.execute("SET LOCAL statement_timeout = '2500ms'")
        beats = await conn.fetch(
            "SELECT role, detail, EXTRACT(EPOCH FROM now()-last_seen_at)::int AS age "
            "FROM process_heartbeats WHERE last_seen_at > now() - interval '1 day'")
        queue = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE state='queued' AND scheduled_at <= now()) AS ready,
                   EXTRACT(EPOCH FROM now() - min(scheduled_at) FILTER (
                       WHERE state='queued' AND scheduled_at <= now()))::int AS oldest_ready_s,
                   count(*) FILTER (WHERE state='dead_letter' AND updated_at > now() - interval '1 hour') AS dead_1h
              FROM automation_jobs WHERE state IN ('queued','dead_letter')
            """)
        mls_rows = [dict(r) for r in await conn.fetch("SELECT * FROM mls_sync_status")]
        chat = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE status='failed') AS failed, count(*) AS total "
            "FROM ai_chat_messages WHERE role='assistant' AND created_at > now() - interval '15 minutes'")
        # Recovery is the latest finished turn succeeding, not the failures
        # ageing out of a 15-minute window.
        latest_turn = await conn.fetchval(
            "SELECT status FROM ai_chat_messages WHERE role='assistant' AND status IN ('completed','failed') "
            "AND created_at > now() - interval '15 minutes' ORDER BY updated_at DESC LIMIT 1")
        side = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE state='reconciliation_required') AS unresolved, "
            "count(*) FILTER (WHERE state='failed' AND updated_at > now() - interval '15 minutes') AS failed_15m "
            "FROM command_executions WHERE state IN ('reconciliation_required','failed')")
        mail = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE status='sending' AND error LIKE 'delivery_unknown%') AS unknown, "
            "count(*) FILTER (WHERE status='queued' AND updated_at < now() - interval '15 minutes') AS stuck "
            "FROM email_outbox WHERE status IN ('sending','queued')")
    workers = [b for b in beats if b["role"] in ("worker", "all")]
    from process_heartbeat import STALE_AFTER  # same bound as /health/workers (4 missed beats)

    live = [b for b in workers if b["age"] <= STALE_AFTER]
    out["workers"] = (_c(HEALTHY, f"{len(live)} background worker(s) alive", live=len(live)) if live
                      else _c(UNAVAILABLE, "no background worker alive — jobs, sends and syncs are not running"))
    sched = [b for b in beats if b["role"] == "scheduler"]
    if not sched:
        out["scheduler"] = _c(UNKNOWN, "no scheduler heartbeat recorded")
    else:
        newest = min(sched, key=lambda b: b["age"])
        detail = newest["detail"] if isinstance(newest["detail"], dict) else json.loads(newest["detail"] or "{}")
        limit = int(detail.get("tick_seconds", 3600)) * 2 + 120
        out["scheduler"] = (_c(HEALTHY, "scheduler ticking", age_s=newest["age"]) if newest["age"] <= limit
                            else _c(STALE, "scheduled work has stopped", age_s=newest["age"], limit_s=limit))
    ready, oldest, dead = int(queue["ready"] or 0), int(queue["oldest_ready_s"] or 0), int(queue["dead_1h"] or 0)
    if live and oldest > 600:
        out["job_queue"] = _c(DEGRADED, "background work is backing up", ready=ready, oldest_ready_s=oldest)
    elif dead >= 10:
        out["job_queue"] = _c(DEGRADED, "many jobs failed permanently in the last hour", dead_letter_1h=dead)
    else:
        out["job_queue"] = _c(HEALTHY, "queue moving", ready=ready, oldest_ready_s=oldest, dead_letter_1h=dead)
    out["mls"] = _mls(mls_rows)
    failed, total = int(chat["failed"] or 0), int(chat["total"] or 0)
    out["ai"] = (_c(UNAVAILABLE if failed == total else DEGRADED, "Neoh responses are failing",
                    failed_15m=failed, total_15m=total)
                 if failed >= 3 and failed * 2 > total and latest_turn != "completed"
                 else _c(HEALTHY, "Neoh responding", total_15m=total))
    unresolved, failed_side = int(side["unresolved"] or 0), int(side["failed_15m"] or 0)
    out["outbound_side_effects"] = (
        _c(DEGRADED, "some calls/texts/emails need reconciliation", unresolved=unresolved, failed_15m=failed_side)
        if unresolved or failed_side >= 5 else _c(HEALTHY, "outbound actions confirmed", failed_15m=failed_side))
    unknown, stuck = int(mail["unknown"] or 0), int(mail["stuck"] or 0)
    out["email_outbox"] = (_c(DEGRADED, "emails waiting or with unknown delivery", unknown=unknown, stuck=stuck)
                           if unknown or stuck else _c(HEALTHY, "email outbox draining"))
    return out


def _mls(rows: list[dict]) -> dict:
    import mls_health

    if not rows:
        return _c(NOT_CONFIGURED, "no MLS feed configured")
    states = {r["mls_id"]: mls_health.compute_health(r) for r in rows}
    worst = {"AUTH_ERROR": AUTH_FAILED, "RATE_LIMITED": RATE_LIMITED, "STALE": STALE,
             "DEGRADED": DEGRADED, "ERROR": UNAVAILABLE}
    for raw, mapped in worst.items():
        bad = [k for k, v in states.items() if v == raw]
        if bad:
            return _c(mapped, f"{len(bad)} MLS feed(s) {raw.lower().replace('_', ' ')}; listings shown as stale",
                      feeds=len(states), affected=len(bad))
    return _c(HEALTHY, "MLS feeds fresh", feeds=len(states))


_cache: dict[str, Any] = {"at": 0.0, "snap": None}
CACHE_SECONDS = 15.0


async def snapshot(*, fresh: bool = False) -> dict:
    """Cached for CACHE_SECONDS: every signed-in browser polls /api/status."""
    if not fresh and _cache["snap"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["snap"]
    snap = await _snapshot()
    _cache.update(at=time.monotonic(), snap=snap)
    return snap


async def _snapshot() -> dict:
    db = await _bounded(_database)
    realtime = await _bounded(_realtime, 1.0)
    valkey = await _bounded(_valkey, 2.0)
    if db["state"] == HEALTHY:
        try:
            rest = await asyncio.wait_for(_db_components(), timeout=4.0)
        except Exception as exc:  # noqa: BLE001
            rest = {k: _c(UNKNOWN, "health query did not answer", reason=type(exc).__name__)
                    for k in ("workers", "scheduler", "job_queue", "mls", "ai", "outbound_side_effects", "email_outbox")}
    else:
        rest = {k: _c(UNKNOWN, "database unavailable")
                for k in ("workers", "scheduler", "job_queue", "mls", "ai", "outbound_side_effects", "email_outbox")}
    components = {"database": db, "realtime_fanout": realtime, "valkey": valkey, **rest}
    for name, comp in components.items():
        comp["role"] = ROLE.get(name, "optional")
    core_bad = [n for n, c in components.items() if c["role"] == "core" and c["state"] in (UNAVAILABLE, STALE)]
    overall = UNAVAILABLE if components["database"]["state"] == UNAVAILABLE else (
        DEGRADED if core_bad or any(c["state"] not in (HEALTHY, NOT_CONFIGURED) for c in components.values())
        else HEALTHY)
    return {"state": overall, "checked_at": time.time(), "components": components}


# Product-level wording for users (never provider internals).
USER_MESSAGES = {
    "ai": "Neoh is having trouble answering right now. Your work is saved.",
    "mls": "Listing data may be out of date.",
    "outbound_side_effects": "Some calls or messages are still being confirmed.",
    "email_outbox": "Some emails are waiting to send.",
    "valkey": "Calling is temporarily unavailable.",
    "realtime_fanout": "Live updates may be delayed — refresh to see the latest.",
}


def user_banner(snap: dict) -> list[str]:
    return [USER_MESSAGES[n] for n, c in snap["components"].items()
            if n in USER_MESSAGES and c["state"] not in (HEALTHY, NOT_CONFIGURED, UNKNOWN)]
