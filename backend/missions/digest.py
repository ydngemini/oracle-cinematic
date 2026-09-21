"""Mission activity digest — a periodic email report on what the agent did.

Missions run unattended (shadow, or — once a channel is consented via
``auto_channels`` — autopilot) between the operator's own logins, so there is
no other way to notice a mission stalled, got blocked by compliance, or is
quietly working leads well. This is that notice: one email, on a schedule,
summarizing every tenant's mission activity since the last one.

This is an OPERATOR report, not client outreach. It never goes through
``outreach_compliance.guard_outreach`` (there is no client on the other end
of this email) and it carries only counts and mission-level facts — no lead
name, phone number, or address — so it cannot become a second, unaudited
channel for client data to leave the system through.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from tenancy import Role, TenantContext

logger = logging.getLogger("oracle.missions.digest")


def enabled() -> bool:
    return os.getenv("ORACLE_MISSION_DIGEST_ENABLED", "0") == "1"


def _recipient() -> Optional[str]:
    value = os.environ.get("ORACLE_MISSION_DIGEST_EMAIL", "").strip()
    return value or None


async def _tenant_report(ctx: TenantContext, *, since: datetime) -> dict[str, Any]:
    """One tenant's mission activity since ``since``. Runs in that tenant's own
    context — never the platform admin's — so RLS is never asked to do the
    scoping a business predicate should be doing instead."""
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        missions = await conn.fetch(
            """
            SELECT id, objective_kind, objective_text, status, mode,
                   allowed_channels, auto_channels, target_count, launched_at
              FROM missions
             WHERE status IN ('shadow', 'active', 'paused')
             ORDER BY updated_at DESC
            """,
        )
        if not missions:
            return {"missions": [], "actions": {}, "blocked": {}, "events": []}

        mission_ids = [row["id"] for row in missions]
        action_rows = await conn.fetch(
            """
            SELECT mission_id, state, count(*)::int AS n
              FROM mission_actions
             WHERE mission_id = ANY($1::uuid[]) AND created_at >= $2
             GROUP BY mission_id, state
            """,
            mission_ids,
            since,
        )
        blocked_rows = await conn.fetch(
            """
            SELECT mission_id, blocked_reason, count(*)::int AS n
              FROM mission_actions
             WHERE mission_id = ANY($1::uuid[]) AND created_at >= $2
               AND state = 'blocked' AND blocked_reason IS NOT NULL
             GROUP BY mission_id, blocked_reason
             ORDER BY n DESC
             LIMIT 10
            """,
            mission_ids,
            since,
        )
        events = await conn.fetch(
            """
            SELECT mission_id, kind, occurred_at
              FROM mission_events
             WHERE mission_id = ANY($1::uuid[]) AND occurred_at >= $2
               AND kind IN ('launched', 'paused', 'resumed', 'budget_exhausted',
                            'completed', 'failed', 'cancelled', 'plan_failed')
             ORDER BY occurred_at DESC
             LIMIT 20
            """,
            mission_ids,
            since,
        )

    actions_by_mission: dict[str, dict[str, int]] = {}
    for row in action_rows:
        actions_by_mission.setdefault(str(row["mission_id"]), {})[row["state"]] = row["n"]
    blocked_by_mission: dict[str, list[dict[str, Any]]] = {}
    for row in blocked_rows:
        blocked_by_mission.setdefault(str(row["mission_id"]), []).append(
            {"reason": row["blocked_reason"], "count": row["n"]}
        )

    return {
        "missions": [dict(row) for row in missions],
        "actions": actions_by_mission,
        "blocked": blocked_by_mission,
        "events": [dict(row) for row in events],
    }


def _format_report(
    tenant_reports: list[tuple[str, dict[str, Any]]], *, since: datetime, until: datetime
) -> tuple[str, str]:
    """Build (text, html) for the window [since, until)."""
    window = f"{since.strftime('%Y-%m-%d %H:%M UTC')} – {until.strftime('%Y-%m-%d %H:%M UTC')}"
    lines = [f"Neoh AI agent activity — {window}", ""]
    html_parts = [f"<h2>Neoh AI agent activity</h2><p>{window}</p>"]

    any_mission = False
    for tenant_id, report in tenant_reports:
        missions = report["missions"]
        if not missions:
            continue
        any_mission = True
        lines.append(f"Tenant {tenant_id}:")
        html_parts.append(f"<h3>Tenant {tenant_id}</h3>")
        for mission in missions:
            mid = str(mission["id"])
            counts = report["actions"].get(mid, {})
            total = sum(counts.values())
            summary = ", ".join(f"{state}: {n}" for state, n in sorted(counts.items())) or "no activity"
            auto = ", ".join(mission["auto_channels"] or []) or "none — every send awaits your approval"
            lines.append(
                f"  - [{mission['status']}/{mission['mode']}] {mission['objective_kind']}"
                f" — {mission['objective_text'][:80]}"
            )
            lines.append(f"      {total} actions this window: {summary}")
            lines.append(f"      autopilot channels: {auto}")
            html_parts.append(
                f"<p><strong>[{mission['status']}/{mission['mode']}] {mission['objective_kind']}</strong>"
                f" — {mission['objective_text']}<br>"
                f"{total} actions this window: {summary}<br>"
                f"autopilot channels: {auto}</p>"
            )
            for blocked in report["blocked"].get(mid, []):
                lines.append(f"      blocked ×{blocked['count']}: {blocked['reason']}")
                html_parts.append(
                    f"<p style='color:#a33'>&nbsp;&nbsp;blocked ×{blocked['count']}: {blocked['reason']}</p>"
                )
        for event in report["events"]:
            lines.append(f"    event: {event['kind']} at {event['occurred_at'].isoformat()}")
        lines.append("")

    if not any_mission:
        lines.append("No missions are running.")
        html_parts.append("<p>No missions are running.</p>")

    return "\n".join(lines), "".join(html_parts)


async def send_digest() -> dict[str, Any]:
    """Scheduler entry point: one email covering every tenant's mission activity
    since the last digest window. Registered as a periodic task in
    data_integrations/periodic.py, gated OFF by ORACLE_MISSION_DIGEST_ENABLED
    and requiring ORACLE_MISSION_DIGEST_EMAIL — both unset by default, the same
    posture as every other background sweep in this codebase."""
    import asyncio

    from db.connection import tenant_tx
    from missions import executor as missions_executor

    if not enabled():
        return {"skipped": "digest is not enabled on this deployment"}
    if not missions_executor.enabled():
        return {"skipped": "missions are not enabled on this deployment"}
    recipient = _recipient()
    if not recipient:
        return {"skipped": "ORACLE_MISSION_DIGEST_EMAIL is not set"}

    interval_min = max(60, int(os.getenv("ORACLE_MISSION_DIGEST_INTERVAL_MIN", "1440")))
    until = datetime.now(timezone.utc)
    since = until - timedelta(minutes=interval_min)

    platform_ctx = TenantContext(
        agent_id="mission-digest",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        # Business scope, deliberately cross-tenant: finding who has a mission
        # worth reporting on. Every read below runs in that tenant's own
        # context, the same split sweep_all_tenants uses elsewhere.
        tenant_rows = await conn.fetch(
            "SELECT DISTINCT tenant_id FROM missions WHERE status IN ('shadow','active','paused')",
        )

    reports: list[tuple[str, dict[str, Any]]] = []
    for row in tenant_rows:
        tenant_ctx = TenantContext(
            agent_id="mission-digest", tenant_id=str(row["tenant_id"]), role=Role.BROKER_OWNER,
        )
        try:
            report = await _tenant_report(tenant_ctx, since=since)
        except Exception:  # noqa: BLE001 — one tenant must not blank the digest
            logger.exception("mission digest failed for tenant %s", row["tenant_id"])
            continue
        reports.append((str(row["tenant_id"]), report))

    text, html = _format_report(reports, since=since, until=until)

    import smtp_mailer

    try:
        message_id = await asyncio.to_thread(
            smtp_mailer.send,
            recipient=recipient,
            subject="Neoh AI agent — activity digest",
            text=text,
            html=html,
        )
    except smtp_mailer.SmtpConfigurationError as exc:
        logger.warning("mission digest email not sent (SMTP not configured): %s", exc)
        return {"sent": False, "reason": str(exc), "tenants": len(reports)}
    except smtp_mailer.SmtpSendError as exc:
        logger.warning("mission digest email failed to send: %s", exc)
        return {"sent": False, "reason": str(exc), "tenants": len(reports)}

    return {"sent": True, "message_id": message_id, "tenants": len(reports)}
