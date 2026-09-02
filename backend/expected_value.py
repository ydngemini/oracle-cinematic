"""What an hour of the agent's attention is worth, and where to spend it.

A task list of 127 items is not a plan, because it does not say which item to do
first. Ranking by due date ranks by whoever set the reminder; ranking by "hot
lead: 83" ranks by a number with no units. The only ordering an agent can act on
is the one that answers "which of these is worth the most, right now".

    EV = P(conversion) × gross commission × uplift(acting now) − cost − risk

Every term is reported separately, and this is the important design decision.
A single dollar figure is unfalsifiable — the agent cannot tell a confident
estimate from a wild one, so after the first bad ranking they stop trusting all
of them. Shown as a decomposition, a wrong number is *debuggable*: the agent can
see it assumed a 2.5% commission on a $400K median and correct the assumption
rather than abandon the feature.

CALIBRATION. `uplift` is the honest weak point. Claiming "calling within an hour
converts 2.7× better" requires having measured it here, and this deployment has
closed no deals through the system yet. So uplift is a STATED PRIOR, every
result carries `calibrated: false`, and the UI is expected to render these as
relative ordering rather than promised dollars. `agent_decisions` and closed
transactions are the intended fitting data; until there are enough of both, an
uncalibrated ranking that says so beats a calibrated-looking one that lies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from tenancy import TenantContext

logger = logging.getLogger("oracle.expected_value")

#: Per opportunity kind, before a fitted uplift replaces the stated prior. At
#: n=30 and p≈0.3 the Wilson interval is roughly ±0.16 — narrow enough that one
#: deal does not move the ranking. Below that, a "fitted" number is noise
#: wearing a decimal point.
MIN_OUTCOMES_PER_KIND_FOR_UPLIFT = 30

#: Tenant-wide, before portfolio() is allowed to call itself calibrated at all.
MIN_OUTCOMES_FOR_PORTFOLIO = 50

#: A fitted interval wider than this is still a prior, whatever n says.
MAX_INTERVAL_WIDTH = 0.35

#: How far back fit() looks. Long enough to accumulate outcomes; short enough
#: that a rate from a different market regime does not sit in the average.
FIT_WINDOW_DAYS = 365


@dataclass
class Calibration:
    """What this brokerage's own outcomes say about each kind of action.

    `per_kind` maps an opportunity kind to (uplift, n, low, high). The uplift
    is a crude difference in rates — P(positive result | acted on) minus the
    organic base rate — not a causal estimate; there is no counterfactual
    here, only a comparison against outcomes that followed nothing we did.
    That is stated on every result that uses it.
    """
    per_kind: dict[str, tuple[float, int, float, float]] = field(default_factory=dict)
    total_outcomes: int = 0
    base_rate: float = 0.0
    fitted_at: Optional[str] = None

    def uplift_for(self, kind: str) -> tuple[float, bool]:
        """(uplift, is_fitted). Falls back to the prior below the thresholds."""
        entry = self.per_kind.get(kind)
        if entry is None:
            return TIMING_UPLIFT.get(kind, DEFAULT_UPLIFT), False
        uplift, n, low, high = entry
        if n < MIN_OUTCOMES_PER_KIND_FOR_UPLIFT or (high - low) > MAX_INTERVAL_WIDTH:
            return TIMING_UPLIFT.get(kind, DEFAULT_UPLIFT), False
        return uplift, True


async def fit(ctx: "TenantContext") -> Calibration:
    """Fit uplift per opportunity kind from Outcome Memory.

    Reads agent_decisions rows that were accepted AND received a result, groups
    by opportunity_kind, and compares the positive-result rate against the
    organic base rate from outcome_events (rows attribution examined and
    credited to nothing). Both sides come from the same table family and the
    same window, so a busy quarter inflates both and cancels.

    Never raises: a calibration that cannot be read is an empty one, and an
    empty one makes value_of() use its priors — which is what it did before
    this function existed.
    """
    from datetime import datetime, timedelta, timezone

    from agent_twin import wilson_interval
    from db.connection import tenant_tx

    since = datetime.now(timezone.utc) - timedelta(days=FIT_WINDOW_DAYS)
    try:
        async with tenant_tx(ctx) as conn:
            acted = await conn.fetch(
                """
                SELECT opportunity_kind,
                       count(*)::int AS n,
                       count(*) FILTER (WHERE result_valence > 0)::int AS positive
                  FROM agent_decisions
                 WHERE outcome = 'accepted'
                   AND result_kind IS NOT NULL
                   AND result_at >= $1
              GROUP BY opportunity_kind
                """,
                since,
            )
            organic = await conn.fetchrow(
                """
                SELECT count(*)::int AS n,
                       count(*) FILTER (WHERE outcome_valence > 0)::int AS positive
                  FROM outcome_events
                 WHERE attributed_at IS NOT NULL
                   AND attributed_trace_id IS NULL
                   AND attributed_decision_id IS NULL
                   AND occurred_at >= $1
                """,
                since,
            )
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning("calibration unavailable; priors stay in force", exc_info=True)
        return Calibration()

    base_n = int(organic["n"] or 0) if organic else 0
    base_positive = int(organic["positive"] or 0) if organic else 0
    base_rate = (base_positive / base_n) if base_n else 0.0

    per_kind: dict[str, tuple[float, int, float, float]] = {}
    total = base_n
    for row in acted:
        n = int(row["n"])
        total += n
        point, low, high = wilson_interval(int(row["positive"]), n)
        # The interval is on the acted-on rate; the uplift is that rate less
        # the base rate. Both bounds shift by the same constant, so the width
        # — the thing the threshold checks — is unchanged.
        per_kind[row["opportunity_kind"]] = (
            round(point - base_rate, 4), n,
            round(low - base_rate, 4), round(high - base_rate, 4),
        )

    return Calibration(
        per_kind=per_kind,
        total_outcomes=total,
        base_rate=round(base_rate, 4),
        fitted_at=datetime.now(timezone.utc).isoformat(),
    )

#: Fallback gross commission rate when the tenant has not configured one.
#: US residential convention for one side of a transaction.
DEFAULT_COMMISSION_RATE = 0.025

#: Acting on an opportunity now versus letting it age. Stated priors, ordered by
#: how perishable the situation is — a contract deadline is a cliff, a dormant
#: past client is not.
TIMING_UPLIFT: dict[str, float] = {
    "contract_deadline": 0.45,
    "behavioural_spike": 0.35,
    "price_reduction_match": 0.30,
    "intent_next_action": 0.20,
    "belief_dispute": 0.15,
    "distress_signal": 0.12,
    "dormant_reengagement": 0.10,
}
DEFAULT_UPLIFT = 0.15

#: Rough minutes of agent time, converted to money at the tenant's own implied
#: hourly rate. Included because "call this person" and "review this contract"
#: are not the same ask, and an EV that ignores cost ranks a two-minute text
#: below a two-hour analysis with the same payoff.
ACTION_MINUTES: dict[str, int] = {
    "call": 20, "text": 5, "email": 12, "review": 45,
    "research": 30, "meeting": 90, "none": 5,
}


@dataclass
class ValuedAction:
    expected_value: float
    probability: float
    gross_commission: float
    uplift: float
    cost: float
    risk_discount: float
    calibrated: bool
    basis: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_value": round(self.expected_value, 2),
            "probability": round(self.probability, 3),
            "gross_commission": round(self.gross_commission, 2),
            "uplift": round(self.uplift, 3),
            "cost": round(self.cost, 2),
            "risk_discount": round(self.risk_discount, 3),
            "calibrated": self.calibrated,
            "basis": self.basis,
        }


def value_of(
    *,
    kind: str,
    confidence: float,
    deal_value: Optional[float],
    action_type: str = "call",
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    hourly_rate: float = 75.0,
    market_median: Optional[float] = None,
    calibration: Optional[Calibration] = None,
) -> Optional[ValuedAction]:
    """Value one opportunity, or decline to.

    Returns None when there is no defensible basis for a dollar figure — no deal
    value and no market median. A ranked list with a hole in it is more useful
    than one where a guessed number sorts above a measured one, and this is the
    same refusal `avm_client` makes when it reports estimatedValue=0 rather than
    inventing a valuation.
    """
    basis: list[str] = []

    if deal_value and deal_value > 0:
        gross = deal_value * commission_rate
        basis.append(f"{commission_rate:.2%} of a known ${deal_value:,.0f} deal value")
    elif market_median and market_median > 0:
        gross = market_median * commission_rate
        basis.append(
            f"{commission_rate:.2%} of the ${market_median:,.0f} local median — "
            f"no deal value on file, so this is a market-typical stand-in"
        )
    else:
        return None

    if calibration is not None:
        uplift, fitted = calibration.uplift_for(kind)
    else:
        uplift, fitted = TIMING_UPLIFT.get(kind, DEFAULT_UPLIFT), False
    if fitted:
        _, n, low, high = calibration.per_kind[kind]
        basis.append(
            f"{uplift:+.0%} uplift fitted from {n} of this brokerage's own outcomes "
            f"({low:+.0%} to {high:+.0%} at 95%) — a difference in rates, not a causal estimate"
        )
    else:
        basis.append(f"{uplift:.0%} assumed uplift from acting now rather than later (uncalibrated)")

    minutes = ACTION_MINUTES.get(action_type, ACTION_MINUTES["none"])
    cost = (minutes / 60.0) * hourly_rate
    basis.append(f"~{minutes} min of agent time at ${hourly_rate:.0f}/hr")

    # Low-confidence opportunities are not merely worth less in proportion to
    # their probability — acting on them also costs credibility with the client.
    # The quadratic discount makes a 50% call rank well below half a 100% call,
    # which matches how agents actually triage.
    risk_discount = round(1.0 - (1.0 - confidence) ** 2, 3)

    ev = (confidence * gross * uplift * risk_discount) - cost

    return ValuedAction(
        expected_value=ev,
        probability=confidence,
        gross_commission=gross,
        uplift=uplift,
        cost=cost,
        risk_discount=risk_discount,
        calibrated=fitted,
        basis=basis,
    )


def portfolio(
    valued: list[ValuedAction], *, calibration: Optional[Calibration] = None,
) -> dict[str, Any]:
    """The headline number, with the caveat attached to it rather than beside it.

    `calibrated` flips only when enough outcomes exist tenant-wide AND every
    valued action used a fitted uplift. One prior in the sum makes the total
    a mixed figure, and a mixed figure labelled "calibrated" is the exact
    false precision this module refuses.
    """
    positives = [v for v in valued if v.expected_value > 0]
    total = sum(v.expected_value for v in positives)
    outcomes = calibration.total_outcomes if calibration else 0
    all_fitted = bool(positives) and all(v.calibrated for v in positives)
    calibrated = outcomes >= MIN_OUTCOMES_FOR_PORTFOLIO and all_fitted

    if calibrated:
        caveat = (
            f"Fitted from {outcomes} of this brokerage's own outcomes. The uplifts are "
            f"differences in rates against the organic base rate, not causal "
            f"estimates; use the ordering, and read the amounts as expectations "
            f"with the intervals each card shows."
        )
    elif outcomes:
        caveat = (
            f"Modelled from stated priors. {outcomes} outcome"
            f"{'' if outcomes == 1 else 's'} recorded so far — "
            f"{max(0, MIN_OUTCOMES_FOR_PORTFOLIO - outcomes)} more before the "
            f"portfolio figure is fitted rather than assumed. Use the ordering; "
            f"treat the dollar amounts as relative, not forecast."
        )
    else:
        caveat = (
            "Modelled from stated priors, not fitted to this brokerage's own "
            "closed deals — no outcomes have been recorded yet. Use the ordering; "
            "treat the dollar amounts as relative, not forecast."
        )
    return {
        "total_expected_value": round(total, 2),
        "opportunity_count": len(positives),
        "suppressed_negative_ev": len(valued) - len(positives),
        "calibrated": calibrated,
        "outcomes_observed": outcomes,
        "outcomes_needed": max(0, MIN_OUTCOMES_FOR_PORTFOLIO - outcomes),
        "caveat": caveat,
    }
