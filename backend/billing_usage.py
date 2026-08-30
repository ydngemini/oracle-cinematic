"""
Usage metering — the dimension ``billing.py`` cannot express.

``billing.py`` opens checkout with a single ``STRIPE_PRICE_ID`` at
``quantity=1``. That shape can bill a flat monthly fee and nothing else: there
is no seat count to grow and no usage line to attach. The vault research
(``Research/Deep/2026-08-06 — proptech-revenue-patterns``) found seat-based SaaS
posting 95% median net revenue retention against 108% for usage-based, and that
the closest comparable in this category prices per lead rather than per seat.

This module is the local ledger for that dimension. Three deliberate choices:

**Recording is unconditional; reporting is optional.** Usage rows are written
whether or not ``STRIPE_METERED_PRICE_ID`` is configured. Which unit Oracle
should actually bill on is an open question, and the answer will be argued from
a usage history that only exists if we start keeping it before we decide. A
metric that begins accruing on the day pricing changes can prove nothing about
the change.

**Idempotency is a table constraint.** Double-reported usage is a billing
incident, not a bug report. ``(tenant_id, idempotency_key)`` is UNIQUE in 0067
and every writer supplies a natural key derived from the event it describes.

**Reporting failures are recorded, never raised.** A Stripe outage must not fail
the CRM action that generated the usage. Unreported rows stay unreported and the
drain picks them up next pass — the same durable-drain posture the audit ledger
uses.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import stripe
from fastapi import APIRouter, Depends, Query

from db.connection import tenant_tx
from tenancy import Role, TenantContext, require_context, require_role

logger = logging.getLogger("oracle.billing.usage")

router = APIRouter(prefix="/api/billing", tags=["Billing"])

# Optional. Unset is a fully supported steady state: meter locally, bill flat.
STRIPE_METERED_PRICE_ID = os.getenv("STRIPE_METERED_PRICE_ID", "").strip()
# Stripe meters are addressed by event_name, which is NOT the price id.
STRIPE_METER_EVENT_NAME = os.getenv("STRIPE_METER_EVENT_NAME", "oracle_lead_engaged").strip()

# Mirrors the CHECK constraint in 0067. Kept in Python too so a bad metric name
# fails at the call site with a clear error instead of as a constraint violation
# 500 several frames away.
METRICS = frozenset({
    "lead_engaged",        # a first-response touch was staged for an inbound lead
    "ai_voice_minute",     # AI-handled call minutes
    "transaction_closed",  # a deal reached closed — the outcome-pricing unit
    "media_capture",       # a property capture landed (photo/video/splat/floorplan)
    # Inference consumption, split because prompt and completion tokens are
    # priced differently by every provider and summing them loses the ratio that
    # decides whether a prompt or an answer is the expensive half.
    #
    # Requests were already capped (20/min/agent in rate_limiter) but SPEND was
    # not, and nothing recorded it — so on a flat $299 plan the first sign of a
    # runaway conversation would have been the provider invoice. Recording it
    # while metered_billing_enabled is false is the point: the history has to
    # exist before the pricing question can be answered honestly.
    "ai_prompt_tokens",
    "ai_completion_tokens",
})

# Only one metric maps to a Stripe meter today. The others accrue locally until a
# pricing decision is made — see the module docstring.
_STRIPE_REPORTED = frozenset({"lead_engaged"})


def metering_configured() -> bool:
    return bool(STRIPE_METERED_PRICE_ID)


async def record_inference(
    ctx: TenantContext,
    response: Any,
    *,
    idempotency_key: str,
) -> tuple[int, int]:
    """Meter one model call's token consumption. Returns what was recorded.

    Every inference path in the product funnels through here rather than each
    one reaching for record_usage with its own metric name — the same reasoning
    that puts every tool through a single dispatcher. A path that forgets to
    meter is invisible, and invisible spend on a flat plan is the whole problem.

    A call that reports no usage records nothing rather than a zero. Zero is a
    real measurement — "this call cost nothing" — and a provider that simply
    omits the field has not made that measurement.
    """
    from llm_gateway import token_usage_of

    prompt_tokens, completion_tokens = token_usage_of(response)
    if not (prompt_tokens or completion_tokens):
        return (0, 0)

    if prompt_tokens:
        await record_usage(
            ctx,
            metric="ai_prompt_tokens",
            quantity=prompt_tokens,
            idempotency_key=f"{idempotency_key}:prompt",
        )
    if completion_tokens:
        await record_usage(
            ctx,
            metric="ai_completion_tokens",
            quantity=completion_tokens,
            idempotency_key=f"{idempotency_key}:completion",
        )
    return (prompt_tokens, completion_tokens)


async def record_usage(
    ctx: TenantContext,
    *,
    metric: str,
    quantity: float | int | Decimal = 1,
    idempotency_key: str,
    occurred_at: Optional[datetime] = None,
    conn: Any = None,
) -> bool:
    """Append one usage row. Returns True when a NEW row was written.

    Never raises into the caller. Every call site is a CRM action whose success
    is independent of whether we managed to record that it happened.

    When ``conn`` is the caller's own in-flight transaction, swallowing the error
    is not enough: a failed statement aborts the entire Postgres transaction, so
    the caller's next statement and its COMMIT would fail too. The insert is
    therefore isolated in a SAVEPOINT — a metering failure rolls back only this
    row and leaves the caller's work committable.
    """
    if metric not in METRICS:
        logger.warning("Refusing to record unknown usage metric %r", metric)
        return False
    if quantity is None or Decimal(str(quantity)) < 0:
        logger.warning("Refusing to record negative usage: metric=%s qty=%r", metric, quantity)
        return False

    sql = """
        INSERT INTO billing_usage_events (
            tenant_id,metric,quantity,occurred_at,idempotency_key
        ) VALUES ($1::uuid,$2,$3::numeric,$4,$5)
        ON CONFLICT (tenant_id,idempotency_key) DO NOTHING
        RETURNING id
    """
    args = (
        ctx.tenant_id, metric, Decimal(str(quantity)),
        occurred_at or datetime.now(timezone.utc), idempotency_key[:240],
    )
    try:
        if conn is not None:
            # Nested transaction() on an asyncpg connection already inside a
            # transaction emits SAVEPOINT/ROLLBACK TO, which is exactly the
            # containment this needs.
            async with conn.transaction():
                row = await conn.fetchrow(sql, *args)
        else:
            async with tenant_tx(ctx) as c:
                row = await c.fetchrow(sql, *args)
        return row is not None
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning(
            "Usage record deferred: metric=%s key=%s", metric, idempotency_key, exc_info=True,
        )
        return False


async def drain_usage_to_stripe(ctx: TenantContext, *, limit: int = 200) -> dict[str, Any]:
    """Push unreported usage to Stripe's meter, oldest first.

    Each row carries its own ``identifier`` so a retry after a partial failure
    cannot double-count — Stripe de-duplicates on it, and our UNIQUE key means we
    never generate two identifiers for the same underlying event.

    Every statement filters ``tenant_id`` explicitly rather than leaning on RLS.
    The sweeper below runs this under a platform-admin context (the same posture
    ``billing.py`` uses for webhooks), and under that context the RLS predicate
    is ``app_is_platform_admin() OR ...`` — i.e. wide open. Without the explicit
    filter the drain would meter one tenant's usage against another tenant's
    Stripe customer, which is a billing incident, not a bug report.
    """
    if not metering_configured():
        return {"state": "unconfigured", "reported": 0}

    customer_id = await _stripe_customer_for(ctx)
    if not customer_id:
        # Honest degradation: a tenant with no Stripe customer has nothing to
        # meter against. The rows stay unreported rather than being dropped.
        return {"state": "no_customer", "reported": 0}

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,metric,quantity,occurred_at,idempotency_key
              FROM billing_usage_events
             WHERE tenant_id=$3::uuid
               AND reported_at IS NULL AND metric = ANY($1::text[])
             ORDER BY occurred_at ASC
             LIMIT $2
            """,
            sorted(_STRIPE_REPORTED),
            limit,
            ctx.tenant_id,
        )

    reported = 0
    for row in rows:
        try:
            event = stripe.billing.MeterEvent.create(
                event_name=STRIPE_METER_EVENT_NAME,
                payload={
                    "stripe_customer_id": customer_id,
                    "value": str(row["quantity"]),
                },
                identifier=str(row["idempotency_key"])[:100],
                timestamp=int(row["occurred_at"].timestamp()),
            )
            async with tenant_tx(ctx) as conn:
                await conn.execute(
                    """
                    UPDATE billing_usage_events
                       SET reported_at=now(),stripe_event_id=$2,report_error=NULL
                     WHERE id=$1::uuid AND tenant_id=$3::uuid
                    """,
                    row["id"],
                    getattr(event, "identifier", None) or str(row["idempotency_key"])[:100],
                    ctx.tenant_id,
                )
            reported += 1
        except Exception as exc:  # noqa: BLE001 — recorded on the row, drain continues
            # One poisoned row must not block the queue behind it. The error is
            # stamped so a stuck row is visible instead of silently retried forever.
            async with tenant_tx(ctx) as conn:
                await conn.execute(
                    "UPDATE billing_usage_events SET report_error=$2"
                    " WHERE id=$1::uuid AND tenant_id=$3::uuid",
                    row["id"],
                    (str(exc).strip() or exc.__class__.__name__)[:500],
                    ctx.tenant_id,
                )
            logger.warning("Usage report failed for %s: %s", row["id"], exc)

    return {"state": "ok", "reported": reported, "considered": len(rows)}


async def drain_all_tenants(
    *, tenant_limit: int = 500, per_tenant_limit: int = 200
) -> dict[str, Any]:
    """Sweep every tenant holding unreported meterable usage. Scheduler entry point.

    ``drain_usage_to_stripe`` is per-tenant because a meter event is addressed to
    one Stripe customer. Nothing was calling it on a timer, so recorded usage
    accrued locally and never reached Stripe even when metering was configured —
    this closes that loop.

    Tenants are ordered by their oldest pending row, so a tenant that has been
    waiting longest drains first and no tenant can be starved by a noisier one.
    """
    if not metering_configured():
        return {"state": "unconfigured", "tenants": 0, "reported": 0}

    # Cross-tenant read: the whole point of the sweep is to find tenants we have
    # no context for yet. Same platform-admin posture as billing.py's webhook
    # path; per-tenant work below re-scopes to a single tenant.
    platform_ctx = TenantContext(
        agent_id="usage-drain",
        tenant_id=os.getenv(
            "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        ),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT tenant_id, count(*)::int AS pending
              FROM billing_usage_events
             WHERE reported_at IS NULL AND metric = ANY($1::text[])
             GROUP BY tenant_id
             ORDER BY min(occurred_at) ASC
             LIMIT $2
            """,
            sorted(_STRIPE_REPORTED),
            max(1, min(5000, int(tenant_limit))),
        )

    reported = 0
    drained = 0
    states: dict[str, int] = {}
    for row in rows:
        tenant_ctx = TenantContext(
            agent_id="usage-drain",
            tenant_id=str(row["tenant_id"]),
            role=Role.PLATFORM_ADMIN,
        )
        try:
            result = await drain_usage_to_stripe(tenant_ctx, limit=per_tenant_limit)
        except Exception as exc:  # noqa: BLE001 — one tenant must not stop the sweep
            logger.warning("Usage drain failed for tenant %s: %s", row["tenant_id"], exc)
            states["error"] = states.get("error", 0) + 1
            continue
        state = str(result.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
        reported += int(result.get("reported") or 0)
        drained += 1

    logger.info(
        "ORACLE_METRIC usage_meter_drain tenants=%d reported=%d", drained, reported
    )
    return {
        "state": "ok",
        "tenants": drained,
        "tenants_pending": len(rows),
        "reported": reported,
        "by_state": states,
    }


async def _stripe_customer_for(ctx: TenantContext) -> Optional[str]:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT stripe_customer_id FROM subscriptions
             WHERE tenant_id=$1::uuid AND stripe_customer_id IS NOT NULL
             ORDER BY updated_at DESC LIMIT 1
            """,
            ctx.tenant_id,
        )
    return str(row["stripe_customer_id"]) if row else None


@router.get("/usage")
async def usage_summary(
    days: int = Query(default=30, ge=1, le=365),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """What this tenant consumed, and whether any of it is billable yet."""
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            -- tenant_id is filtered explicitly, not left to RLS — the same rule
            -- drain_usage_to_stripe documents. require_role(BROKER_OWNER) passes
            -- for platform admins, and under that context the 0067 policy is
            -- `app_is_platform_admin() OR tenant_id = app_current_tenant()`,
            -- i.e. wide open: without this predicate an admin's own usage panel
            -- reports the sum of EVERY tenant's activity as its own.
            SELECT metric,
                   sum(quantity)::numeric AS quantity,
                   count(*)::int AS events,
                   count(*) FILTER (WHERE reported_at IS NOT NULL)::int AS reported,
                   max(occurred_at) AS last_at
              FROM billing_usage_events
             WHERE tenant_id = $2::uuid
               AND occurred_at >= now() - ($1::int * interval '1 day')
             GROUP BY metric ORDER BY metric
            """,
            days,
            ctx.tenant_id,
        )
    return {
        "window_days": days,
        "metrics": [
            {
                "metric": r["metric"],
                "quantity": float(r["quantity"] or 0),
                "events": r["events"],
                "reported_to_stripe": r["reported"],
                "last_at": r["last_at"].isoformat() if r["last_at"] else None,
            }
            for r in rows
        ],
        # The frontend must not imply a usage charge when none is configured.
        # This flag is why the Billing overlay can show consumption honestly
        # while the plan is still flat.
        "metered_billing_enabled": metering_configured(),
        "evidence_status": "observed",
    }
