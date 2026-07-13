"""
Platform Admin Ops — the all-seeing surface behind the OPS tab.

Every route is double-gated: require_context decodes the Bearer JWT, then
require_platform_admin rejects anything that isn't the platform_admin role.
Queries still run inside tenant_tx(ctx) — the platform admin's RLS escape
(app_is_platform_admin(), migration 0001) is what makes the cross-tenant
reads legal at the Postgres level; nothing here bypasses the policy layer.

Surfaces:
  * /overview — per-tenant fleet counts + live sessions + WS socket groups.
  * /billing-summary — fleet revenue (Stripe MRR/MTD) + per-tenant sub status.
  * /users    — every auth identity merged with its user_profiles row and
                live session state (who is online right now).
  * /activity — the firehose at rest: recent interaction_logs across all
                tenants + the hash-chained audit ledger tail.
  * /outbox   — the AI emailer's queue, all tenants, with status rollup.
  * /system   — process uptime, DB health, WS groups, session registry.

Live counterpart: ws_hub mirrors every tenant's frames into the platform
firehose group, so the OPS tab's feed updates in real time over /ws.
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, status

import ws_hub
from auth import active_sessions, DEMO_TENANCY
from audit_ledger import ledger
from db.connection import tenant_tx
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.admin_ops")

router = APIRouter(prefix="/api/admin", tags=["Platform Admin Ops"])

_STARTED = time.time()  # process start (module import) for uptime reporting


# ---------------------------------------------------------------------------
# Gate — platform_admin or 403. Mounted as the dependency on every route.
# ---------------------------------------------------------------------------

def require_platform_admin(
    ctx: TenantContext = Depends(require_context),
) -> TenantContext:
    if not ctx.is_platform_admin:
        # Same posture as RLS: don't enumerate what exists behind the gate.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin only.",
        )
    return ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _j(value):
    """JSON-safe scalar — asyncpg hands back datetime/UUID objects."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row(record) -> dict:
    return {k: _j(v) for k, v in dict(record).items()}


async def _fetch(ctx: TenantContext, query: str, *args) -> list[dict]:
    """Run one read inside tenant_tx; translate a dead Memory Core into 503
    (crm.py house style) instead of a 500 stack trace."""
    try:
        async with tenant_tx(ctx) as conn:
            return [_row(r) for r in await conn.fetch(query, *args)]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — connection/pool failures
        logger.error("Admin query failed (Memory Core offline?): %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Core offline.",
        )


# ---------------------------------------------------------------------------
# /overview — the fleet at a glance
# ---------------------------------------------------------------------------

@router.get("/overview")
async def overview(ctx: TenantContext = Depends(require_platform_admin)):
    tenants = await _fetch(
        ctx,
        """
        SELECT t.id, t.slug, t.name, t.created_at,
               (SELECT count(*) FROM listings  l WHERE l.tenant_id = t.id)::int AS listings,
               (SELECT count(*) FROM clients   c WHERE c.tenant_id = t.id
                                                  AND c.archived_at IS NULL)::int AS clients,
               (SELECT count(*) FROM leads     d WHERE d.tenant_id = t.id)::int AS leads,
               (SELECT count(*) FROM showings  s WHERE s.tenant_id = t.id)::int AS showings,
               (SELECT count(*) FROM user_profiles p
                 WHERE p.tenant_id = t.id::text)::int AS agents,  -- user_profiles ids are TEXT
               (SELECT count(*) FROM email_outbox e WHERE e.tenant_id = t.id
                                                     AND e.status = 'queued')::int AS queued_emails,
               (SELECT max(i.created_at) FROM interaction_logs i
                 WHERE i.tenant_id = t.id) AS last_activity_at
        FROM tenants t
        ORDER BY t.created_at
        """,
    )

    count_keys = ("listings", "clients", "leads", "showings", "agents", "queued_emails")
    totals = {key: sum(t[key] for t in tenants) for key in count_keys}
    totals["tenants"] = len(tenants)

    return {
        "totals": totals,
        "tenants": tenants,
        "sessions": active_sessions(),
        "ws_groups": ws_hub.connection_counts(),
    }


# ---------------------------------------------------------------------------
# /billing-summary — fleet revenue (Stripe) + per-tenant subscription status
# Lets external dashboards (e.g. the Observatory) read aggregate revenue without
# ever holding the Stripe key — the key stays here, server-side.
# ---------------------------------------------------------------------------

def _monthly_minor(price: dict, qty) -> float:
    """Normalize one recurring Stripe price+quantity to a monthly amount (minor units)."""
    # A quantity of 0 means 0 units billed — only a missing quantity defaults to 1.
    qty = 1 if qty is None else qty
    amt = (price.get("unit_amount") or 0) * qty
    rec = price.get("recurring") or {}
    count = rec.get("interval_count") or 1
    interval = rec.get("interval")
    if interval == "month":
        return amt / count
    if interval == "year":
        return amt / (12 * count)
    if interval == "week":
        return amt * 52 / 12 / count
    if interval == "day":
        return amt * 365 / 12 / count
    return 0.0


def _stripe_revenue() -> dict:
    """Synchronous Stripe rollup (run in an executor): MRR from active
    subscriptions + month-to-date captured revenue (net of refunds).

    Amounts are segregated by currency — Stripe minor units are only additive
    within one currency (100 JPY minor units != 100 USD cents), so we never sum
    across currencies. The top-level currency/mrr/mtd_revenue report the dominant
    currency (largest MRR); `by_currency` carries the full per-currency breakdown
    so a mixed-currency account is represented honestly instead of conflated."""
    mrr_by_ccy: dict[str, float] = defaultdict(float)
    mtd_by_ccy: dict[str, float] = defaultdict(float)
    active = 0
    for sub in stripe.Subscription.list(status="active", limit=100).auto_paging_iter():
        active += 1
        ccy = (sub.get("currency") or "usd").lower()
        for item in (sub.get("items", {}).get("data") or []):
            mrr_by_ccy[ccy] += _monthly_minor(item.get("price") or {}, item.get("quantity"))

    now = datetime.now(timezone.utc)
    month_start = int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())
    payments = 0
    for ch in stripe.Charge.list(created={"gte": month_start}, limit=100).auto_paging_iter():
        if ch.get("status") == "succeeded":
            ccy = (ch.get("currency") or "usd").lower()
            mtd_by_ccy[ccy] += (ch.get("amount_captured") or 0) - (ch.get("amount_refunded") or 0)
            payments += 1

    # Dominant currency = largest MRR (tie-break on MTD), defaulting to usd.
    all_ccys = set(mrr_by_ccy) | set(mtd_by_ccy)
    primary = max(
        all_ccys,
        key=lambda c: (mrr_by_ccy.get(c, 0.0), mtd_by_ccy.get(c, 0.0)),
        default="usd",
    )
    by_currency = {
        c: {
            "mrr": round(mrr_by_ccy.get(c, 0.0)) / 100,
            "mtd_revenue": round(mtd_by_ccy.get(c, 0.0)) / 100,
        }
        for c in sorted(all_ccys)
    }

    return {
        "currency": primary,
        "mrr": round(mrr_by_ccy.get(primary, 0.0)) / 100,
        "mtd_revenue": round(mtd_by_ccy.get(primary, 0.0)) / 100,
        "active_subscriptions": active,
        "mtd_payments": payments,
        "by_currency": by_currency,
    }


@router.get("/billing-summary")
async def billing_summary(ctx: TenantContext = Depends(require_platform_admin)):
    # Per-tenant subscription status from our DB (entitlement source of truth).
    rows = await _fetch(
        ctx,
        """
        SELECT s.tenant_id, t.name AS tenant_name, s.status, s.plan, s.current_period_end
        FROM subscriptions s
        LEFT JOIN tenants t ON t.id = s.tenant_id
        ORDER BY t.name NULLS LAST
        """,
    )
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    # Aggregate revenue from Stripe (the money source of truth). Degrade to
    # DB-only counts if Stripe is unconfigured/unreachable.
    revenue = {
        "currency": "usd",
        "mrr": None,
        "mtd_revenue": None,
        "active_subscriptions": by_status.get("active", 0),
        "mtd_payments": None,
        "stripe_ok": False,
    }
    if stripe.api_key:
        try:
            loop = asyncio.get_event_loop()
            rolled = await loop.run_in_executor(None, _stripe_revenue)
            revenue = {**revenue, **rolled, "stripe_ok": True}
        except Exception as exc:  # noqa: BLE001 — Stripe/network failures
            logger.error("Stripe revenue rollup failed: %s", exc)

    return {
        **revenue,
        "subscriptions_by_status": by_status,
        "tenants": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# /users — every identity: auth map ∪ user_profiles, with live session state
# ---------------------------------------------------------------------------

@router.get("/users")
async def users(ctx: TenantContext = Depends(require_platform_admin)):
    profiles = await _fetch(
        ctx,
        """
        SELECT p.user_id, p.tenant_id, t.name AS tenant_name, p.display_name,
               p.public_email, p.phone, p.brokerage, p.license_number,
               p.experience_level, p.summary_updated_at
        FROM user_profiles p
        LEFT JOIN tenants t ON t.id::text = p.tenant_id
        ORDER BY t.name NULLS LAST, p.user_id
        """,
    )

    live = {s["agent_id"]: s for s in active_sessions()}
    by_id = {p["user_id"]: p for p in profiles}

    merged: list[dict] = []
    # Auth identities first (passphrases never leave auth.py — IDs only).
    for agent_id, (tenant_id, role) in DEMO_TENANCY.items():
        profile = by_id.pop(agent_id, None) or {}
        session = live.get(agent_id)
        merged.append(
            {
                "agent_id": agent_id,
                "tenant_id": profile.get("tenant_id", tenant_id),
                "tenant_name": profile.get("tenant_name"),
                "role": role,
                "display_name": profile.get("display_name"),
                "public_email": profile.get("public_email"),
                "brokerage": profile.get("brokerage"),
                "has_profile": bool(profile),
                "online": session is not None,
                "session_issued_at": session["issued_at"] if session else None,
                "session_expires_at": session["expires_at"] if session else None,
            }
        )
    # Profile rows with no auth-map identity (future real-user table rows).
    for user_id, profile in by_id.items():
        session = live.get(user_id)
        merged.append(
            {
                "agent_id": user_id,
                "tenant_id": profile.get("tenant_id"),
                "tenant_name": profile.get("tenant_name"),
                "role": "agent",
                "display_name": profile.get("display_name"),
                "public_email": profile.get("public_email"),
                "brokerage": profile.get("brokerage"),
                "has_profile": True,
                "online": session is not None,
                "session_issued_at": session["issued_at"] if session else None,
                "session_expires_at": session["expires_at"] if session else None,
            }
        )

    merged.sort(key=lambda u: (not u["online"], u["agent_id"]))
    return {"users": merged, "online": sum(1 for u in merged if u["online"])}


# ---------------------------------------------------------------------------
# /activity — recent cross-tenant interactions + the audit ledger tail
# ---------------------------------------------------------------------------

@router.get("/activity")
async def activity(
    limit: int = Query(default=60, ge=1, le=200),
    ctx: TenantContext = Depends(require_platform_admin),
):
    interactions = await _fetch(
        ctx,
        """
        SELECT i.id, i.tenant_id, i.lead_id, i.client_id, i.actor_role,
               i.interaction_type, i.direction, i.subject, i.created_at,
               c.full_name AS client_name, t.name AS tenant_name
        FROM interaction_logs i
        LEFT JOIN clients c ON c.id = i.client_id
        LEFT JOIN tenants t ON t.id = i.tenant_id
        ORDER BY i.created_at DESC
        LIMIT $1
        """,
        limit,
    )

    try:
        audit = await ledger.get_entries(limit=limit)
    except Exception as exc:  # noqa: BLE001 — ledger degrades, never blocks the feed
        logger.warning("Audit ledger read failed: %s", exc)
        audit = []

    return {"interactions": interactions, "audit": audit}


# ---------------------------------------------------------------------------
# /outbox — the AI emailer's queue across every tenant
# ---------------------------------------------------------------------------

@router.get("/outbox")
async def outbox(
    limit: int = Query(default=60, ge=1, le=200),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    ctx: TenantContext = Depends(require_platform_admin),
):
    rollup = await _fetch(
        ctx,
        "SELECT status, count(*)::int AS n FROM email_outbox GROUP BY status",
    )

    if status_filter:
        rows = await _fetch(
            ctx,
            """
            SELECT e.id, e.tenant_id, t.name AS tenant_name, e.to_email, e.subject,
                   e.status, e.error, e.template_key, e.scheduled_at, e.sent_at,
                   e.created_by, e.created_at
            FROM email_outbox e
            LEFT JOIN tenants t ON t.id = e.tenant_id
            WHERE e.status = $2
            ORDER BY e.created_at DESC
            LIMIT $1
            """,
            limit,
            status_filter,
        )
    else:
        rows = await _fetch(
            ctx,
            """
            SELECT e.id, e.tenant_id, t.name AS tenant_name, e.to_email, e.subject,
                   e.status, e.error, e.template_key, e.scheduled_at, e.sent_at,
                   e.created_by, e.created_at
            FROM email_outbox e
            LEFT JOIN tenants t ON t.id = e.tenant_id
            ORDER BY e.created_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {"counts": {r["status"]: r["n"] for r in rollup}, "emails": rows}


# ---------------------------------------------------------------------------
# /system — process + Memory Core + socket health
# ---------------------------------------------------------------------------

@router.get("/system")
async def system(ctx: TenantContext = Depends(require_platform_admin)):
    db_health = {"status": "offline", "version": None, "connections": None}
    try:
        async with tenant_tx(ctx) as conn:
            db_health["version"] = (await conn.fetchval("SELECT version()")).split(" on ")[0]
            db_health["connections"] = await conn.fetchval(
                "SELECT count(*)::int FROM pg_stat_activity WHERE datname = current_database()"
            )
            db_health["status"] = "online"
    except Exception as exc:  # noqa: BLE001 — health endpoint must never 500
        logger.warning("System health DB probe failed: %s", exc)

    sessions = active_sessions()
    return {
        "uptime_seconds": round(time.time() - _STARTED, 1),
        "db": db_health,
        "ws_groups": ws_hub.connection_counts(),
        "firehose_watchers": ws_hub.connection_counts().get(ws_hub.FIREHOSE_TENANT_ID, 0),
        "active_sessions": len(sessions),
        "sessions": sessions,
    }
