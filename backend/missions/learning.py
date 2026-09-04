"""What Missions learns, and — mostly — what it does not.

This module is rules. Not a model, not a bandit, not reinforcement learning of
any kind. That is a deliberate stopping point, and this docstring exists so the
next person to open the file knows it was a decision rather than an omission.

WHAT IS HERE
    Ordered, inspectable rules over what the evaluator measured: prefer the
    channel that separated, back off a channel that is producing suppressions,
    stop a mission that has hit its target. Each returns a sentence naming the
    evidence, because a rule an agent cannot audit is a black box with extra
    steps.

WHAT IS NOT HERE, AND WHY
    Propensity modelling, uplift modelling, contextual bandits and any online
    policy that reallocates traffic on its own. Not because they are wrong —
    they are the right shape for this problem — but because none of them can be
    fitted from what exists. The whole product recorded its first outcome this
    week; `outcome_events` is where they land and its base rates are still
    priors.

    The volumes that would justify each, per tenant, so this can be revisited
    against evidence rather than enthusiasm:

      logistic propensity   ~500 attributed actions, >=50 positives per feature
                            group, one full season so seasonality is not
                            mistaken for a lift
      uplift modelling      >=2,000 attributed actions AND a genuine holdout —
                            uplift needs contacts deliberately NOT made, which
                            means accepting a control group that is a real cost
      contextual bandit     >=200 positives per arm, >=3 months, and per-tenant
                            fitting: brokerages differ enough that a pooled
                            model would be confidently wrong for the small ones

    Until then a rule that says "email separated from sms, 34/120 against
    9/98" is worth more than a model that says 0.31, because the agent can
    check the first one and cannot check the second.

THE FAILURE THIS AVOIDS
    A learner fitted on a few dozen outcomes will find a difference, because
    noise always contains one. It will then act on it, generating data that
    confirms it, and the confirmation will look like learning. The
    MIN_PER_ARM gate in the evaluator and the absence of a fitted model here
    are the same decision made twice.
"""

from __future__ import annotations

from typing import Any, Optional

from .evaluator import MIN_PER_ARM

#: A channel producing this share of suppressions is doing damage, regardless
#: of its reply rate. Suppression is a person asking to be left alone.
SUPPRESSION_ALARM = 0.10

#: Below this many measured actions, nothing is inferred from a suppression
#: rate either — one opt-out in three sends is not a pattern.
MIN_FOR_SUPPRESSION = 10


def recommendations(progress: dict[str, Any]) -> list[dict[str, Any]]:
    """Rules over what was measured. Each carries the evidence it used.

    Returns [] freely: no recommendation is the correct output for a mission
    that has not yet learned anything, and inventing one would be the whole
    mistake this module is built to avoid.
    """
    out: list[dict[str, Any]] = []
    channels = progress.get("channels") or []
    comparison = progress.get("comparison") or {}

    if comparison.get("verdict") == "separated":
        out.append({
            "rule": "prefer_separated_channel",
            "action": f"weight {comparison['winner']} more heavily",
            "because": comparison.get("sentence"),
            "evidence": comparison.get("evidence"),
        })

    for channel in channels:
        alarm = _suppression_alarm(channel)
        if alarm:
            out.append(alarm)

    goal = progress.get("goal") or {}
    if goal.get("target") and goal.get("achieved", 0) >= goal["target"]:
        out.append({
            "rule": "target_reached",
            "action": "stop the mission",
            "because": (
                f"{goal['achieved']} of {goal['target']} — though counted by "
                f"last touch, so this is what followed the mission's contacts, "
                f"not what it is proven to have caused."
            ),
        })
    return out


def _suppression_alarm(channel: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Opting out is the outcome that matters even at small n.

    A reply rate needs MIN_PER_ARM before it means anything. Suppression is
    different in kind: it is a person asking to stop being contacted, and the
    cost of ignoring it is not a worse conversion rate.
    """
    measured = channel.get("measured") or 0
    suppressed = channel.get("suppressed") or 0
    if measured < MIN_FOR_SUPPRESSION or not suppressed:
        return None
    rate = suppressed / measured
    if rate < SUPPRESSION_ALARM:
        return None
    return {
        "rule": "suppression_alarm",
        "action": f"pause {channel['channel']} on this mission",
        "because": (
            f"{suppressed} of {measured} {channel['channel']} contacts ended in "
            f"someone opting out. That is a rate to act on at any sample size, "
            f"because each one is a person asking to be left alone."
        ),
        "evidence": {k: channel.get(k) for k in ("channel", "measured", "suppressed")},
    }


def what_is_not_fitted() -> dict[str, Any]:
    """Machine-readable version of the docstring, for the UI to show honestly."""
    return {
        "fitted_models": [],
        "method": "ordered rules over measured outcomes",
        "min_per_arm": MIN_PER_ARM,
        "not_built": [
            {"model": "logistic propensity",
             "needs": "~500 attributed actions, >=50 positives per feature group, one season"},
            {"model": "uplift",
             "needs": ">=2,000 attributed actions and a real holdout of contacts NOT made"},
            {"model": "contextual bandit",
             "needs": ">=200 positives per arm, >=3 months, fitted per tenant"},
        ],
        "why": (
            "A learner fitted on a few dozen outcomes finds a difference, "
            "because noise contains one, then acts on it and generates data "
            "that confirms it."
        ),
    }
