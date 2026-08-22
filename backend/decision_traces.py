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
from typing import Any, Mapping, Optional

from automation_jobs import canonical_json, payload_hash
from db.connection import tenant_tx
from tenancy import TenantContext

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
                ctx.agent_id,
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
