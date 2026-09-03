"""What a mission WOULD do, before anything does it.

This is the screen a person reads before launching something that will contact
their clients under their licence, so it has one job beyond arithmetic: it must
not read like a forecast. Everything here is deterministic — the same mission
and the same candidates produce the same simulation — and every number carries
what it was derived from.

The expected result is the part most likely to be misread, so it is deliberately
awkward: a Wilson interval over PRIOR rates, labelled `calibrated: false`, and
spoken in prose that says the brokerage's own outcomes have not been fitted.
A point estimate here would be a rate the agent could falsify from memory,
which is how a feature loses its credibility permanently.
"""

from __future__ import annotations

from typing import Any, Optional

from agent_twin import wilson_interval

from . import costs

#: A candidate at or above this is "strong". Stated, not fitted.
STRONG_SCORE = 0.6

#: Prior reply/engagement rates per channel, before any of this brokerage's own
#: outcomes exist. Industry-order-of-magnitude, not measured here.
PRIOR_RATES: dict[str, float] = {
    "email": 0.08,
    "sms": 0.18,
    "voice": 0.25,
    "task": 0.0,
}

#: The denominator the prior interval is computed against. Small on purpose:
#: it produces a wide interval, which is the honest shape of "we are guessing".
PRIOR_SAMPLE = 40


def simulate(
    mission: dict[str, Any],
    candidates: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    outcomes_observed: int = 0,
) -> dict[str, Any]:
    """A deterministic account of the plan: who, what, what it costs, what it
    might return, and what none of it is based on."""
    analysed = list(candidates or [])
    strong = [c for c in analysed if _score(c) >= STRONG_SCORE]
    recommended = [c for c in analysed if (c.get("state") or "proposed") == "selected"] or strong

    cost = costs.cost_of(actions)
    expected = _expected_result(actions, outcomes_observed)
    budget_cents = int(mission.get("budget_cents") or 0)

    return {
        "candidates": {
            "analysed": len(analysed),
            "strong": len(strong),
            "recommended": len(recommended),
            "threshold": STRONG_SCORE,
        },
        "actions": {
            "planned": len(actions or []),
            "by_channel": cost["by_channel"],
        },
        "cost": {
            "total_cents": cost["total_cents"],
            "total_minutes": cost["total_minutes"],
            "budget_cents": budget_cents,
            "within_budget": budget_cents == 0 or cost["total_cents"] <= budget_cents,
            "basis": cost["basis"],
        },
        "expected": expected,
        "caveat": _caveat(expected, len(analysed)),
    }


def _score(candidate: dict[str, Any]) -> float:
    try:
        return float(candidate.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _expected_result(
    actions: list[dict[str, Any]], outcomes_observed: int,
) -> dict[str, Any]:
    """A range, never a number.

    The interval is Wilson over the prior rate at a small assumed sample, so it
    is wide by construction. That width IS the message.
    """
    planned = actions or []
    if not planned:
        return {
            "replies_low": 0, "replies_high": 0, "rate_low": 0.0, "rate_high": 0.0,
            "calibrated": False, "outcomes_observed": outcomes_observed,
        }

    low_total = 0.0
    high_total = 0.0
    for action in planned:
        rate = PRIOR_RATES.get(action.get("channel") or "task", 0.0)
        successes = round(rate * PRIOR_SAMPLE)
        _point, low, high = wilson_interval(successes, PRIOR_SAMPLE)
        low_total += low
        high_total += high

    return {
        "replies_low": int(low_total),
        "replies_high": int(round(high_total)),
        "rate_low": round(low_total / len(planned), 3),
        "rate_high": round(high_total / len(planned), 3),
        # Never True in this module. Calibration is the evaluator's job once
        # real outcomes exist, and claiming it here would be the lie.
        "calibrated": False,
        "outcomes_observed": outcomes_observed,
    }


def _caveat(expected: dict[str, Any], analysed: int) -> str:
    """Spoken, not a flag. The label `calibrated: false` is for code; this
    sentence is what the person actually reads before pressing launch."""
    if analysed == 0:
        return (
            "Nothing matched this objective yet, so there is no plan to price. "
            "That is a fact about the current book, not a prediction about it."
        )
    observed = expected.get("outcomes_observed") or 0
    if observed:
        return (
            f"Modelled from published rates, not from this brokerage's own results. "
            f"{observed} outcome{'s' if observed != 1 else ''} have been recorded so "
            f"far — not enough to fit against, so the range below is still priors. "
            f"Read it as an order of magnitude, not a forecast."
        )
    return (
        "Modelled from published rates. No outcomes have been recorded for this "
        "brokerage yet, so nothing here is fitted to how your clients actually "
        "respond. Read the range as an order of magnitude, not a forecast, and "
        "expect it to move once the first replies land."
    )
