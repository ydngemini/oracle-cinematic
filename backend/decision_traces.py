"""Capture of human judgement on machine proposals.

Every approval decision is a labelled training example that is currently thrown
away. This module records them into `ai_decision_traces` so a preference and
reward corpus accumulates from ordinary use, because none of it can be
backfilled — an approval decided today and not captured is gone.

Two rules shape everything here.

**Capture is subordinate to the action.** A failure to record a trace must never
fail the decision that produced it. Refusing to approve an outreach message
because a training-corpus insert hit a constraint would be a far worse outcome
than losing one example. Failures are logged and swallowed; `record_decision`
returns None rather than raising.

**The signal is derived, never supplied.** Callers pass what happened (the draft,
and the edited payload if the human changed one); this module decides whether
that is `accepted_unchanged`, `edited` or `rejected`. Letting callers label their
own examples is how a corpus silently acquires whatever labels were convenient
at each call site.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

from automation_jobs import canonical_json, payload_hash
from db.connection import tenant_tx
from tenancy import Role, TenantContext

logger = logging.getLogger(__name__)

# Surfaces that produce a decidable proposal. Kept in step with the CHECK
# constraint in migration 0074 — a value absent there fails at insert.
SURFACE_APPROVAL = "approval"
SURFACE_CHAT_ACTION = "chat_action"
SURFACE_STAGE_OVERRIDE = "stage_override"

SIGNAL_ACCEPTED = "accepted_unchanged"
SIGNAL_EDITED = "edited"
SIGNAL_REJECTED = "rejected"
SIGNAL_EXPIRED = "expired"

# Decisions that carry no learning signal at all. An approval that expired tells
# us the human never engaged, which is not evidence the draft was wrong — it is
# recorded so the corpus can report its own coverage honestly, but it must not
# be exported as a negative.
NON_EVIDENTIAL_SIGNALS = frozenset({SIGNAL_EXPIRED})


def derive_signal(
    *,
    decision: str,
    proposal_digest: str,
    final_digest: Optional[str],
) -> str:
    """Classify a decision from what actually changed.

    An "approved" decision whose edited payload hashes to the proposal is an
    acceptance, not an edit — a UI that round-trips an unmodified draft through
    an edit box must not manufacture a preference pair out of nothing.
    """
    if decision == "rejected":
        return SIGNAL_REJECTED
    if decision == "expired":
        return SIGNAL_EXPIRED
    if final_digest is not None and final_digest != proposal_digest:
        return SIGNAL_EDITED
    return SIGNAL_ACCEPTED


async def record_decision(
    ctx: TenantContext,
    *,
    surface: str,
    action_type: str,
    source_table: str,
    source_id: str,
    proposal: Mapping[str, Any],
    decision: str,
    decided_at: Any,
    risk_class: Optional[str] = None,
    model_version: Optional[str] = None,
    final: Optional[Mapping[str, Any]] = None,
    decision_latency_ms: Optional[int] = None,
    consent_version: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Optional[str]:
    """Record one decided proposal. Returns the trace id, or None on failure.

    Never raises: see the module docstring. The caller's action has already
    happened by the time this runs, and a capture problem must not undo it.
    """
    try:
        draft = dict(proposal)
        proposal_digest = payload_hash(draft)

        final_payload: Optional[dict[str, Any]] = None
        final_digest: Optional[str] = None
        if final is not None:
            candidate = dict(final)
            candidate_digest = payload_hash(candidate)
            # Only keep the final payload when it genuinely differs; the CHECK
            # constraint refuses an 'edited' row whose digests match, and
            # storing an identical copy would double the corpus for nothing.
            if candidate_digest != proposal_digest:
                final_payload = candidate
                final_digest = candidate_digest

        signal = derive_signal(
            decision=decision,
            proposal_digest=proposal_digest,
            final_digest=final_digest,
        )

        # A rejected or expired draft has no "what they wanted instead", so any
        # final payload collected alongside it is dropped rather than stored
        # against a signal the constraint forbids.
        if signal in {SIGNAL_REJECTED, SIGNAL_EXPIRED}:
            final_payload = None
            final_digest = None

        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ai_decision_traces (
                    tenant_id, agent_id, surface, action_type, risk_class,
                    model_version, source_table, source_id,
                    proposal, proposal_sha256, final, final_sha256,
                    signal, decided_at, decision_latency_ms, consent_version
                ) VALUES (
                    $1::uuid,$2,$3,$4,$5,
                    $6,$7,$8::uuid,
                    $9::jsonb,$10,$11::jsonb,$12,
                    $13,$14,$15,$16
                )
                ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                RETURNING id
                """,
                ctx.tenant_id,
                agent_id or ctx.agent_id,
                surface,
                action_type[:120],
                risk_class,
                model_version,
                source_table[:120],
                str(source_id),
                canonical_json(draft),
                proposal_digest,
                canonical_json(final_payload) if final_payload is not None else None,
                final_digest,
                signal,
                decided_at,
                decision_latency_ms,
                consent_version,
            )
        # DO NOTHING returns no row when the trace already exists, which is the
        # idempotent case and not an error.
        return str(row["id"]) if row else None
    except Exception:
        logger.exception(
            "decision trace capture failed for %s/%s; the decision itself stands",
            source_table,
            source_id,
        )
        return None


async def attach_outcome(
    ctx: TenantContext,
    *,
    source_table: str,
    source_id: str,
    outcome_kind: str,
    outcome_at: Any,
    outcome_source: str,
    outcome_value: Optional[float] = None,
) -> bool:
    """Bind a reward to a trace recorded earlier.

    Separate from `record_decision` because reward arrives on a different clock:
    an offer is accepted weeks after it is drafted, a deal closes months after.
    The trigger in 0074 permits exactly these columns to change, so this cannot
    quietly rewrite the decision it is scoring.
    """
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                UPDATE ai_decision_traces
                   SET outcome_kind=$3, outcome_value=$4,
                       outcome_at=$5, outcome_source=$6
                 WHERE tenant_id=$1::uuid
                   AND source_table=$2
                   AND source_id=$7::uuid
                   AND outcome_kind IS NULL
                RETURNING id
                """,
                ctx.tenant_id,
                source_table,
                outcome_kind,
                outcome_value,
                outcome_at,
                outcome_source,
                str(source_id),
            )
        return row is not None
    except Exception:
        logger.exception(
            "outcome attach failed for %s/%s", source_table, source_id
        )
        return False


async def _settled_chat_actions_for_tenant(
    ctx: TenantContext, *, limit: int
) -> dict[str, int]:
    """Record one trace per chat action whose undo window has closed.

    A chat tool applies its change immediately and the only human verdict is
    the Undo button, so the signal arrives on a delay: an action still inside
    its 24h undo window is undecided. Once the window closes, `status='applied'`
    is an acceptance and `status='undone'` is a rejection. `status='conflict'`
    is neither — the record moved under the undo — so it is left alone.

    Idempotent: `record_decision` upserts on (tenant, source_table, source_id)
    and does nothing on a repeat, so re-running this is free.
    """
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.user_id, a.action_type, a.record_type, a.record_id,
                   a.status, a.undone_at, a.undo_expires_at
              FROM ai_chat_actions a
             WHERE a.status IN ('applied', 'undone')
               AND a.undo_expires_at < now()
               AND NOT EXISTS (
                   SELECT 1 FROM ai_decision_traces t
                    WHERE t.tenant_id = a.tenant_id
                      AND t.source_table = 'ai_chat_actions'
                      AND t.source_id = a.id
               )
             ORDER BY a.undo_expires_at ASC
             LIMIT $1
            """,
            max(1, min(2000, limit)),
        )

    counts = {"examined": 0, "accepted": 0, "rejected": 0}
    for row in rows:
        counts["examined"] += 1
        decision = "rejected" if row["status"] == "undone" else "accepted"
        trace_id = await record_decision(
            ctx,
            agent_id=str(row["user_id"] or ctx.agent_id),
            surface=SURFACE_CHAT_ACTION,
            action_type=str(row["action_type"] or "chat_tool"),
            source_table="ai_chat_actions",
            source_id=str(row["id"]),
            proposal={
                "tool": row["action_type"],
                "record_type": row["record_type"],
                "record_id": str(row["record_id"]),
            },
            decision=decision,
            decided_at=row["undone_at"] or row["undo_expires_at"],
        )
        if trace_id is not None:
            counts[decision] += 1
    return counts


async def sweep_settled_chat_actions(
    *, tenant_limit: int = 500, per_tenant_limit: int = 500
) -> dict[str, Any]:
    """Scheduler entry point: capture chat-action verdicts across every tenant.

    Same posture as outcome_memory.sweep_all_tenants — one cross-tenant read to
    find who has settled-but-uncaptured actions, then a fresh single-tenant
    context per tenant so the writes never run as an admin.
    """
    platform_ctx = TenantContext(
        agent_id="chat-decision-sweep",
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT a.tenant_id, count(*)::int AS pending
              FROM ai_chat_actions a
             WHERE a.status IN ('applied', 'undone')
               AND a.undo_expires_at < now()
               AND NOT EXISTS (
                   SELECT 1 FROM ai_decision_traces t
                    WHERE t.tenant_id = a.tenant_id
                      AND t.source_table = 'ai_chat_actions'
                      AND t.source_id = a.id
               )
             GROUP BY a.tenant_id
             ORDER BY min(a.undo_expires_at) ASC
             LIMIT $1
            """,
            max(1, min(5000, int(tenant_limit))),
        )

    totals = {"tenants": 0, "examined": 0, "accepted": 0, "rejected": 0, "failed": 0}
    for row in rows:
        tenant_ctx = TenantContext(
            agent_id="chat-decision-sweep",
            tenant_id=str(row["tenant_id"]),
            role=Role.BROKER_OWNER,
        )
        try:
            result = await _settled_chat_actions_for_tenant(
                tenant_ctx, limit=per_tenant_limit
            )
        except Exception:  # noqa: BLE001 — one tenant must not stall the rest
            logger.exception("chat-decision sweep failed for tenant %s", row["tenant_id"])
            totals["failed"] += 1
            continue
        totals["tenants"] += 1
        for key in ("examined", "accepted", "rejected"):
            totals[key] += result[key]
    return totals


async def revoke_traces_for_agent(ctx: TenantContext, agent_id: str) -> int:
    """Withdraw an agent's traces from future dataset builds.

    Mirrors `style_training_examples.revoked_at`: the row survives for audit,
    but dataset assembly filters on `revoked_at IS NULL`. Deleting instead would
    make an already-trained model's provenance unauditable.
    """
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            UPDATE ai_decision_traces
               SET revoked_at=now()
             WHERE tenant_id=$1::uuid AND agent_id=$2 AND revoked_at IS NULL
            RETURNING id
            """,
            ctx.tenant_id,
            agent_id,
        )
    return len(rows)
