"""Operational alerting: notice when a dependency breaks, tell a person, never
make things worse.

Before 2026-10-02 there was none — a dead worker, a stuck scheduler or a
revoked MLS token was found when a customer complained. Every API replica runs
this evaluator once a minute; a PostgreSQL advisory lock elects one of them, so
one incident is one alert, not one per replica. If the database itself is down
(no lock to take), every replica evaluates on its own and rate-limits its
email in memory, because "the database is down" is the alert that matters most.

Delivery is best-effort and never on a request path:
  * always: a structured log line on `oracle.ops.alert` (the log pipeline)
  * when ORACLE_ALERT_EMAIL is set: one email per incident, through the same
    SMTP relay as everything else, in a thread with a timeout
  * ops_alerts table: open/last_seen/resolved/notified — MTTD is measured here
A failure to deliver is recorded (notify_error) and logged; it never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Optional

log = logging.getLogger("oracle.ops.alert")

EVALUATE_SECONDS = float(os.getenv("ORACLE_ALERT_EVALUATE_SECONDS", "60"))
# A component must be bad on two consecutive evaluations before anyone is
# paged: a single slow probe is not an incident.
CONFIRMATIONS = 2
_LOCK_KEY = 0x4F505341_4C525453  # "OPSALRTS"

_task: Optional[asyncio.Task] = None
_pending: dict[str, int] = {}
_offline_sent_at: dict[str, float] = {}


async def evaluate_once() -> dict[str, Any]:
    import component_health
    from db.connection import get_pool

    snap = await component_health.snapshot(fresh=True)
    bad = {n: c for n, c in snap["components"].items() if c["state"] in component_health.ALERTING}
    for name in list(_pending):
        if name not in bad:
            _pending.pop(name, None)
    confirmed = {}
    for name, comp in bad.items():
        _pending[name] = _pending.get(name, 0) + 1
        if _pending[name] >= CONFIRMATIONS:
            confirmed[name] = comp

    pool = get_pool()
    if snap["components"]["database"]["state"] != component_health.HEALTHY or pool is None:
        await _offline_alert(snap["components"]["database"])
        return {"mode": "offline", "confirmed": list(confirmed)}
    try:
        async with pool.acquire(timeout=3) as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK_KEY):
                return {"mode": "standby"}
            try:
                return await _record(conn, confirmed, bad, snap["components"])
            finally:
                await conn.fetchval("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    except Exception as exc:  # noqa: BLE001 - never sink the evaluator
        log.warning("alert evaluation could not record: %s", type(exc).__name__)
        return {"mode": "error"}


async def _record(conn, confirmed: dict, bad: dict, healthy_now: dict) -> dict:
    opened, resolved = [], []
    if bad:
        await conn.execute("UPDATE ops_alerts SET last_seen_at=now() WHERE resolved_at IS NULL "
                           "AND component = ANY($1::text[])", list(bad))
    for name, comp in confirmed.items():
        row = await conn.fetchrow(
            """
            INSERT INTO ops_alerts (component, state, summary) VALUES ($1,$2,$3)
            ON CONFLICT (component) WHERE resolved_at IS NULL
            DO UPDATE SET last_seen_at=now(), state=EXCLUDED.state, summary=EXCLUDED.summary
            RETURNING id, notified_at, (xmax = 0) AS inserted
            """, name, comp["state"], comp["summary"][:300])
        if row["notified_at"] is None:
            error = await _notify(f"[Neoh] {name} {comp['state']}", _body(name, comp))
            await conn.execute("UPDATE ops_alerts SET notified_at=CASE WHEN $2::text IS NULL THEN now() END, "
                               "notify_error=$2 WHERE id=$1", row["id"], error)
            opened.append(name)
    # Hysteresis: an incident resolves only when the component is positively
    # healthy now AND no evaluation (on any replica) has seen it bad for 2.5
    # periods. "Not currently in the bad set" was not enough — a probe that
    # returned UNKNOWN, or a replica mid-restart, closed incidents that were
    # still open, and a stale feed paged every minute (drill 2026-10-02).
    open_rows = await conn.fetch(
        "SELECT id, component FROM ops_alerts WHERE resolved_at IS NULL "
        "AND last_seen_at < now() - make_interval(secs => $1)", EVALUATE_SECONDS * 2.5)
    for row in open_rows:
        state = (healthy_now.get(row["component"]) or {}).get("state")
        if state in ("HEALTHY", "NOT_CONFIGURED"):
            await conn.execute("UPDATE ops_alerts SET resolved_at=now() WHERE id=$1", row["id"])
            log.info(json.dumps({"alert_resolved": row["component"], "observed": state}))
            await _notify(f"[Neoh] {row['component']} recovered", f"{row['component']} is healthy again.")
            resolved.append(row["component"])
    return {"mode": "leader", "opened": opened, "resolved": resolved}


async def _offline_alert(db_component: dict) -> None:
    """Database down: no table, no lock. One email per replica per 15 min."""
    log.error(json.dumps({"alert": "database", "state": db_component["state"], "summary": db_component["summary"]}))
    if time.time() - _offline_sent_at.get("database", 0) < 900:
        return
    _offline_sent_at["database"] = time.time()
    await _notify("[Neoh] database UNAVAILABLE",
                  "The database is not answering. Customers see 'temporarily unavailable'; no data is lost "
                  "by this alone. See docs/runbooks/database-down.md.")


def _body(name: str, comp: dict) -> str:
    detail = ", ".join(f"{k}={v}" for k, v in (comp.get("detail") or {}).items())
    return (f"Component: {name}\nState: {comp['state']}\nWhat it means: {comp['summary']}\n"
            f"Detail: {detail or '-'}\n\nRunbook: docs/provider-failure-matrix.md")


async def _notify(subject: str, body: str) -> Optional[str]:
    """Log always; email when configured. Returns an error string or None."""
    log.error(json.dumps({"alert": subject, "body": body[:500]}))
    to = os.getenv("ORACLE_ALERT_EMAIL", "").strip()
    if not to:
        return None
    try:
        import smtp_mailer

        await asyncio.wait_for(asyncio.to_thread(
            smtp_mailer.send, recipient=to, subject=subject, text=body), timeout=30)
        return None
    except Exception as exc:  # noqa: BLE001 - an alert that cannot be sent must not crash anything
        log.warning("alert email not delivered: %s", type(exc).__name__)
        return type(exc).__name__


async def _loop() -> None:
    await asyncio.sleep(random.uniform(5, 20))  # replicas do not evaluate in lockstep
    while True:
        try:
            await evaluate_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("alert evaluator iteration failed", exc_info=True)
        await asyncio.sleep(EVALUATE_SECONDS * random.uniform(0.9, 1.1))


def start() -> None:
    global _task
    if _task is None and os.getenv("ORACLE_ALERTS_ENABLED", "1") != "0":
        _task = asyncio.create_task(_loop(), name="ops-alert-evaluator")


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
