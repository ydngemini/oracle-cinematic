"""The Command Center — the state of the business, and what to do about it.

The first screen of a CRM is normally a dashboard: counts of things, in boxes,
in an order chosen at design time. It answers "how many" for six metrics an
agent did not ask about, and leaves "what should I do now" to them.

This assembles the other thing. Three sections, in the order a person actually
needs them:

  CHANGED   what moved since they last looked — a diff, not a total, because a
            number that was already 47 yesterday is not news
  ATTENTION the ranked opportunities, valued, so the ordering means something
  HORIZON   the same work laid out in time, so "later" is visible rather than
            silently dropped off the bottom of a list

Two rules hold throughout. Every count is a link to the rows behind it, never a
bare figure. And a section with nothing in it says *why* it is empty —
`opportunity_engine.perception_coverage` distinguishes a quiet week from an
uninstrumented one, and that distinction is the difference between an agent
trusting a calm screen and an agent assuming the product is broken.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import expected_value
import opportunity_engine
from db.connection import tenant_tx
from tenancy import TenantContext

logger = logging.getLogger("oracle.command_center")

#: The client-originated signal types, as installed by 0095. Named here rather
#: than derived from intent_states.SIGNAL_WEIGHTS because that mapping also
#: carries staff-side types like email_open, and a "what the client did" digest
#: that counts our own sends is measuring the brokerage, not the market.
BEHAVIOURAL_TYPES: tuple[str, ...] = (
    "listing_view", "listing_favorite", "listing_unfavorite", "listing_share",
    "search", "saved_search", "calculator_use", "showing_request",
    "availability_view", "map_view",
)

#: "While you were away" defaults to a day. Longer and the digest stops being a
#: diff; shorter and a normal night's sleep produces an empty briefing.
DEFAULT_LOOKBACK_HOURS = 24

#: Horizon buckets, in hours from now. `WATCHING` is deliberately unbounded —
#: it holds the things with no date at all, which are exactly the things a
#: due-date-sorted task list loses.
_HORIZONS: tuple[tuple[str, str, Optional[float]], ...] = (
    ("now",       "Now",           2.0),
    ("today",     "Today",         24.0),
    ("this_week", "This week",     168.0),
    ("this_month","Next 30 days",  720.0),
    ("watching",  "Watching",      None),
)


async def _changed_since(ctx: TenantContext, since: datetime) -> dict[str, Any]:
    """What actually moved. Every figure here is a delta over `since`."""
    async with tenant_tx(ctx) as conn:
        new_clients = await conn.fetchval(
            "SELECT count(*) FROM clients WHERE tenant_id = app_current_tenant() "
            "AND created_at >= $1 AND archived_at IS NULL", since)

        # Behavioural events are the headline of any "what changed" digest —
        # when there are any. Counted by type so the summary can name the
        # behaviour rather than reporting an undifferentiated total.
        behaviour = await conn.fetch(
            """
            SELECT interaction_type, count(*) AS n
              FROM interaction_logs
             WHERE tenant_id = app_current_tenant() AND created_at >= $1
               AND interaction_type = ANY($2::text[])
          GROUP BY interaction_type ORDER BY n DESC
            """,
            since, list(BEHAVIOURAL_TYPES),
        )

        # The AI's own work, from the execution ledger written by 0087. This is
        # the "Neoh handled 37 things" line, and it is drawn from the same rows
        # that make tool calls replay-safe — so it is a record of what actually
        # committed, not a count of what was attempted.
        handled = await conn.fetch(
            """
            SELECT tool_name, count(*) AS n
              FROM ai_tool_operations
             WHERE tenant_id = app_current_tenant() AND user_id = app_current_agent()
               AND status = 'completed' AND created_at >= $1
          GROUP BY tool_name ORDER BY n DESC LIMIT 8
            """,
            since,
        )

        # Beliefs that changed their mind about something. The most interesting
        # single line in a morning briefing, when it is non-empty.
        new_disputes = await conn.fetchval(
            "SELECT count(*) FROM beliefs WHERE tenant_id = app_current_tenant() "
            "AND revision_state = 'disputed' AND recorded_at >= $1", since)

    behaviour_total = sum(r["n"] for r in behaviour)
    handled_total = sum(r["n"] for r in handled)

    return {
        "since": since.isoformat(),
        "new_clients": new_clients or 0,
        "behavioural_events": behaviour_total,
        "behavioural_breakdown": {r["interaction_type"]: r["n"] for r in behaviour},
        "handled_automatically": handled_total,
        "handled_breakdown": {r["tool_name"]: r["n"] for r in handled},
        "new_contradictions": new_disputes or 0,
    }


def _horizon_of(opportunity: dict[str, Any], now: datetime) -> str:
    """Place an opportunity in time using its own deadline, if it has one.

    Opportunities without a deadline land in `watching` rather than being
    guessed into `today`. A fabricated urgency is worse than an admitted
    absence of one: it trains the agent to ignore the urgent bucket.
    """
    deadline = opportunity.get("deadline") or opportunity.get("due_at")
    if not deadline:
        return "watching"
    try:
        when = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "watching"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    hours = (when - now).total_seconds() / 3600.0
    for key, _label, ceiling in _HORIZONS:
        if ceiling is not None and hours <= ceiling:
            return key
    return "watching"


async def _market_median(ctx: TenantContext) -> Optional[float]:
    """A median sale price to value opportunities that carry no deal value.

    Read from state_market_stats, which 0079 made provenanced and projected —
    so this is a real published figure with a known lag, not a constant someone
    typed. Returns None when the tenant's markets are not covered, and the
    valuation then declines to produce a number at all.
    """
    try:
        async with tenant_tx(ctx) as conn:
            return await conn.fetchval(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY median_sale_price)
                  FROM state_market_stats
                 WHERE median_sale_price IS NOT NULL AND median_sale_price > 0
                """
            )
    except Exception:
        logger.warning("market median unavailable; opportunities will be unvalued",
                       exc_info=True)
        return None


async def briefing(
    ctx: TenantContext, *, lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict[str, Any]:
    """Everything the first screen needs, in one round trip."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    scan = await opportunity_engine.scan(ctx)
    changed = await _changed_since(ctx, since)
    median = await _market_median(ctx)

    opportunities = scan.get("opportunities", [])
    valued: list[expected_value.ValuedAction] = []
    for opportunity in opportunities:
        action = expected_value.value_of(
            kind=opportunity.get("kind", ""),
            confidence=float(opportunity.get("confidence", 0.0)),
            deal_value=opportunity.get("value_signal"),
            action_type=opportunity.get("action_type", "call"),
            market_median=median,
        )
        # Attached to the opportunity, not held alongside it: the number and the
        # thing it values must travel together or the UI will pair them wrongly.
        opportunity["economics"] = action.as_dict() if action else None
        if action:
            valued.append(action)

    horizons = {key: [] for key, _label, _c in _HORIZONS}
    for opportunity in opportunities:
        horizons[_horizon_of(opportunity, now)].append(opportunity)

    return {
        "generated_at": now.isoformat(),
        "changed": changed,
        "attention": {
            "opportunities": opportunities,
            "portfolio": expected_value.portfolio(valued),
        },
        "horizon": [
            {"key": key, "label": label, "items": horizons[key]}
            for key, label, _c in _HORIZONS
        ],
        # Carried through verbatim so the UI can explain an empty screen without
        # a second request — and so a quiet week never renders identically to a
        # missing capture path.
        "perception": scan.get("perception", {}),
        "suppressed_low_confidence": scan.get("suppressed_low_confidence", 0),
        "detectors_failed": scan.get("detectors_failed", []),
    }
