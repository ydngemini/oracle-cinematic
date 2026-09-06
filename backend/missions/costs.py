"""What a mission's plan costs, in money and in the agent's time.

Both numbers are STATED, not measured. The unit costs are published list
prices and the minutes are estimates; nothing here has watched this brokerage
send a single message. That is why every figure this module produces travels
with the label that says so — a simulation that reads like a forecast is worse
than no simulation, because it is the number a person will repeat to their
broker.
"""

from __future__ import annotations

from typing import Any

#: Published list prices, in cents, per message. Sources are the providers'
#: own pricing pages; they are not this tenant's negotiated rates and they do
#: not include carrier surcharges.
CHANNEL_UNIT_COSTS_CENTS: dict[str, int] = {
    "email": 0,     # within the plan's included volume
    "sms": 1,       # ~$0.0079 + carrier fees, rounded up to a whole cent
    "voice": 4,     # per-minute origination, one minute assumed
    "task": 0,      # a task costs time, not money — see ACTION_MINUTES
}

#: Minutes of a person's attention each action still costs even when the
#: system does the work: reading the draft, deciding, handling the reply.
ACTION_MINUTES: dict[str, float] = {
    "email": 1.5,
    "sms": 1.0,
    "voice": 6.0,
    "task": 5.0,
}

COST_BASIS = (
    "Published list prices and estimated minutes, not this brokerage's own "
    "rates or measured handling time."
)


def unit_cost_cents(channel: str) -> int:
    return CHANNEL_UNIT_COSTS_CENTS.get(channel, 0)


def action_minutes(channel: str) -> float:
    return ACTION_MINUTES.get(channel, 1.0)


def cost_of(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Total the plan. Channels are counted, never averaged."""
    by_channel: dict[str, dict[str, Any]] = {}
    for action in actions or []:
        channel = action.get("channel") or "task"
        row = by_channel.setdefault(
            channel, {"channel": channel, "count": 0, "cents": 0, "minutes": 0.0},
        )
        row["count"] += 1
        row["cents"] += unit_cost_cents(channel)
        row["minutes"] += action_minutes(channel)

    return {
        "total_cents": sum(r["cents"] for r in by_channel.values()),
        "total_minutes": round(sum(r["minutes"] for r in by_channel.values()), 1),
        "by_channel": [by_channel[k] for k in sorted(by_channel)],
        "basis": COST_BASIS,
    }
