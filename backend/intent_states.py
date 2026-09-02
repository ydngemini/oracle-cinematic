"""Declared intent, observed intent, and the gap between them.

A CRM stores what a client said: "we're probably six months away". It is a fact
about a sentence, not about a person. People understate timelines to avoid being
sold to, overstate budgets to be taken seriously, and change their minds without
filing an amendment.

So this module keeps two readings and refuses to merge them into one number:

  DECLARED  what they told us, with when they told us
  OBSERVED  what they have actually done, from first-party behaviour
  LATENT    the reconciliation — and, when the two disagree, the disagreement
            itself, stated plainly, because that is the sellable insight

The rule that keeps this honest is that **absence of signal is not signal of
absence.** A client with no behavioural rows is not a client with zero intent;
they are a client we cannot see. Those two produce identical numbers under a
naive scorer and require opposite responses — one needs a nurture campaign, the
other needs the portal instrumented. Every function here therefore returns an
`evidence_state` alongside its numbers, and callers that ignore it will render
"cold" over people the system has simply never watched.

That is not hypothetical. interaction_logs currently holds four rows across the
entire deployment, so at the time of writing `observed` is `unobserved` for
essentially every client, and the honest UI says so instead of drawing a zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import TenantContext

logger = logging.getLogger("oracle.intent_states")

#: WHO the row is about. interaction_logs is shared by the brokerage's own
#: activity and the client's — crm.py writes actor_role='agent' for every
#: outbound message — so a reading that counts every row measures how busy the
#: agent has been and reports it as how interested the client is.
#:
#: This filter is the whole reason the observed score means anything. The moment
#: an agent-side surface emits a listing_view (which is exactly what wiring the
#: property page would do), an unfiltered count would score the agent's own
#: browsing as their client's intent, silently, on every client they look at.
CLIENT_ACTORS: tuple[str, ...] = ("buyer", "seller")

#: The window behavioural intent is read over. Long enough to survive a holiday,
#: short enough that a burst three weeks ago does not read as current heat.
OBSERVATION_WINDOW_DAYS = 21

#: Below this many behavioural events we will not compute an observed reading at
#: all. Two clicks is not a pattern, and a confident number built on two clicks
#: is worse than an honest gap because the agent cannot tell them apart.
MIN_EVENTS_FOR_OBSERVED = 4

#: What each behaviour says about proximity to a transaction, and how much.
#: Weights are intentionally coarse — these are ordinal judgements about
#: intent, and a false precision (0.734) would imply a calibration this has
#: never been fitted against. They are re-fittable from agent_decisions and
#: closed-deal outcomes once there is enough of either; until then they are
#: stated priors, and `basis` says so on every reading.
SIGNAL_WEIGHTS: dict[str, float] = {
    "showing_request":   1.00,   # asked to stand inside it
    "calculator_use":    0.75,   # doing arithmetic about their own money
    "availability_view": 0.70,   # opened the calendar and did not book
    "saved_search":      0.55,   # asked to be interrupted in future
    "listing_favorite":  0.50,
    "listing_share":     0.45,   # showing someone else — a co-decider exists
    "search":            0.30,
    "listing_view":      0.25,
    "map_view":          0.15,
    "email_open":        0.10,
    "link_click":        0.15,
    "portal_view":       0.20,
    # Negative. A removed favourite is information, and a scorer that only ever
    # adds will drift upward forever and call everyone hot by March.
    "listing_unfavorite": -0.35,
}

#: Buyer and seller journeys. A client occupies a distribution over these, not a
#: single label, because the evidence genuinely underdetermines the answer — and
#: a stage dropdown that forces one value is how CRMs end up full of stale
#: "Active" rows nobody trusts.
BUYER_STATES = [
    "browsing", "exploring", "defining_criteria", "financial_preparation",
    "actively_shopping", "showing", "offer_ready", "negotiating",
    "under_contract", "closed",
]
SELLER_STATES = [
    "curious", "valuation_aware", "considering", "preparing",
    "interviewing_agents", "listing_ready", "listed", "negotiating", "closed",
]

#: Signals that specifically indicate a state, used to shape the distribution.
_STATE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "browsing": ("listing_view", "map_view", "email_open"),
    "exploring": ("listing_view", "search", "listing_favorite"),
    "defining_criteria": ("saved_search", "search", "listing_favorite"),
    "financial_preparation": ("calculator_use",),
    "actively_shopping": ("listing_favorite", "listing_share", "saved_search"),
    "showing": ("showing_request", "availability_view"),
}

#: What to do to move someone forward, keyed by the gap that is holding them.
#: Prescriptive rather than descriptive: "hot lead: 83" tells an agent nothing
#: they can act on, and this exists to answer "what would raise it".
_LEVERS: dict[str, tuple[str, str]] = {
    "no_financing_signal": (
        "Introduce a lender",
        "Nothing on file shows they have tested affordability. Buyers who "
        "complete pre-approval reach a showing materially sooner.",
    ),
    "no_criteria": (
        "Pin down criteria",
        "Their saved searches are broad enough that no inventory can match "
        "them well. Narrowing beds/area/price makes every later suggestion better.",
    ),
    "no_showing": (
        "Offer two concrete times",
        "They have looked without ever asking to visit. An open-ended "
        "'let me know' asks them to do the scheduling work.",
    ),
    "stalled": (
        "Re-engage with a change, not a check-in",
        "Activity has stopped. A price cut or new listing gives a reason to "
        "reply; 'just following up' does not.",
    ),
    "unobserved": (
        "Instrument the portal for this client",
        "Nothing they do is visible to us, so every reading here rests on what "
        "they last said. Send a portal link and the picture fills in by itself.",
    ),
}


@dataclass
class IntentReading:
    """One reading with its own honesty attached.

    `evidence_state` is not decoration. 'unobserved' and 'weak' must reach the
    UI, because a 0.0 that means "no data" and a 0.0 that means "cold" call for
    opposite actions from the agent.
    """
    score: Optional[float]
    evidence_state: str          # 'observed' | 'weak' | 'unobserved'
    basis: str
    signals: dict[str, int] = field(default_factory=dict)
    as_of: Optional[str] = None


def _observed_reading(counts: dict[str, int], newest: Optional[datetime]) -> IntentReading:
    total = sum(counts.values())
    if total == 0:
        return IntentReading(
            score=None, evidence_state="unobserved",
            basis=(
                "No behavioural events recorded for this client. This is a "
                "capture gap, not a cold client — nothing here should be read "
                "as low intent."
            ),
        )
    if total < MIN_EVENTS_FOR_OBSERVED:
        return IntentReading(
            score=None, evidence_state="weak", signals=dict(counts),
            basis=(
                f"Only {total} behavioural event(s) in {OBSERVATION_WINDOW_DAYS} "
                f"days — below the {MIN_EVENTS_FOR_OBSERVED} needed to read a "
                f"pattern rather than a coincidence."
            ),
            as_of=newest.isoformat() if newest else None,
        )

    # Weighted sum, squashed. Repeats of the same behaviour count with
    # diminishing returns: the fifth view of a listing is real evidence, the
    # fiftieth is a browser tab left open.
    raw = 0.0
    for signal, count in counts.items():
        weight = SIGNAL_WEIGHTS.get(signal, 0.1)
        raw += weight * (1 + (count - 1) ** 0.5 if count > 1 else 1)

    score = round(max(0.0, min(raw / (raw + 4.0), 0.97)), 3)
    top = sorted(counts.items(), key=lambda kv: -SIGNAL_WEIGHTS.get(kv[0], 0.1) * kv[1])[:3]
    return IntentReading(
        score=score, evidence_state="observed", signals=dict(counts),
        basis="Weighted from " + ", ".join(f"{n}× {s.replace('_', ' ')}" for s, n in top),
        as_of=newest.isoformat() if newest else None,
    )


def _declared_reading(
    stage: Optional[str], lead_score: Optional[int], timeline_belief: Optional[dict],
) -> IntentReading:
    if timeline_belief is not None:
        return IntentReading(
            score=round(float(timeline_belief["confidence"]), 3),
            evidence_state="observed",
            basis=(
                f"They said: {timeline_belief['value']!r} "
                f"({timeline_belief['age_days']:.0f} days ago, "
                f"via {timeline_belief['source']['kind']})"
            ),
            as_of=timeline_belief["learned_at"],
        )
    # `lead_score` defaults to 0 on every new client, so 0 means "nobody has
    # scored this person" far more often than it means "scored, and cold".
    # Reading it as a declared intent of 0% would manufacture exactly the
    # false zero this module exists to prevent — and worse, it would then be
    # compared against real behaviour and reported as a dramatic contradiction.
    if lead_score:
        return IntentReading(
            score=round(lead_score / 100.0, 3), evidence_state="weak",
            basis=(
                f"Staff-assigned lead score of {lead_score}. This is an opinion "
                f"someone typed, not something the client said or did."
            ),
        )
    if stage:
        return IntentReading(
            score=None, evidence_state="weak",
            basis=f"Only a pipeline stage of {stage!r} — set by staff, undated.",
        )
    return IntentReading(
        score=None, evidence_state="unobserved",
        basis="Nothing declared: no stated timeline, no lead score, no stage.",
    )


def _state_distribution(
    counts: dict[str, int], declared: IntentReading, observed: IntentReading,
) -> list[dict[str, Any]]:
    """A distribution over journey states, or nothing at all.

    Returns [] when there is no behavioural evidence. A uniform prior dressed up
    as a prediction is the single most misleading thing this module could
    render: it looks like knowledge and contains none.
    """
    if observed.evidence_state == "unobserved":
        return []

    raw: dict[str, float] = {}
    for state, indicators in _STATE_EVIDENCE.items():
        weight = sum(counts.get(sig, 0) * max(SIGNAL_WEIGHTS.get(sig, 0.1), 0.0)
                     for sig in indicators)
        if weight > 0:
            raw[state] = weight
    if not raw:
        return []

    total = sum(raw.values())
    ranked = sorted(raw.items(), key=lambda kv: -kv[1])
    # Cap any single state — the evidence here is never strong enough to justify
    # near-certainty about where someone is in their own head.
    return [
        {"state": state, "probability": round(min(value / total, 0.85), 3)}
        for state, value in ranked[:4]
    ]


def _reconcile(declared: IntentReading, observed: IntentReading) -> dict[str, Any]:
    """The headline: do their words and their behaviour agree?

    This is the part competitors' behavioural scoring does not say out loud.
    Everyone can compute engagement; the useful sentence is "she says six
    months and is behaving like thirty days", and it only exists if the two
    readings were kept apart long enough to be compared.
    """
    if observed.evidence_state == "unobserved":
        return {
            "verdict": "cannot_compare",
            "summary": (
                "Only their declared position is available — nothing they do is "
                "visible to us yet, so there is nothing to check it against."
            ),
            "latent_score": declared.score,
            "confidence": 0.3 if declared.score is not None else 0.0,
        }
    if declared.score is None:
        return {
            "verdict": "behaviour_only",
            "summary": (
                "Behaviour is visible but they have never stated a timeline. "
                "Worth asking, so the two can be compared."
            ),
            "latent_score": observed.score,
            "confidence": 0.5,
        }

    gap = observed.score - declared.score
    latent = round(declared.score * 0.35 + observed.score * 0.65, 3)

    if gap >= 0.25:
        verdict, summary = "behaviour_ahead", (
            f"Acting well ahead of what they said. Declared position reads "
            f"{declared.score:.0%}; behaviour reads {observed.score:.0%}. "
            f"Treat the timeline they gave as conservative."
        )
    elif gap <= -0.25:
        verdict, summary = "behaviour_behind", (
            f"Stated intent is not showing up in behaviour "
            f"({declared.score:.0%} declared vs {observed.score:.0%} observed). "
            f"Either something changed, or the plan was aspirational."
        )
    else:
        verdict, summary = "consistent", (
            "What they say and what they do agree, which makes both more "
            "trustworthy than either alone."
        )

    return {
        "verdict": verdict,
        "summary": summary,
        "latent_score": latent,
        "gap": round(gap, 3),
        # Agreement between independent readings is itself evidence; the
        # confidence is higher when they corroborate than when they clash.
        "confidence": round(0.75 if verdict == "consistent" else 0.6, 2),
    }


def _levers(counts: dict[str, int], observed: IntentReading, newest: Optional[datetime]) -> list[dict[str, str]]:
    """What would move this person forward — the prescriptive half."""
    gaps: list[str] = []
    if observed.evidence_state == "unobserved":
        gaps.append("unobserved")
    else:
        if not counts.get("calculator_use"):
            gaps.append("no_financing_signal")
        if not counts.get("saved_search") and not counts.get("search"):
            gaps.append("no_criteria")
        if not counts.get("showing_request"):
            gaps.append("no_showing")
        if newest and (datetime.now(timezone.utc) - newest).days > 14:
            gaps.append("stalled")

    out = []
    for gap in gaps:
        action, why = _LEVERS[gap]
        out.append({"gap": gap, "action": action, "why": why})
    return out


async def read_intent(ctx: TenantContext, client_id: str) -> dict[str, Any]:
    """The full intent picture for one client."""
    import belief_store

    since = datetime.now(timezone.utc) - timedelta(days=OBSERVATION_WINDOW_DAYS)

    async with tenant_tx(ctx) as conn:
        # No tenant predicate here on purpose. `clients` is FORCE RLS'd with
        # `app_is_platform_admin() OR tenant_id = app_current_tenant()`, so the
        # policy already scopes this — and repeating only the second half of it
        # in the WHERE clause silently narrowed the result for a platform admin,
        # producing a 404 on a client they were looking at a moment earlier.
        # Duplicating a security predicate in application SQL can only ever make
        # it wrong; it cannot make it safer.
        client = await conn.fetchrow(
            """
            SELECT id, full_name, client_type, stage, lead_score
              FROM clients
             WHERE id = $1::uuid
            """,
            client_id,
        )
        if client is None:
            raise LookupError(f"client {client_id} not found")

        rows = await conn.fetch(
            """
            SELECT interaction_type, count(*) AS n, max(created_at) AS newest
              FROM interaction_logs
             WHERE client_id = $1::uuid
               AND created_at >= $2
               AND interaction_type = ANY($3::text[])
               AND actor_role = ANY($4::text[])
          GROUP BY interaction_type
            """,
            client_id, since, list(SIGNAL_WEIGHTS.keys()), list(CLIENT_ACTORS),
        )

    counts = {r["interaction_type"]: r["n"] for r in rows}
    newest = max((r["newest"] for r in rows), default=None)

    known = await belief_store.beliefs_about(ctx, "client", client_id)
    timeline = next(iter(known["beliefs"].get("timeline", [])), None)

    observed = _observed_reading(counts, newest)
    declared = _declared_reading(client["stage"], client["lead_score"], timeline)
    reconciliation = _reconcile(declared, observed)

    states = SELLER_STATES if (client["client_type"] or "").lower() == "seller" else BUYER_STATES

    return {
        "client_id": str(client["id"]),
        "client_name": client["full_name"],
        "journey": "seller" if states is SELLER_STATES else "buyer",
        "declared": declared.__dict__,
        "observed": observed.__dict__,
        "latent": reconciliation,
        "state_distribution": _state_distribution(counts, declared, observed),
        "levers": _levers(counts, observed, newest),
        "disputes": known["disputes"],
        "window_days": OBSERVATION_WINDOW_DAYS,
    }
