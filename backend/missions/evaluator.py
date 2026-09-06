"""Did the mission work? — and the discipline of not answering too early.

Two jobs. `attach_outcomes` binds recorded outcomes to the actions that
plausibly earned them. `progress` reports where the mission stands.

The hard part is the second one, and it is not arithmetic. A mission that has
sent eleven emails and nine texts CAN produce two percentages, and those
percentages will differ, and a person reading them will change their strategy.
At those volumes the difference is noise, and the product would have taught the
agent something false in a way they cannot check.

So: per-channel rates are always Wilson intervals, never point estimates, and
the sentence "this channel is working better" is emitted ONLY when two channels
each have at least MIN_PER_ARM observations and their intervals do not overlap.
Below that the progress report says, in words, that it cannot tell them apart
yet — and shows the raw counts so the agent can see exactly how thin the
evidence is rather than being asked to trust a withheld conclusion.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from agent_twin import wilson_interval
from outcome_memory import NEGATIVE_KINDS
from tenancy import TenantContext

logger = logging.getLogger(__name__)

#: Per channel, before its rate may be compared with another channel's.
#: Not a statistical derivation — a floor chosen so the intervals below are
#: narrow enough that non-overlap means something.
MIN_PER_ARM = 20

#: How long after an action an outcome may still be credited to it. Matches
#: outcome_memory's own reply window; a longer one would let a mission claim
#: credit for a conversation it did not start.
CREDIT_WINDOW_DAYS = 14


async def attach_outcomes(ctx: TenantContext, mission_id: str) -> int:
    """Bind outcomes to the actions that plausibly earned them.

    Last touch, within the window, on the same person — the same rule
    outcome_memory uses, applied to mission actions. Only rows with
    `outcome_event_id IS NULL` are considered, so this is idempotent and can
    run on every tick.
    """
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        # One statement: for each unattributed executed action, the earliest
        # outcome on that person after it and inside the window.
        updated = await conn.fetch(
            """
            WITH candidates AS (
                SELECT a.id AS action_id,
                       (SELECT o.id
                          FROM outcome_events o
                          JOIN mission_candidates c ON c.id = a.candidate_id
                         WHERE o.client_id::text = c.subject_id
                           AND o.occurred_at >= a.updated_at
                           AND o.occurred_at <= a.updated_at
                                                + ($2 || ' days')::interval
                         ORDER BY o.occurred_at
                         LIMIT 1) AS outcome_id
                  FROM mission_actions a
                 WHERE a.mission_id = $1::uuid
                   AND a.outcome_event_id IS NULL
                   AND a.state IN ('executed', 'approved')
            )
            UPDATE mission_actions a
               SET outcome_event_id = c.outcome_id, updated_at = now()
              FROM candidates c
             WHERE a.id = c.action_id AND c.outcome_id IS NOT NULL
            RETURNING a.id
            """,
            mission_id, str(CREDIT_WINDOW_DAYS),
        )
    return len(updated)


async def progress(ctx: TenantContext, mission_id: str) -> dict[str, Any]:
    """Where the mission stands, and what it is not yet entitled to conclude."""
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        mission = await conn.fetchrow(
            "SELECT * FROM missions WHERE id = $1::uuid", mission_id)
        if mission is None:
            return {"error": "mission not found"}
        funnel = await conn.fetch(
            """SELECT state, count(*)::int AS n FROM mission_actions
                WHERE mission_id = $1::uuid GROUP BY state ORDER BY state""",
            mission_id,
        )
        per_channel = await conn.fetch(
            """SELECT a.channel,
                      count(*)::int AS attempted,
                      count(a.outcome_event_id)::int AS with_outcome,
                      count(*) FILTER (
                          WHERE o.outcome_kind IS NOT NULL
                            AND NOT (o.outcome_kind = ANY($2::text[]))
                      )::int AS positive,
                      -- Counted separately from the negatives: opting out is
                      -- the one outcome that matters at any sample size, so
                      -- learning needs it on its own rather than folded into
                      -- a rate.
                      count(*) FILTER (
                          WHERE o.outcome_kind = 'contact_suppressed'
                      )::int AS suppressed
                 FROM mission_actions a
                 LEFT JOIN outcome_events o ON o.id = a.outcome_event_id
                WHERE a.mission_id = $1::uuid
                  AND a.state IN ('executed', 'approved')
                GROUP BY a.channel ORDER BY a.channel""",
            mission_id, sorted(NEGATIVE_KINDS),
        )

    channels = [_channel_row(dict(r)) for r in per_channel]
    comparison = compare_channels(channels)

    return {
        "mission_id": mission_id,
        "objective": mission["objective_text"],
        "status": mission["status"],
        "mode": mission["mode"],
        "goal": _goal(dict(mission), channels),
        "funnel": [{"state": r["state"], "count": r["n"]} for r in funnel],
        "channels": channels,
        "comparison": comparison,
    }


def _channel_row(row: dict[str, Any]) -> dict[str, Any]:
    """One channel's record, as an interval. Never a bare percentage."""
    n = int(row.get("with_outcome") or 0)
    positive = int(row.get("positive") or 0)
    point, low, high = wilson_interval(positive, n)
    return {
        "channel": row["channel"],
        "attempted": int(row.get("attempted") or 0),
        "measured": n,
        "positive": positive,
        "suppressed": int(row.get("suppressed") or 0),
        "rate": point if n else None,
        "low": low if n else None,
        "high": high if n else None,
        "enough_to_judge": n >= MIN_PER_ARM,
    }


def compare_channels(channels: list[dict[str, Any]]) -> dict[str, Any]:
    """Say which channel is working better — or say why that cannot be said.

    Both halves matter. A product that stays silent below the threshold looks
    broken; one that answers anyway teaches something false. This says the
    honest third thing, with the counts attached.
    """
    ready = [c for c in channels if c["enough_to_judge"]]
    if len(ready) < 2:
        thin = ", ".join(
            f"{c['channel']} {c['positive']}/{c['measured']}"
            for c in channels if c["measured"]
        )
        return {
            "verdict": "not_enough_evidence",
            "changed": False,
            "sentence": (
                f"Not enough yet to tell the channels apart ({thin})."
                if thin else
                "No outcomes recorded yet, so no channel can be compared."
            ),
            "needed_per_channel": MIN_PER_ARM,
        }

    ranked = sorted(ready, key=lambda c: c["rate"], reverse=True)
    best, second = ranked[0], ranked[1]
    if best["low"] <= second["high"]:
        # Enough data, and the intervals still overlap. That is a result: the
        # channels are not distinguishable, and saying so prevents a
        # coin-flip difference being read as a finding.
        return {
            "verdict": "indistinguishable",
            "changed": False,
            "sentence": (
                f"{best['channel']} and {second['channel']} cannot be separated: "
                f"{best['channel']} {_pct(best)}, {second['channel']} {_pct(second)} "
                f"— the ranges overlap."
            ),
        }

    return {
        "verdict": "separated",
        "changed": True,
        "winner": best["channel"],
        "sentence": (
            f"{best['channel']} is outperforming {second['channel']}: "
            f"{_pct(best)} against {_pct(second)}, and the ranges do not overlap."
        ),
        "evidence": {
            "winner": {k: best[k] for k in ("channel", "positive", "measured", "low", "high")},
            "runner_up": {k: second[k] for k in ("channel", "positive", "measured", "low", "high")},
        },
    }


def _pct(channel: dict[str, Any]) -> str:
    return (
        f"{channel['positive']}/{channel['measured']} "
        f"({int(channel['low'] * 100)}–{int(channel['high'] * 100)}%)"
    )


def _goal(mission: dict[str, Any], channels: list[dict[str, Any]]) -> dict[str, Any]:
    target = mission.get("target_count")
    achieved = sum(c["positive"] for c in channels)
    return {
        "target": target,
        "achieved": achieved,
        "fraction": round(achieved / target, 3) if target else None,
        # An outcome credited to a mission action is not proof the mission
        # caused it. Last-touch attribution is a convention, not a causal
        # claim, and the number above inherits that.
        "caveat": (
            "Counted by last touch within 14 days — these are outcomes that "
            "followed the mission's contacts, not outcomes it is proven to "
            "have caused."
        ),
    }


async def record_strategy_change(
    ctx: TenantContext, mission_id: str, comparison: dict[str, Any],
) -> bool:
    """Journal a strategy change, once, and only when it is warranted."""
    from db.connection import tenant_tx

    if not comparison.get("changed"):
        return False
    async with tenant_tx(ctx) as conn:
        existing = await conn.fetchval(
            """SELECT 1 FROM mission_events
                WHERE mission_id = $1::uuid AND kind = 'strategy_changed'
                  AND detail->>'winner' = $2
                LIMIT 1""",
            mission_id, comparison.get("winner"),
        )
        if existing:
            return False
        await conn.execute(
            """INSERT INTO mission_events (tenant_id, mission_id, kind, detail)
               VALUES ($1::uuid, $2::uuid, 'strategy_changed', $3::jsonb)""",
            ctx.tenant_id, mission_id,
            json.dumps({
                "winner": comparison.get("winner"),
                "sentence": comparison.get("sentence"),
                "evidence": comparison.get("evidence"),
            }, default=str),
        )
    return True
