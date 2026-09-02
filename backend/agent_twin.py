"""The Agent Twin — learning how this agent decides, not how they write.

Competitors are converging on "learns your playbook", and the shallow reading of
that is style transfer: sound like Nathan. Style is the easy half and the
worthless half. The valuable half is *policy* — which recommendations this agent
takes, which they skip, and why — because that is the thing a general model
cannot know and cannot guess.

The raw material is a decision the product currently throws away. An agent who
disagrees with the Intelligence Feed's top card today just… does not click it.
That leaves no trace, so the system cannot tell "wrong suggestion" from "right
suggestion, busy afternoon", and it never improves.

WHAT MAKES THIS HONEST RATHER THAN ASTROLOGY.

Small samples lie loudly. Three accepted contract-deadline cards is not "you
always act on deadlines" — it is three. Every rate reported here is a Wilson
score interval, never a raw proportion, so 3/3 comes back as "44%–100%, n=3"
instead of "100%". That single choice is the difference between a twin that
earns trust and one that makes a confident claim the agent can immediately
falsify from memory.

Below `MIN_DECISIONS_PER_KIND` nothing is reported for that kind at all, and
below `MIN_DECISIONS_FOR_POLICY` the twin declines to describe the agent
entirely and says how many more decisions it needs. An agent being told "I have
watched you 6 times, that is not enough to have an opinion" trusts the opinion
that arrives at 40 far more than one asserted at 6.

WHAT IS NOT DONE HERE. Nothing in this module feeds a model prompt or changes a
ranking yet. It observes and reports. Closing that loop needs outcomes — whether
the accepted action actually led anywhere — and no outcome has been recorded in
this deployment, so weighting the engine by a policy fitted to unvalidated
preferences would encode the agent's habits as if they were results.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import TenantContext

logger = logging.getLogger("oracle.agent_twin")

#: Below this, the twin has no opinion about the agent at all.
MIN_DECISIONS_FOR_POLICY = 20

#: Below this, a per-kind rate is withheld. Eight is not statistically generous;
#: it is the point at which a Wilson interval stops spanning most of the range,
#: which is what makes the number worth printing.
MIN_DECISIONS_PER_KIND = 8

#: 95% two-sided.
_Z = 1.959963985

#: What the agent did. 'dismissed' and 'deferred' are kept apart on purpose:
#: "not this" and "not now" are different judgements, and collapsing them would
#: teach the twin that a busy Tuesday means a bad recommendation.
OUTCOMES = ("accepted", "overridden", "deferred", "dismissed")

#: Reasons offered as one tap. Free text is always allowed and is worth more,
#: but an interface that demands prose for every dismissal gets abandoned in a
#: week — and the resulting data is worse than none, because it is uniformly
#: whatever the fastest option was.
COMMON_REASONS = {
    "not_ready": "They are not ready yet",
    "wrong_priority": "Something else matters more",
    "already_handled": "Already handled outside Neoh",
    "bad_read": "Neoh has this wrong",
    "no_capacity": "No time today",
}


def wilson_interval(successes: int, total: int) -> tuple[float, float, float]:
    """(point, low, high) for a proportion, Wilson score, 95%.

    Wilson rather than the textbook normal interval because the normal one is
    degenerate exactly where this module lives: at 3/3 it returns [1.0, 1.0],
    asserting certainty from three observations. Wilson returns [0.44, 1.0],
    which is the truth.
    """
    if total <= 0:
        return (0.0, 0.0, 1.0)
    p = successes / total
    denominator = 1 + _Z**2 / total
    centre = (p + _Z**2 / (2 * total)) / denominator
    margin = (_Z / denominator) * math.sqrt(
        p * (1 - p) / total + _Z**2 / (4 * total**2)
    )
    return (
        round(p, 3),
        round(max(0.0, centre - margin), 3),
        round(min(1.0, centre + margin), 3),
    )


async def record_decision(
    ctx: TenantContext,
    *,
    opportunity_kind: str,
    subject_type: str,
    subject_id: str,
    recommended_action: str,
    outcome: str,
    recommended_confidence: Optional[float] = None,
    recommended_rank: Optional[int] = None,
    chosen_action: Optional[str] = None,
    rationale: Optional[str] = None,
    rationale_source: Optional[str] = None,
) -> dict[str, Any]:
    """Record what the agent did about one recommendation.

    `recommended_confidence` is stored rather than looked up later, because
    scoring a decision against today's confidence when it was made under last
    month's would measure the wrong model.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    if rationale and not rationale_source:
        # The DB CHECK enforces this too. Raising here turns a constraint
        # violation into a sentence a caller can act on.
        raise ValueError("a rationale must say where it came from")

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_decisions (
                tenant_id, user_id, opportunity_kind, subject_type, subject_id,
                recommended_action, recommended_confidence, recommended_rank,
                outcome, chosen_action, rationale, rationale_source
            ) VALUES (
                app_current_tenant(), app_current_agent(), $1, $2, $3,
                $4, $5, $6, $7, $8, $9, $10
            ) RETURNING id, decided_at
            """,
            opportunity_kind, subject_type, subject_id,
            recommended_action, recommended_confidence, recommended_rank,
            outcome, chosen_action, rationale, rationale_source,
        )
    return {"id": str(row["id"]), "decided_at": row["decided_at"].isoformat()}


async def attach_rationale(
    ctx: TenantContext, decision_id: str, *, rationale: str, rationale_source: str,
) -> dict[str, Any]:
    """Attach a reason to a decision that is already recorded.

    The decision and its reason arrive as two separate interactions — the agent
    dismisses a card, and only then is asked why — but they are ONE decision and
    must be ONE row. Inserting again on the reason click double-counted every
    reasoned dismissal, so a kind the agent explained their way out of scored as
    twice as disliked as one they skipped silently. Caught in the browser, not
    by a test: both inserts succeeded and both looked correct in isolation.

    Recording happens first and the reason is optional precisely because the
    agent may never answer — closing the tab must not lose the decision.

    Refuses to overwrite. A rationale is a thing the agent said once; letting a
    later click replace it would quietly rewrite history in the one table whose
    entire value is being an honest record of what they chose.
    """
    if not rationale or not rationale.strip():
        raise ValueError("a rationale must have content")
    if rationale_source not in ("agent_typed", "agent_selected"):
        raise ValueError(f"unknown rationale source: {rationale_source}")

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE agent_decisions
               SET rationale = $2, rationale_source = $3
             WHERE id = $1::uuid AND rationale IS NULL
         RETURNING id, outcome, rationale
            """,
            decision_id, rationale.strip()[:1000], rationale_source,
        )
    if row is None:
        raise LookupError(
            f"decision {decision_id} not found, or already carries a reason")
    return {
        "id": str(row["id"]),
        "outcome": row["outcome"],
        "rationale": row["rationale"],
    }


def _confidence_threshold(buckets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The confidence at which this agent starts acting.

    Reported only when the pattern is actually monotonic-ish: if acceptance does
    not rise with confidence, this agent is not using confidence to decide and
    claiming a threshold would be inventing a rule they do not have.
    """
    usable = [b for b in buckets if b["total"] >= 5]
    if len(usable) < 2:
        return None
    usable.sort(key=lambda b: b["floor"])
    first, last = usable[0], usable[-1]
    if last["rate"] <= first["rate"] + 0.15:
        return None
    crossing = next((b for b in usable if b["rate"] >= 0.5), None)
    if crossing is None:
        return None
    return {
        "acts_above": crossing["floor"],
        "detail": (
            f"Acts on most recommendations at {crossing['floor']:.0%} confidence "
            f"and above ({crossing['accepted']}/{crossing['total']}); below that, "
            f"{first['accepted']}/{first['total']}."
        ),
    }


async def policy(ctx: TenantContext) -> dict[str, Any]:
    """What this agent's decisions say about how they work.

    NOTE ON THE WHERE CLAUSE. Unlike belief_store and intent_states — where
    repeating `tenant_id = app_current_tenant()` reproduced half an RLS policy
    and wrongly hid rows — the filters here are a BUSINESS predicate, not a
    security one. A twin is one person's decision policy; a broker owner or a
    platform admin must see their own habits, not the pooled habits of everyone
    whose rows RLS would let them read. Averaging two agents into one twin would
    describe nobody. Do not "simplify" these away.

    Returns `learning` with a count when there is not yet enough to say
    anything. That is a real answer, not a placeholder: it tells the agent the
    twin is watching and has not started guessing.
    """
    async with tenant_tx(ctx) as conn:
        total = await conn.fetchval(
            "SELECT count(*)::int FROM agent_decisions "
            "WHERE tenant_id = app_current_tenant() AND user_id = app_current_agent()"
        )
        if (total or 0) < MIN_DECISIONS_FOR_POLICY:
            return {
                "status": "learning",
                "decisions_recorded": total or 0,
                "decisions_needed": MIN_DECISIONS_FOR_POLICY - (total or 0),
                "summary": (
                    f"Watching. {total or 0} decision"
                    f"{'' if total == 1 else 's'} recorded so far — not enough to "
                    f"describe how you work without guessing."
                ),
            }

        by_kind = await conn.fetch(
            """
            SELECT opportunity_kind AS kind,
                   count(*)::int AS total,
                   count(*) FILTER (WHERE outcome = 'accepted')::int AS accepted,
                   count(*) FILTER (WHERE outcome = 'deferred')::int AS deferred,
                   count(*) FILTER (WHERE outcome = 'dismissed')::int AS dismissed
              FROM agent_decisions
             WHERE tenant_id = app_current_tenant() AND user_id = app_current_agent()
          GROUP BY 1 ORDER BY 2 DESC
            """
        )
        buckets = await conn.fetch(
            """
            SELECT width_bucket(recommended_confidence, 0.4, 1.0, 4) AS b,
                   count(*)::int AS total,
                   count(*) FILTER (WHERE outcome = 'accepted')::int AS accepted
              FROM agent_decisions
             WHERE tenant_id = app_current_tenant() AND user_id = app_current_agent()
               AND recommended_confidence IS NOT NULL
          GROUP BY 1 ORDER BY 1
            """
        )
        reasons = await conn.fetch(
            """
            SELECT rationale, rationale_source, count(*)::int AS n,
                   max(decided_at) AS latest
              FROM agent_decisions
             WHERE tenant_id = app_current_tenant() AND user_id = app_current_agent()
               AND rationale IS NOT NULL AND rationale_source <> 'inferred'
          GROUP BY 1, 2 ORDER BY 3 DESC, 4 DESC LIMIT 8
            """
        )

    kinds = []
    for row in by_kind:
        if row["total"] < MIN_DECISIONS_PER_KIND:
            # Counted, but no rate. An interval this wide says nothing and
            # printing it invites the reader to average it in their head.
            kinds.append({
                "kind": row["kind"], "total": row["total"],
                "rate": None,
                "note": f"{row['total']} so far — too few to read a preference.",
            })
            continue
        point, low, high = wilson_interval(row["accepted"], row["total"])
        kinds.append({
            "kind": row["kind"],
            "total": row["total"],
            "accepted": row["accepted"],
            "deferred": row["deferred"],
            "dismissed": row["dismissed"],
            "rate": point,
            "rate_low": low,
            "rate_high": high,
            # The interval is spoken, not just returned, because a UI that
            # renders only `rate` would reintroduce exactly the false precision
            # this module exists to avoid.
            "note": (
                f"Acted on {row['accepted']} of {row['total']} "
                f"({low:.0%}–{high:.0%} at 95% confidence)."
            ),
        })

    bucket_rows = []
    for row in buckets:
        if not row["b"] or row["b"] > 4:
            continue
        floor = 0.4 + (row["b"] - 1) * 0.15
        bucket_rows.append({
            "floor": round(floor, 2),
            "total": row["total"],
            "accepted": row["accepted"],
            "rate": row["accepted"] / row["total"] if row["total"] else 0.0,
        })

    return {
        "status": "ready",
        "decisions_recorded": total,
        "by_kind": kinds,
        "confidence_threshold": _confidence_threshold(bucket_rows),
        # Verbatim, attributed, and never paraphrased. "She's wasting time until
        # she gets pre-approved" is a rule this agent runs and the model does
        # not have; summarising it into "prefers qualified leads" throws away
        # the only part worth keeping.
        "stated_reasons": [
            {
                "reason": r["rationale"],
                "source": r["rationale_source"],
                "times": r["n"],
                "latest": r["latest"].isoformat(),
            }
            for r in reasons
        ],
        "caveat": (
            "Observed preferences, not results. Nothing here is weighted by "
            "whether the action worked — no outcomes have been recorded yet — so "
            "this describes how you decide, not whether it pays off."
        ),
    }
