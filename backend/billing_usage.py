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

import asyncio
import functools
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
    "lead_engaged",        # a speed-to-lead first response was approved AND delivered (0133)
    "ai_voice_minute",     # AI-handled call minutes
    "transaction_closed",  # a deal reached closed — the outcome-pricing unit
    "media_capture",       # a property capture landed (photo/video/splat/floorplan)
    # Inference consumption, split because prompt and completion tokens are
    # priced differently by every provider and summing them loses the ratio that
    # decides whether a prompt or an answer is the expensive half.
    #
    # Requests were already capped (20/min/agent in rate_limiter) but SPEND was
    # not, and nothing recorded it — so on a flat-price plan the first sign of a
    # runaway conversation would have been the provider invoice. Recording it
    # while metered_billing_enabled is false is the point: the history has to
    # exist before the pricing question can be answered honestly.
    "ai_prompt_tokens",
    "ai_completion_tokens",
})

# Only one metric maps to a Stripe meter today. The others accrue locally until a
# pricing decision is made — see the module docstring.
_STRIPE_REPORTED = frozenset({"lead_engaged"})

# Metrics that may only be written at a named lifecycle stage, by their own
# writer — never through the generic record_usage. lead_engaged was metered when
# a draft was merely STAGED (audit A05); it is now written only by
# record_lead_engagement, at outreach_lifecycle.BILLABLE_ENGAGEMENT_STAGE.
_STAGE_BOUND = frozenset({"lead_engaged"})

# The rows that may be reported to a meter. A lead_engaged row is billable only
# when it names the delivered event it bills for (0133 CHECK); rows written by
# the pre-0133 code at the staged step have no stage and are never reported.
# Every query that counts "pending billing" must use this, or a legacy staged
# row reads as a bill that is waiting to go out.
# IS NOT DISTINCT FROM, not =: a legacy row's NULL stage must make this FALSE,
# never NULL, or "NOT billable" would silently drop it from a count too.
def billable_predicate(alias: str = "") -> str:
    col = f"{alias}." if alias else ""
    return (f"({col}metric <> 'lead_engaged'"
            f" OR {col}lifecycle_stage IS NOT DISTINCT FROM 'delivered')")


BILLABLE_PREDICATE = billable_predicate()


def metering_configured() -> bool:
    return bool(STRIPE_METERED_PRICE_ID)


async def record_inference(
    ctx: TenantContext,
    response: Any,
    *,
    idempotency_key: str,
    conn: Any = None,
    raise_errors: bool = False,
) -> tuple[int, int]:
    """Meter one model call's token consumption. Returns what was recorded.

    Every inference path in the product funnels through here rather than each
    one reaching for record_usage with its own metric name — the same reasoning
    that puts every tool through a single dispatcher. A path that forgets to
    meter is invisible, and invisible spend on a flat plan is the whole problem.

    A call that reports no usage records nothing rather than a zero. Zero is a
    real measurement — "this call cost nothing" — and a provider that simply
    omits the field has not made that measurement.

    ``conn`` is the caller's own transaction (ai_budget.settle records the
    usage and drops the call's hold in one), as for record_usage.
    ``raise_errors`` passes through to record_usage: settle must know a row
    was refused, or it drops the hold and the tokens count nowhere (BILL-001).
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
            conn=conn,
            raise_errors=raise_errors,
        )
    if completion_tokens:
        await record_usage(
            ctx,
            metric="ai_completion_tokens",
            quantity=completion_tokens,
            idempotency_key=f"{idempotency_key}:completion",
            conn=conn,
            raise_errors=raise_errors,
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
    raise_errors: bool = False,
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
    if metric in _STAGE_BOUND:
        logger.warning(
            "Refusing to record %s outside its lifecycle stage; use record_lead_engagement",
            metric,
        )
        return False
    # BILL-003: Decimal("abc") and NaN < 0 raised out of a function documented
    # never to raise, and Infinity passed and would be stored as infinite usage.
    try:
        qty = Decimal(str(quantity)) if quantity is not None else None
    except (InvalidOperation, ValueError, TypeError):
        qty = None
    if qty is None or not qty.is_finite() or qty < 0:
        logger.warning("Refusing to record invalid usage: metric=%s qty=%r", metric, quantity)
        return False

    sql = """
        INSERT INTO billing_usage_events (
            tenant_id,metric,quantity,occurred_at,idempotency_key
        ) VALUES ($1::uuid,$2,$3::numeric,$4,$5)
        ON CONFLICT (tenant_id,idempotency_key) DO NOTHING
        RETURNING id
    """
    args = (
        ctx.tenant_id, metric, qty,
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
        if raise_errors:
            raise
        return False


async def record_lead_engagement(
    ctx: TenantContext,
    *,
    conn: Any,
    idempotency_key: str,
    command_id: str,
    occurred_at: Optional[datetime] = None,
) -> bool:
    """Meter one billable lead engagement. Returns True for a NEW billable row.

    Called only by outreach_lifecycle.meter_delivered_engagement, after it has
    re-read that the command was approved and accepted and its first response
    delivered. The row names that command (source_id), which is what the 0133
    CHECK demands of a billable lead_engaged row.

    The key is the pre-0133 one, so a lead the old code metered at the staged
    step already has a row. That row is PROMOTED — given its delivered stage and
    source — instead of a second row being written; one engagement per lead,
    ever. A legacy row already reported to Stripe is left alone (it shows up in
    reconcile_lead_engagements for a human), and so is a row that is already
    billable, which is what makes a redelivered receipt count zero times.
    """
    sql = """
        INSERT INTO billing_usage_events (
            tenant_id,metric,quantity,occurred_at,idempotency_key,
            lifecycle_stage,source_table,source_id
        ) VALUES ($1::uuid,'lead_engaged',1,$2,$3,'delivered','command_executions',$4::uuid)
        ON CONFLICT (tenant_id,idempotency_key) DO UPDATE
           SET lifecycle_stage='delivered', source_table='command_executions',
               source_id=EXCLUDED.source_id, occurred_at=EXCLUDED.occurred_at
         WHERE billing_usage_events.metric='lead_engaged'
           AND billing_usage_events.lifecycle_stage IS NULL
           AND billing_usage_events.reported_at IS NULL
        RETURNING id
    """
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                sql, ctx.tenant_id, occurred_at or datetime.now(timezone.utc),
                idempotency_key[:240], str(command_id),
            )
        return row is not None
    except Exception:  # noqa: BLE001 — same contract as record_usage
        logger.warning(
            "Engagement record deferred: key=%s command=%s", idempotency_key, command_id,
            exc_info=True,
        )
        return False


async def reconcile_lead_engagements(ctx: TenantContext, *, limit: int = 200) -> list[dict[str, Any]]:
    """Usage rows that do not reconcile to a delivered event (AC18.04).

    Two kinds, both for a human to resolve, neither fixed automatically:
      * ``billable_without_delivery`` — a billable lead_engaged row whose
        command is not succeeded, or whose first response is not delivered.
        The 0133 CHECK and the writer make this impossible; this proves it.
      * ``reported_before_delivery`` — a pre-0133 row metered at the staged
        step that a drain already pushed to Stripe. If any exist, those
        customers were billed for drafts; the meter event has to be credited.
    """
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT u.id, u.idempotency_key, u.occurred_at, u.reported_at,
                   u.lifecycle_stage, u.source_id, c.state AS command_state,
                   r.disposition,
                   CASE WHEN u.lifecycle_stage = 'delivered'
                        THEN 'billable_without_delivery'
                        ELSE 'reported_before_delivery' END AS problem
              FROM billing_usage_events u
              LEFT JOIN command_executions c
                ON c.id = u.source_id AND c.tenant_id = u.tenant_id
              LEFT JOIN lead_response_events r
                ON r.command_id = u.source_id AND r.tenant_id = u.tenant_id
             WHERE u.tenant_id = $1::uuid AND u.metric = 'lead_engaged'
               AND (
                    (u.lifecycle_stage = 'delivered'
                     AND (c.state IS DISTINCT FROM 'succeeded'
                          OR r.disposition IS DISTINCT FROM 'delivered'))
                 OR (u.lifecycle_stage IS NULL AND u.reported_at IS NOT NULL)
               )
             ORDER BY u.occurred_at
             LIMIT $2
            """,
            ctx.tenant_id,
            max(1, min(1000, int(limit))),
        )
    return [dict(row) for row in rows]


# ── Reporting usage to Stripe (audits 24.5, 24.6; migration 0175) ───────────
# Stripe de-duplicates a meter event's identifier for a rolling period of at
# least 24 hours, no longer. Inside that window a resend with the same
# identifier is safe whether or not the first attempt landed; after it, a
# resend may bill the lead twice. So every attempt is recorded (and the row
# claimed) BEFORE Stripe is called, a row may be resent only while its first
# uncertain attempt is younger than this, and a row still unacknowledged after
# it is quarantined for a person to reconcile — never resent automatically.
REPORT_RESEND_WINDOW_HOURS = 20
# Stripe accepts a meter event whose timestamp is within the past 35 days. A
# day of margin: a row this old is quarantined as 'expired' and leaves the drain.
REPORT_EXPIRY_DAYS = 34
# Unreported usage this old is alerted on, a week before it would expire.
REPORT_AGING_ALERT_DAYS = 28
# One row is claimed at a time and its lease outlives one Stripe call (one HTTP
# attempt: the SDK's own retries are off, below; it times out at 80 s), so a
# second drain replica does not send a leased row. Should a send outlive the
# lease anyway, every write after it names the attempt it belongs to.
REPORT_CLAIM_LEASE_SECONDS = 300
# The Stripe call runs in a worker thread (24.7: the SDK's synchronous client
# would block the event loop that also runs job heartbeats and, all-in-one,
# live calls). Waited on for at most this long. A timeout is an unknown outcome
# and the thread may still finish, so the row KEEPS its claim until the lease
# lapses (no other drain sends it meanwhile). The SDK's timeout is per socket
# read (80 s), not per call, so a send could in principle outlive even the
# lease; then the resend reuses the identifier and Idempotency-Key and Stripe
# de-duplicates it (24 h) — that, not the lease, is the final guarantee.
REPORT_SEND_TIMEOUT_SECONDS = 30
# This many transport failures in a row (timeout, connection error, Stripe 5xx)
# means Stripe is not answering: the drain stops instead of spending
# REPORT_SEND_TIMEOUT_SECONDS on every pending row of every brokerage (and the
# sweep stops with it). A fast, row-specific answer such as an idempotency
# conflict does not count: it says nothing about Stripe's health.
REPORT_MAX_CONSECUTIVE_UNKNOWN = 3


def _transport_failure(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, stripe.APIConnectionError)):
        return True
    if isinstance(exc, stripe.APIError):
        status_code = getattr(exc, "http_status", None)
        return status_code is None or int(status_code) >= 500
    return False
# The resolutions a person may record for a quarantined row (0175).
REPORT_RESOLUTIONS = frozenset({"billed", "credited", "written_off"})

# A row still in play: not reported, not quarantined.
_IN_PLAY = "reported_at IS NULL AND report_state IS NULL"

# Errors with which Stripe REFUSED the request: the event was not recorded, so
# the attempt adds no uncertainty. Everything else — a connection failure, a
# timeout, a 5xx, an idempotency conflict, our own failure after Stripe
# answered — may have recorded it. Only valid because each send is ONE HTTP
# attempt (max_network_retries=0): with the SDK's retries a refusal could be
# the answer to a retry of an attempt that timed out after Stripe recorded it.
# A connection failure is counted as possibly recorded even when nothing left
# this host (the SDK does not say which); after the window a person decides.
_REFUSED = (
    stripe.InvalidRequestError, stripe.AuthenticationError, stripe.PermissionError,
    stripe.RateLimitError, stripe.CardError,
)
# The v2 meter-event API answers a duplicate identifier with this code; it
# proves an earlier attempt landed. (Any OTHER refusal of a row that already had
# an uncertain attempt leaves that uncertainty standing.)
_DUPLICATE_EVENT = "duplicate_meter_event"


def _idempotency_key(identifier: str) -> str:
    """The same Idempotency-Key for every send of one row: a resend inside
    Stripe's 24 h idempotency window replays Stripe's answer to the first
    request instead of being processed again."""
    return f"oracle-meter-event:{identifier}"


def _error_text(exc: BaseException) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:500]


async def _quarantine_unreportable(conn: Any, tenant_id: str) -> dict[str, int]:
    """Take rows that may no longer be sent out of the drain (24.5, 24.6).

    'uncertain': an attempt that may have reached Stripe began more than
    REPORT_RESEND_WINDOW_HOURS ago and was never acknowledged. Resending could
    bill twice; a person checks the Stripe meter and settles it.
    'expired': older than Stripe accepts. Kept for a manual billing or credit
    decision instead of failing every sweep ahead of current usage.
    A row under a live claim is left to its sender.
    """
    rows = await conn.fetch(
        """
        UPDATE billing_usage_events
           SET report_state = CASE WHEN report_uncertain_since IS NOT NULL
                                   THEN 'uncertain' ELSE 'expired' END,
               report_quarantined_at = now(),
               report_claimed_until = NULL,
               updated_at = now()
         WHERE tenant_id = $1::uuid AND """ + _IN_PLAY + """
           AND metric = ANY($2::text[]) AND """ + BILLABLE_PREDICATE + """
           AND (report_claimed_until IS NULL OR report_claimed_until < now())
           AND (occurred_at <= now() - make_interval(days => $3)
                OR report_uncertain_since <= now() - make_interval(hours => $4))
        RETURNING report_state
        """,
        tenant_id, sorted(_STRIPE_REPORTED), REPORT_EXPIRY_DAYS, REPORT_RESEND_WINDOW_HOURS,
    )
    counts = {"expired": 0, "uncertain": 0}
    for row in rows:
        counts[row["report_state"]] = counts.get(row["report_state"], 0) + 1
    if rows:
        logger.warning(
            "ORACLE_ALERT usage_meter_quarantined tenant=%s expired=%d uncertain=%d"
            " (reconcile against the Stripe meter; these rows are never resent)",
            tenant_id, counts["expired"], counts["uncertain"],
        )
    return counts


async def _claim_next_report(conn: Any, tenant_id: str, skip: list[str]) -> Optional[Any]:
    """Claim one sendable row and record, before any send, that an attempt began.

    FOR UPDATE SKIP LOCKED + a lease: two drain replicas never hold one row.
    report_uncertain_since is set in the same statement (kept if already set),
    so a crash between Stripe's acceptance and our acknowledgement still leaves
    a durable record that this row may be billed. Rows with an open uncertain
    attempt go first — they have a deadline — then never-tried rows oldest
    first, then rows Stripe refused before.
    """
    return await conn.fetchrow(
        """
        WITH picked AS (
            SELECT id, report_uncertain_since AS prior_uncertain_since
              FROM billing_usage_events
             WHERE tenant_id = $1::uuid AND """ + _IN_PLAY + """
               AND metric = ANY($2::text[]) AND """ + BILLABLE_PREDICATE + """
               AND (report_claimed_until IS NULL OR report_claimed_until < now())
               AND occurred_at > now() - make_interval(days => $3)
               AND (report_uncertain_since IS NULL
                    OR report_uncertain_since > now() - make_interval(hours => $4))
               AND NOT (id = ANY($5::uuid[]))
             ORDER BY (report_uncertain_since IS NULL), (report_attempts > 0), occurred_at
             LIMIT 1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE billing_usage_events u
           SET report_claimed_until = now() + make_interval(secs => $6),
               report_attempts = u.report_attempts + 1,
               report_uncertain_since = COALESCE(u.report_uncertain_since, now()),
               updated_at = now()
          FROM picked
         WHERE u.id = picked.id
        RETURNING u.id, u.metric, u.quantity, u.occurred_at, u.idempotency_key,
                  u.report_attempts AS attempt, picked.prior_uncertain_since
        """,
        tenant_id, sorted(_STRIPE_REPORTED), REPORT_EXPIRY_DAYS, REPORT_RESEND_WINDOW_HOURS,
        skip, REPORT_CLAIM_LEASE_SECONDS,
    )


async def _aging_count(conn: Any, tenant_id: str) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) FROM billing_usage_events WHERE tenant_id = $1::uuid AND "
        + _IN_PLAY + " AND metric = ANY($2::text[]) AND " + BILLABLE_PREDICATE
        + " AND occurred_at <= now() - make_interval(days => $3)",
        tenant_id, sorted(_STRIPE_REPORTED), REPORT_AGING_ALERT_DAYS,
    ) or 0)


async def drain_usage_to_stripe(ctx: TenantContext, *, limit: int = 200) -> dict[str, Any]:
    """Push unreported usage to Stripe's meter, at most once per event.

    Each row carries its own ``identifier`` (its UNIQUE idempotency key), and
    each send is claimed and recorded first (``_claim_next_report``). A row
    whose send had an unknown outcome is resent only inside Stripe's
    de-duplication window, then quarantined for a person (24.5). Rows older
    than Stripe accepts are quarantined instead of blocking the queue (24.6).

    Every statement filters ``tenant_id`` explicitly rather than leaning on RLS.
    The sweeper below runs this under a platform-admin context (the same posture
    ``billing.py`` uses for webhooks), and under that context the RLS predicate
    is ``app_is_platform_admin() OR ...`` — i.e. wide open. Without the explicit
    filter the drain would meter one tenant's usage against another tenant's
    Stripe customer, which is a billing incident, not a bug report.
    """
    if not metering_configured():
        return {"state": "unconfigured", "reported": 0}
    import recovery_mode

    if recovery_mode.is_recovery_mode():
        # A restored copy must not report usage to the live Stripe meter: that
        # bills the customer a second time for usage already metered (OUT-1).
        return {"state": "recovery_mode", "reported": 0}

    tenant_id = str(ctx.tenant_id)
    async with tenant_tx(ctx) as conn:
        quarantined = await _quarantine_unreportable(conn, tenant_id)

    customer_id = await _stripe_customer_for(ctx)
    if not customer_id:
        # Honest degradation: a tenant with no Stripe customer has nothing to
        # meter against. The rows stay unreported rather than being dropped.
        return {"state": "no_customer", "reported": 0, "quarantined": quarantined}

    reported = 0
    unknown_streak = 0
    seen: list[str] = []
    for _ in range(max(0, int(limit))):
        async with tenant_tx(ctx) as conn:
            row = await _claim_next_report(conn, tenant_id, seen)
        if row is None:
            break
        seen.append(str(row["id"]))
        identifier = str(row["idempotency_key"])[:100]
        params = {
            "event_name": STRIPE_METER_EVENT_NAME,
            "payload": {"stripe_customer_id": customer_id, "value": str(row["quantity"])},
            "identifier": identifier,
            "timestamp": int(row["occurred_at"].timestamp()),
        }
        try:
            event = await asyncio.wait_for(asyncio.to_thread(functools.partial(
                stripe.billing.MeterEvent.create,
                **params,
                idempotency_key=_idempotency_key(identifier),
                max_network_retries=0,
            )), timeout=REPORT_SEND_TIMEOUT_SECONDS)
            event_id = getattr(event, "identifier", None) or identifier
        except _REFUSED as exc:
            if getattr(exc, "code", None) == _DUPLICATE_EVENT:
                # Stripe already holds this identifier: an earlier attempt landed.
                event_id = identifier
                logger.info("Usage %s was already on the Stripe meter", row["id"])
            else:
                # Refused, so not recorded: this attempt adds no uncertainty, but
                # an earlier uncertain attempt's stays (a refusal of a resend does
                # not prove the first send failed).
                await _release_report(ctx, row, _error_text(exc), row["prior_uncertain_since"])
                logger.warning("Usage report refused for %s: %s", row["id"], exc)
                unknown_streak = 0
                continue
        except Exception as exc:  # noqa: BLE001 — the outcome is unknown; recorded, drain continues
            # A timed-out send may still be running in its thread: the row
            # keeps its claim until the lease lapses.
            await _release_report(ctx, row, _error_text(exc), keep_uncertain=True,
                                  keep_claim=isinstance(exc, TimeoutError))
            if isinstance(exc, stripe.IdempotencyError):
                # The key was used with other parameters inside Stripe's 24 h
                # idempotency window: the brokerage's Stripe customer changed
                # between sends. Kept uncertain (the earlier send may have
                # landed under the old customer); named so a person sees why.
                logger.warning("Usage %s: Stripe customer changed between sends; held as uncertain", row["id"])
            else:
                logger.warning("Usage report outcome unknown for %s: %s", row["id"], exc)
            unknown_streak = unknown_streak + 1 if _transport_failure(exc) else 0
            if unknown_streak >= REPORT_MAX_CONSECUTIVE_UNKNOWN:
                logger.warning(
                    "ORACLE_ALERT usage_meter_stripe_unresponsive tenant=%s unknown_in_a_row=%d"
                    " (drain stopped; the rows stay uncertain and are resent inside Stripe's window)",
                    tenant_id, unknown_streak,
                )
                async with tenant_tx(ctx) as conn:
                    aging = await _aging_count(conn, tenant_id)
                return {"state": "stripe_unresponsive", "reported": reported, "considered": len(seen),
                        "quarantined": quarantined, "aging": aging}
            continue
        try:
            async with tenant_tx(ctx) as conn:
                # Tied to this attempt: if the lease lapsed and another drain
                # claimed the row, that drain's send (same identifier and
                # idempotency key) acknowledges it. A row quarantined meanwhile
                # was reported after all: the quarantine is lifted.
                acked = await conn.fetchval(
                    """
                    UPDATE billing_usage_events
                       SET reported_at=now(), stripe_event_id=$2, report_error=NULL,
                           report_claimed_until=NULL, report_state=NULL,
                           report_quarantined_at=NULL, report_resolution=NULL,
                           report_resolved_at=NULL, report_resolved_by=NULL,
                           report_resolution_note=NULL, updated_at=now()
                     WHERE id=$1::uuid AND tenant_id=$3::uuid AND report_attempts=$4
                       AND reported_at IS NULL
                    RETURNING id
                    """,
                    row["id"], event_id, ctx.tenant_id, row["attempt"],
                )
        except Exception as exc:  # noqa: BLE001
            # Stripe has it and we could not say so. The row keeps its uncertain
            # attempt; its lease lapses and the resend inside the window is
            # de-duplicated by Stripe, or a person reconciles it after.
            logger.warning("Usage %s reported but not acknowledged locally: %s", row["id"], exc)
            continue
        if acked is None:
            logger.info("Usage %s: a later claim took over before this report was acknowledged", row["id"])
            continue
        reported += 1
        unknown_streak = 0

    async with tenant_tx(ctx) as conn:
        aging = await _aging_count(conn, tenant_id)
    if aging:
        logger.warning(
            "ORACLE_ALERT usage_meter_aging tenant=%s rows=%d older_than_days=%d"
            " (Stripe refuses usage older than 35 days)",
            tenant_id, aging, REPORT_AGING_ALERT_DAYS,
        )
    return {"state": "ok", "reported": reported, "considered": len(seen),
            "quarantined": quarantined, "aging": aging}


async def _release_report(ctx: TenantContext, row: Any, error: str,
                          prior_uncertain_since: Any = None, *, keep_uncertain: bool = False,
                          keep_claim: bool = False) -> None:
    """End a claim without a report: stamp the error and free the row.

    keep_uncertain: the send may have landed, so report_uncertain_since stays
    set (COALESCE: never cleared here). Otherwise it goes back to what it was
    before this claim. Tied to this claim's attempt, so a send that outlived
    its lease cannot touch a later claim's row. keep_claim: the send may still
    be in flight (a timeout), so the lease is left to lapse instead of being
    cleared. Best effort: if this write fails too, the lease lapses.
    """
    try:
        async with tenant_tx(ctx) as conn:
            if keep_uncertain:
                await conn.execute(
                    "UPDATE billing_usage_events SET report_error=$2,"
                    " report_claimed_until=CASE WHEN $5::boolean THEN report_claimed_until END,"
                    " report_uncertain_since=COALESCE(report_uncertain_since, now()), updated_at=now()"
                    " WHERE id=$1::uuid AND tenant_id=$3::uuid AND report_attempts=$4"
                    " AND reported_at IS NULL",
                    row["id"], error, ctx.tenant_id, row["attempt"], keep_claim,
                )
            else:
                await conn.execute(
                    "UPDATE billing_usage_events SET report_error=$2, report_claimed_until=NULL,"
                    " report_uncertain_since=$5, updated_at=now()"
                    " WHERE id=$1::uuid AND tenant_id=$3::uuid AND report_attempts=$4"
                    " AND reported_at IS NULL AND report_state IS NULL",
                    row["id"], error, ctx.tenant_id, row["attempt"], prior_uncertain_since,
                )
    except Exception:  # noqa: BLE001
        logger.warning("Could not release usage report claim %s", row["id"], exc_info=True)


async def resolve_quarantined_usage(
    ctx: TenantContext, *, tenant_id: str, report_state: str, resolution: str,
    note: str, usage_ids: Optional[list[str]] = None,
) -> int:
    """Record a person's decision on quarantined usage (24.5, 24.6; 0175).

    'billed' (reported to Stripe by hand or found on the meter), 'credited' or
    'written_off'. The quarantine stays on the row as history; only rows not
    yet resolved change. Returns how many. Callers check the role: the
    operator route requires a platform admin and writes the audit chain.
    """
    if report_state not in ("expired", "uncertain"):
        raise ValueError("report_state must be 'expired' or 'uncertain'")
    if resolution not in REPORT_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {sorted(REPORT_RESOLUTIONS)}")
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            UPDATE billing_usage_events
               SET report_resolution=$3, report_resolved_at=now(), report_resolved_by=$4,
                   report_resolution_note=$5, updated_at=now()
             WHERE tenant_id=$1::uuid AND report_state=$2 AND report_resolution IS NULL
               AND ($6::uuid[] IS NULL OR id = ANY($6::uuid[]))
            RETURNING id
            """,
            tenant_id, report_state, resolution, str(ctx.agent_id)[:200], note[:500] or None,
            usage_ids,
        )
    return len(rows)


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
             WHERE """ + _IN_PLAY + """ AND metric = ANY($1::text[])
               AND """ + BILLABLE_PREDICATE + """
             GROUP BY tenant_id
             ORDER BY min(occurred_at) ASC
             LIMIT $2
            """,
            sorted(_STRIPE_REPORTED),
            max(1, min(5000, int(tenant_limit))),
        )

    reported = 0
    drained = 0
    aging = 0
    quarantined: dict[str, int] = {"expired": 0, "uncertain": 0}
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
        if state == "stripe_unresponsive":
            # Stripe is not answering: every other brokerage would time out too.
            reported += int(result.get("reported") or 0)
            for kind, n in (result.get("quarantined") or {}).items():
                quarantined[kind] = quarantined.get(kind, 0) + int(n or 0)
            aging += int(result.get("aging") or 0)
            drained += 1
            break
        reported += int(result.get("reported") or 0)
        for kind, n in (result.get("quarantined") or {}).items():
            quarantined[kind] = quarantined.get(kind, 0) + int(n or 0)
        aging += int(result.get("aging") or 0)
        drained += 1

    logger.info(
        "ORACLE_METRIC usage_meter_drain tenants=%d reported=%d expired=%d uncertain=%d aging=%d",
        drained, reported, quarantined["expired"], quarantined["uncertain"], aging,
    )
    return {
        "state": "ok",
        "tenants": drained,
        "tenants_pending": len(rows),
        "reported": reported,
        "by_state": states,
        "quarantined": quarantined,
        "aging": aging,
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
            -- Counts are of BILLABLE rows only (BILLABLE_PREDICATE): a
            -- lead_engaged row the pre-0133 code wrote at the staged step
            -- names no delivered event, so it is reported apart as
            -- not_billable_events rather than as consumption or as a bill
            -- waiting to go out.
            SELECT metric,
                   coalesce(sum(quantity) FILTER (WHERE """ + BILLABLE_PREDICATE + """), 0)::numeric
                       AS quantity,
                   count(*) FILTER (WHERE """ + BILLABLE_PREDICATE + """)::int AS events,
                   count(*) FILTER (WHERE reported_at IS NOT NULL
                                      AND """ + BILLABLE_PREDICATE + """)::int AS reported,
                   count(*) FILTER (WHERE NOT """ + BILLABLE_PREDICATE + """)::int AS not_billable,
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
                "not_billable_events": r["not_billable"],
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
