"""Outcome Memory — what happened afterwards, and which decision it rewards.

Every module in the intelligence layer reports preferences and predictions.
`beliefs` says what is claimed, `intent_states` what is likely, `expected_value`
what an hour is worth, `agent_twin` which cards the agent takes. None of them
knows whether anything WORKED, and each says so in its own caveat. This is the
missing half: `action → outcome → learning`.

Two operations, on two clocks.

**Recording** happens at the moment a state change is an outcome — a reply
lands, a showing resolves, a deal closes or dies. It is called from inside the
transaction that made the change, and it must never be able to roll that
change back: a deal that closed has closed whether or not we managed to note
it. So `record_outcome` follows `billing_usage.record_usage` exactly — never
raises, `ON CONFLICT DO NOTHING`, and a SAVEPOINT when handed the caller's
connection.

**Attribution** happens later, on a sweep. A reply arrives days after the text
that earned it; a closing arrives months after the card that surfaced the
lead. The join is deliberately simple — the LAST accepted decision or approved
command on the same subject inside a per-kind window — because there is no
data yet to fit anything smarter, and a multi-touch model with invented decay
weights would be a guess wearing a formula.

The most important row this module writes is the one where attribution found
NOTHING. It is written with `attributed_at` set and both ids NULL. That row is
the base rate: "of forty showings, thirty-one followed nothing we did". Without
it, "unattributed" and "not yet examined" are the same value, and every
interval the twin computes has a numerator and no denominator.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import decision_traces
from db.connection import tenant_tx
from tenancy import Role, TenantContext

logger = logging.getLogger("oracle.outcome_memory")

#: Mirrors the CHECK in 0098. A kind outside this set is refused before the
#: database gets to refuse it, with a sentence rather than a constraint name.
OUTCOME_KINDS: frozenset[str] = frozenset({
    "reply_received",
    "appointment_booked",
    "showing_held",
    "no_show",
    "offer_made",
    "transaction_closed",
    "transaction_lost",
    "contact_suppressed",
})

#: Mirrors the generated column. Kept here so the write-back to agent_decisions
#: derives valence the same way the table does, and neither can be told a loss
#: is a win.
NEGATIVE_KINDS: frozenset[str] = frozenset({
    "no_show", "transaction_lost", "contact_suppressed",
})

SUBJECT_TYPES: frozenset[str] = frozenset({"client", "lead", "transaction", "contact"})

#: Names the rule that produced an attribution, stored on every row it touches,
#: so a later model can re-run over the same facts and be told apart.
ATTRIBUTION_MODEL = "last_touch_v1"

#: How far back an outcome looks for the decision that earned it. Stated, not
#: fitted: these are judgement calls about how long each kind of result takes
#: to arrive, and they are the first thing to revisit once outcomes exist in
#: volume. Too long and an organic closing credits a card from last quarter;
#: too short and a slow deal credits nothing.
ATTRIBUTION_WINDOWS: dict[str, timedelta] = {
    "reply_received":     timedelta(days=14),
    "appointment_booked": timedelta(days=14),
    "showing_held":       timedelta(days=21),
    "no_show":            timedelta(days=21),
    "offer_made":         timedelta(days=45),
    "transaction_closed": timedelta(days=90),
    "transaction_lost":   timedelta(days=90),
    "contact_suppressed": timedelta(days=7),
}


def valence_of(outcome_kind: str) -> int:
    return -1 if outcome_kind in NEGATIVE_KINDS else 1


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

_INSERT_SQL = """
    INSERT INTO outcome_events (
        tenant_id, outcome_kind, subject_type, subject_id, client_id,
        outcome_value, occurred_at, source_table, source_id, detail
    ) VALUES (
        $1::uuid, $2, $3, $4, $5::uuid, $6, $7, $8, $9::uuid, $10::jsonb
    )
    ON CONFLICT (tenant_id, source_table, source_id, outcome_kind) DO NOTHING
    RETURNING id
"""


async def _resolve_client_id(conn: Any, subject_type: str, subject_id: str) -> Optional[str]:
    """The person behind a subject, when one can be found.

    NULL is returned rather than guessed. A consumer that groups outcomes by
    person must treat NULL as a coverage gap, not as a bucket — 0096 learned
    that an unattributable row is worse than a missing one only when it is
    silently counted somewhere it does not belong.
    """
    try:
        if subject_type == "client":
            return subject_id
        if subject_type == "lead":
            row = await conn.fetchval(
                "SELECT seller_client_id FROM leads WHERE id = $1::uuid", subject_id)
            return str(row) if row else None
        if subject_type == "transaction":
            row = await conn.fetchval(
                "SELECT client_id FROM transactions WHERE id = $1::uuid", subject_id)
            return str(row) if row else None
    except Exception:  # noqa: BLE001 — resolution is best-effort by design
        logger.debug("client resolution failed for %s/%s", subject_type, subject_id, exc_info=True)
    return None


async def record_outcome(
    ctx: TenantContext,
    *,
    outcome_kind: str,
    subject_type: str,
    subject_id: str,
    source_table: str,
    source_id: str,
    occurred_at: datetime,
    client_id: Optional[str] = None,
    outcome_value: Optional[float] = None,
    detail: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> Optional[str]:
    """Record that something happened. Returns the new row id, or None.

    None means "already recorded" or "could not record" — and the caller is not
    told which, because the caller must not care. Every call site is a state
    change whose success is independent of whether we noted it, and the
    distinction is logged rather than raised.

    When ``conn`` is the caller's in-flight transaction, the insert is wrapped
    in a SAVEPOINT: a failed statement would otherwise abort the caller's whole
    transaction, and the close that produced this outcome would fail to commit
    because its bookkeeping did.
    """
    if outcome_kind not in OUTCOME_KINDS:
        logger.warning("Refusing to record unknown outcome kind %r", outcome_kind)
        return None
    if subject_type not in SUBJECT_TYPES:
        logger.warning("Refusing to record outcome for unknown subject type %r", subject_type)
        return None
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    import json
    encoded_detail = json.dumps(detail or {})

    async def _write(c: Any) -> Optional[str]:
        resolved = client_id or await _resolve_client_id(c, subject_type, subject_id)
        row = await c.fetchrow(
            _INSERT_SQL,
            ctx.tenant_id, outcome_kind, subject_type, str(subject_id), resolved,
            outcome_value, occurred_at, source_table, str(source_id), encoded_detail,
        )
        return str(row["id"]) if row else None

    try:
        if conn is not None:
            async with conn.transaction():
                return await _write(conn)
        async with tenant_tx(ctx) as c:
            return await _write(c)
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning(
            "Outcome not recorded: kind=%s %s/%s from %s/%s",
            outcome_kind, subject_type, subject_id, source_table, source_id,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

#: Which command_executions.target key names a subject of each type. The
#: target payload is what stage_command received; it carries the ids the tool
#: resolved, not the ones the model typed.
_TARGET_KEY = {
    "client": "client_id",
    "lead": "lead_id",
    "contact": "contact_id",
}


async def _last_accepted_decision(conn: Any, subject_type: str, subject_id: str,
                                  lo: datetime, hi: datetime) -> Optional[dict[str, Any]]:
    """The most recent card the agent said yes to, on this subject, in window.

    No tenant predicate: agent_decisions is FORCE RLS and this runs under the
    tenant's own context. agent_twin.policy() keeps a per-agent user_id filter
    because a twin is one person's habits; attribution is tenant-wide and must
    not carry it — a reply earned by a colleague's call is still a reply.
    """
    return await conn.fetchrow(
        """
        SELECT id, decided_at
          FROM agent_decisions
         WHERE subject_type = $1 AND subject_id = $2
           AND outcome = 'accepted'
           AND decided_at BETWEEN $3 AND $4
         ORDER BY decided_at DESC
         LIMIT 1
        """,
        subject_type, str(subject_id), lo, hi,
    )


async def _last_approved_command(conn: Any, subject_type: str, subject_id: str,
                                 client_id: Optional[str],
                                 lo: datetime, hi: datetime) -> Optional[dict[str, Any]]:
    """The most recent human-approved command that reached this subject.

    ai_decision_traces keys on action_approvals.id, so the path to the person
    runs through the command the approval released: trace → approval →
    command_executions.target. Only 'succeeded' commands count — a message
    that was approved and then bounced earned nothing.
    """
    keys: list[tuple[str, str]] = []
    if subject_type in _TARGET_KEY:
        keys.append((_TARGET_KEY[subject_type], str(subject_id)))
    if client_id and subject_type != "client":
        keys.append(("client_id", str(client_id)))
    if not keys:
        return None

    # One OR per candidate key. Bounded to at most two, and the trace side is
    # narrowed by decided_at first, so this is not the unbounded shape 0086
    # exists to prevent.
    clauses = " OR ".join(f"c.target->>'{k}' = ${i + 3}" for i, (k, _) in enumerate(keys))
    return await conn.fetchrow(
        f"""
        SELECT t.id AS trace_id, t.source_id AS approval_id, t.decided_at
          FROM ai_decision_traces t
          JOIN command_executions c ON c.approval_id = t.source_id
         WHERE t.source_table = 'action_approvals'
           AND t.revoked_at IS NULL
           AND c.state = 'succeeded'
           AND t.decided_at BETWEEN $1 AND $2
           AND ({clauses})
         ORDER BY t.decided_at DESC
         LIMIT 1
        """,
        lo, hi, *[v for _, v in keys],
    )


async def _attribute_one(ctx: TenantContext, conn: Any, row: Any) -> dict[str, Any]:
    """Bind one outcome to the last thing Neoh did that could have earned it.

    Last touch, not fan-out. Crediting every decision in the window would count
    one closing five times, and there is no data to fit a decay curve that
    would share it honestly. When both a decision and a command qualify, the
    later one wins — it is the closer cause.
    """
    kind = row["outcome_kind"]
    window = ATTRIBUTION_WINDOWS.get(kind, timedelta(days=30))
    hi = row["occurred_at"]
    lo = hi - window
    subject_type, subject_id = row["subject_type"], row["subject_id"]
    client_id = str(row["client_id"]) if row["client_id"] else None

    decision = await _last_accepted_decision(conn, subject_type, subject_id, lo, hi)
    # A lead outcome can also be reached through its person, and a client
    # outcome through no other door — so the person is tried second, not first.
    if decision is None and client_id and subject_type != "client":
        decision = await _last_accepted_decision(conn, "client", client_id, lo, hi)

    command = await _last_approved_command(conn, subject_type, subject_id, client_id, lo, hi)

    trace_id: Optional[str] = None
    decision_id: Optional[str] = None
    if decision and command:
        if command["decided_at"] >= decision["decided_at"]:
            trace_id = str(command["trace_id"])
        else:
            decision_id = str(decision["id"])
    elif decision:
        decision_id = str(decision["id"])
    elif command:
        trace_id = str(command["trace_id"])

    # Write back to the thing that was rewarded. Each is enrich-in-place with
    # an IS NULL guard, so a second sweep over the same row is a no-op rather
    # than a rewrite.
    if decision_id:
        await conn.execute(
            """
            UPDATE agent_decisions
               SET result_kind = $2, result_valence = $3, result_value = $4,
                   result_at = $5, result_source = $6
             WHERE id = $1::uuid AND result_kind IS NULL
            """,
            decision_id, kind, valence_of(kind), row["outcome_value"],
            row["occurred_at"], f"outcome_memory:{row['source_table']}",
        )
    if trace_id and command:
        await decision_traces.attach_outcome(
            ctx,
            source_table="action_approvals",
            source_id=str(command["approval_id"]),
            outcome_kind=kind,
            outcome_at=row["occurred_at"],
            outcome_source=f"outcome_memory:{row['source_table']}",
            outcome_value=float(row["outcome_value"]) if row["outcome_value"] is not None else None,
        )

    # ALWAYS mark examined — including when nothing matched. That row is the
    # base rate, and it is the reason this table exists.
    await conn.execute(
        """
        UPDATE outcome_events
           SET attributed_at = now(), attribution_model = $2,
               attributed_trace_id = $3::uuid, attributed_decision_id = $4::uuid
         WHERE id = $1::uuid AND attributed_at IS NULL
        """,
        str(row["id"]), ATTRIBUTION_MODEL, trace_id, decision_id,
    )
    return {
        "id": str(row["id"]),
        "kind": kind,
        "attributed_to": "trace" if trace_id else "decision" if decision_id else None,
    }


async def attribute_pending(ctx: TenantContext, *, limit: int = 200) -> dict[str, Any]:
    """Examine every outcome in this tenant that has not yet been examined."""
    limit = max(1, min(1000, int(limit)))
    examined = 0
    credited = 0
    organic = 0
    failed = 0

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, outcome_kind, subject_type, subject_id, client_id,
                   outcome_value, occurred_at, source_table
              FROM outcome_events
             WHERE attributed_at IS NULL
             ORDER BY occurred_at ASC
             LIMIT $1
            """,
            limit,
        )
        for row in rows:
            try:
                result = await _attribute_one(ctx, conn, row)
            except Exception:  # noqa: BLE001 — one bad row must not stall the sweep
                failed += 1
                logger.exception("attribution failed for outcome %s", row["id"])
                continue
            examined += 1
            if result["attributed_to"]:
                credited += 1
            else:
                organic += 1

    return {
        "examined": examined,
        "credited": credited,
        "organic": organic,
        "failed": failed,
        "model": ATTRIBUTION_MODEL,
    }


async def base_rates(ctx: TenantContext, *, since: datetime) -> dict[str, Any]:
    """The denominator: per kind, how many outcomes followed something Neoh did.

    Only examined rows count. An unexamined row is not "organic" — it is
    "unknown", and mixing the two would make the base rate drift upward every
    time the sweep fell behind.
    """
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT outcome_kind,
                   count(*)::int AS total,
                   count(*) FILTER (
                       WHERE attributed_trace_id IS NOT NULL
                          OR attributed_decision_id IS NOT NULL
                   )::int AS credited,
                   count(*) FILTER (WHERE attributed_at IS NULL)::int AS unexamined
              FROM outcome_events
             WHERE occurred_at >= $1
             GROUP BY outcome_kind
            """,
            since,
        )
    out: dict[str, Any] = {}
    for r in rows:
        examined = r["total"] - r["unexamined"]
        out[r["outcome_kind"]] = {
            "total": r["total"],
            "examined": examined,
            "credited": r["credited"],
            "organic": max(0, examined - r["credited"]),
            "unexamined": r["unexamined"],
        }
    return {"since": since.isoformat(), "by_kind": out, "model": ATTRIBUTION_MODEL}


# ---------------------------------------------------------------------------
# Sweep — scheduler entry point
# ---------------------------------------------------------------------------

async def sweep_all_tenants(*, tenant_limit: int = 500, per_tenant_limit: int = 200) -> dict[str, Any]:
    """Attribute pending outcomes across every tenant. Scheduler entry point.

    Same posture as billing_usage.drain_all_tenants: one cross-tenant read to
    find who has work, then a fresh single-tenant context for each, so the
    attribution queries above never run as an admin and never need a tenant
    predicate of their own.
    """
    platform_ctx = TenantContext(
        agent_id="outcome-attribution",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT tenant_id, count(*)::int AS pending
              FROM outcome_events
             WHERE attributed_at IS NULL
             GROUP BY tenant_id
             ORDER BY min(occurred_at) ASC
             LIMIT $1
            """,
            max(1, min(5000, int(tenant_limit))),
        )

    totals = {"tenants": 0, "examined": 0, "credited": 0, "organic": 0, "failed": 0}
    for row in rows:
        tenant_ctx = TenantContext(
            agent_id="outcome-attribution",
            tenant_id=str(row["tenant_id"]),
            role=Role.BROKER_OWNER,
        )
        try:
            result = await attribute_pending(tenant_ctx, limit=per_tenant_limit)
        except Exception:  # noqa: BLE001 — one tenant must not stall the rest
            logger.exception("attribution sweep failed for tenant %s", row["tenant_id"])
            totals["failed"] += 1
            continue
        totals["tenants"] += 1
        for key in ("examined", "credited", "organic", "failed"):
            totals[key] += result[key]
    totals["model"] = ATTRIBUTION_MODEL
    return totals
