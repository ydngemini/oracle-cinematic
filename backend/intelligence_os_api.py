"""Read and write surface for the intelligence layer.

Grouped in one module because these routes are one product surface — the
Command Center reads the briefing, the relationship view reads beliefs and
intent, and both offer the same corrections back. Splitting them across four
files would hide that they share a contract.

Everything that infers is gated on PREDICTIVE_INTELLIGENCE. Perception capture
is deliberately NOT: recording what a client did is bookkeeping the tenant owns
regardless of whether they pay for inference over it, and gating capture would
mean a tenant who enables inference later has no history to reason about.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import agent_twin
import autonomy
import belief_store
import command_center
import intent_states
import living_state
import neoh_intents
from db.connection import tenant_tx
from platform_policy import Feature, require_feature
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.intelligence_os_api")

router = APIRouter(prefix="/api", tags=["intelligence-os"])


# ---------------------------------------------------------------------------
# Command Center
# ---------------------------------------------------------------------------

@router.get("/command-center")
async def command_center_briefing(
    lookback_hours: int = Query(24, ge=1, le=168),
    ctx: TenantContext = Depends(require_context),
):
    """The first screen: what changed, what needs attention, what is coming."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    return await command_center.briefing(ctx, lookback_hours=lookback_hours)


# ---------------------------------------------------------------------------
# Neoh — the universal input
# ---------------------------------------------------------------------------

class NeohAsk(BaseModel):
    """One thing typed into the ⌘K box."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=400)


@router.post("/neoh/ask")
async def neoh_ask(body: NeohAsk, ctx: TenantContext = Depends(require_context)):
    """Render an interface for the question, or say it could not.

    The answer is a list of `{primitive, props}` from a closed vocabulary the
    frontend knows how to draw — never markup, and never anything a model
    wrote. A question no pattern covers comes back with `fallthrough: true`,
    and the caller sends the same text down the existing chat channel instead,
    so the fixed vocabulary is a fast path in front of the general one rather
    than a limit on what can be asked.
    """
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    return await neoh_intents.ask(ctx, body.text)


# ---------------------------------------------------------------------------
# Perception — capture
# ---------------------------------------------------------------------------

class PerceptionEvent(BaseModel):
    """One thing a client did.

    `occurred_at` is optional and defaults to now, but is accepted so a batched
    or replayed capture keeps the real time. A behavioural signal timestamped
    with its ingest time rather than its occurrence would make every backfill
    look like a burst of activity this morning.
    """
    model_config = ConfigDict(extra="forbid")

    client_id: Optional[str] = None
    lead_id: Optional[str] = None
    interaction_type: Literal[
        "listing_view", "listing_favorite", "listing_unfavorite", "listing_share",
        "search", "saved_search", "calculator_use", "showing_request",
        "availability_view", "map_view", "email_open", "link_click",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: Optional[datetime] = None
    #: Who did it. Stated by the caller rather than assumed, because the answer
    #: decides whether the row counts as intent at all: intent_states reads only
    #: buyer/seller rows. An earlier version hardcoded 'buyer', which would have
    #: turned any agent-side emitter into a source of fake client intent.
    actor_role: Literal["buyer", "seller", "agent", "ai_system"] = "buyer"


class PerceptionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[PerceptionEvent] = Field(min_length=1, max_length=200)


@router.post("/perception/events", status_code=201)
async def capture_events(
    batch: PerceptionBatch, ctx: TenantContext = Depends(require_context),
):
    """Record client behaviour.

    First-party only. Every accepted type describes something a person did on a
    surface this brokerage operates — its portal, its listings, its emails.
    Nothing here should ever be fed from a third-party data broker: the value of
    this table is that the tenant can say exactly where each row came from, and
    one imported batch of purchased browsing data destroys that permanently.
    """
    inserted = 0
    async with tenant_tx(ctx) as conn:
        for event in batch.events:
            # 0012 requires an anchor. Enforced here too so the failure is a 422
            # naming the field rather than a constraint violation.
            if not event.client_id and not event.lead_id:
                raise HTTPException(
                    422, "each event needs a client_id or a lead_id to anchor to")
            await conn.execute(
                """
                INSERT INTO interaction_logs
                    (tenant_id, client_id, lead_id, actor_role, interaction_type,
                     payload, created_at)
                VALUES (app_current_tenant(), $1::uuid, $2::uuid, $3, $4,
                        $5::jsonb, COALESCE($6, now()))
                """,
                event.client_id, event.lead_id, event.actor_role,
                event.interaction_type,
                json.dumps(event.payload), event.occurred_at,
            )
            inserted += 1
    return {"captured": inserted}


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

@router.get("/clients/{client_id}/intent")
async def client_intent(client_id: str, ctx: TenantContext = Depends(require_context)):
    """Declared vs observed vs latent intent for one client."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    try:
        out = await intent_states.read_intent(ctx, client_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    # The person's living state rides along so the sheet needs one fetch.
    out["living"] = (await living_state.living_for(ctx, [client_id])).get(client_id)
    return out


@router.get("/living")
async def living(
    client_ids: str = Query(..., description="comma-separated client ids, max 100"),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Living state for a list of people — one call per screen, not per card."""
    ids = [c.strip() for c in client_ids.split(",") if c.strip()][:100]
    return {"living": await living_state.living_for(ctx, ids)}


# ---------------------------------------------------------------------------
# Beliefs
# ---------------------------------------------------------------------------

class BeliefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: Literal["client", "lead", "property", "household",
                          "transaction", "market", "agent"]
    subject_id: str
    predicate: str = Field(min_length=1, max_length=64)
    value: Any
    status: Literal["confirmed", "reported", "inference", "hypothesis"]
    confidence: float = Field(gt=0.0, lt=1.0)
    source_kind: Literal["sms", "call", "email", "form", "behaviour",
                         "public_record", "agent_entry", "model", "import"]
    source_ref: Optional[str] = None
    source_quote: Optional[str] = None
    learned_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class BeliefCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["retract", "pin", "unpin"]
    reason: Optional[str] = None


@router.get("/beliefs/{subject_type}/{subject_id}")
async def read_beliefs(
    subject_type: str, subject_id: str,
    include_history: bool = Query(False),
    ctx: TenantContext = Depends(require_context),
):
    """Everything held about one entity, with provenance and any disputes."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    return await belief_store.beliefs_about(
        ctx, subject_type, subject_id, include_history=include_history)


@router.post("/beliefs", status_code=201)
async def add_belief(body: BeliefInput, ctx: TenantContext = Depends(require_context)):
    """Record a claim. Returns anything it now contradicts."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    try:
        return await belief_store.assert_belief(
            ctx,
            subject_type=body.subject_type, subject_id=body.subject_id,
            predicate=body.predicate, value=body.value,
            status=body.status, confidence=body.confidence,
            source=belief_store.BeliefSource(
                kind=body.source_kind, ref=body.source_ref, quote=body.source_quote),
            learned_at=body.learned_at, valid_until=body.valid_until,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/beliefs/{belief_id}/correct")
async def correct(
    belief_id: str, body: BeliefCorrection,
    ctx: TenantContext = Depends(require_context),
):
    """Pin, unpin or retract. The agent's word outranks the model's."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    try:
        return await belief_store.correct_belief(
            ctx, belief_id, action=body.action, reason=body.reason)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------------------
# Agent Twin
# ---------------------------------------------------------------------------

class DecisionInput(BaseModel):
    """What the agent did about one recommendation.

    The opportunity is described rather than referenced by id, because the feed
    is computed per request and has no stable row to point at. Recording the
    shape of what was recommended is what makes the decision analysable later.
    """
    model_config = ConfigDict(extra="forbid")

    opportunity_kind: str = Field(min_length=1, max_length=64)
    subject_type: str = Field(min_length=1, max_length=32)
    subject_id: str = Field(min_length=1, max_length=200)
    recommended_action: str = Field(min_length=1, max_length=500)
    outcome: Literal["accepted", "overridden", "deferred", "dismissed"]
    recommended_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    recommended_rank: Optional[int] = Field(default=None, ge=1)
    chosen_action: Optional[str] = Field(default=None, max_length=500)
    rationale: Optional[str] = Field(default=None, max_length=1000)
    rationale_source: Optional[Literal["agent_typed", "agent_selected"]] = None


@router.post("/agent-twin/decisions", status_code=201)
async def record_decision(
    body: DecisionInput, ctx: TenantContext = Depends(require_context),
):
    """Record an accept / defer / dismiss against a recommendation.

    Not gated on PREDICTIVE_INTELLIGENCE. Recording what the agent decided is
    bookkeeping the tenant owns, and a tenant who switches inference on later
    should arrive with their own history rather than a blank slate.
    """
    try:
        return await agent_twin.record_decision(
            ctx,
            opportunity_kind=body.opportunity_kind,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            recommended_action=body.recommended_action,
            outcome=body.outcome,
            recommended_confidence=body.recommended_confidence,
            recommended_rank=body.recommended_rank,
            chosen_action=body.chosen_action,
            rationale=body.rationale,
            rationale_source=body.rationale_source,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class RationaleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rationale: str = Field(min_length=1, max_length=1000)
    rationale_source: Literal["agent_typed", "agent_selected"]


@router.post("/agent-twin/decisions/{decision_id}/rationale")
async def attach_rationale(
    decision_id: str, body: RationaleInput,
    ctx: TenantContext = Depends(require_context),
):
    """Attach a reason to a decision already recorded.

    Separate from the insert because the two arrive as separate interactions —
    the agent dismisses, then is asked why — but they are one decision. Posting
    a second decision here would double-count it.
    """
    try:
        return await agent_twin.attach_rationale(
            ctx, decision_id,
            rationale=body.rationale, rationale_source=body.rationale_source,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@router.get("/agent-twin")
async def agent_twin_policy(ctx: TenantContext = Depends(require_context)):
    """What the recorded decisions say about how this agent works."""
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    return await agent_twin.policy(ctx)


@router.get("/agent-twin/reasons")
async def decision_reasons():
    """The one-tap reasons offered alongside free text."""
    return {"reasons": [{"code": k, "label": v} for k, v in agent_twin.COMMON_REASONS.items()]}


# ---------------------------------------------------------------------------
# Autonomy
# ---------------------------------------------------------------------------

class AutonomyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    level: Literal["observe", "assist", "autopilot"]


@router.get("/autonomy")
async def autonomy_settings(ctx: TenantContext = Depends(require_context)):
    """Current levels, permitted levels, and why the ceilings exist."""
    return await autonomy.get_settings(ctx)


@router.put("/autonomy")
async def set_autonomy(body: AutonomyInput, ctx: TenantContext = Depends(require_context)):
    """Move one dial. 403 with an explanation if it is above the ceiling."""
    try:
        return await autonomy.set_level(ctx, body.category, body.level)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
