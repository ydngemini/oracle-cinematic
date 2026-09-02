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
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("oracle.expected_value")

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

    uplift = TIMING_UPLIFT.get(kind, DEFAULT_UPLIFT)
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
        calibrated=False,
        basis=basis,
    )


def portfolio(valued: list[ValuedAction]) -> dict[str, Any]:
    """The headline number, with the caveat attached to it rather than beside it."""
    positives = [v for v in valued if v.expected_value > 0]
    total = sum(v.expected_value for v in positives)
    return {
        "total_expected_value": round(total, 2),
        "opportunity_count": len(positives),
        "suppressed_negative_ev": len(valued) - len(positives),
        "calibrated": False,
        "caveat": (
            "Modelled from stated priors, not fitted to this brokerage's own "
            "closed deals — no outcomes have been recorded yet. Use the ordering; "
            "treat the dollar amounts as relative, not forecast."
        ),
    }
