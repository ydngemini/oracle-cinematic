"""Project harvested market observations into `state_market_stats`.

The scheduled Zillow/Redfin/FHFA/HUD sync has been writing fresh state-level
figures into `public_market_metrics` for as long as it has run. `state_market_stats`
— the table `routes_market.py` and the `get_market_trends` agent tool actually
read — has been serving migration 0025's static seed the whole time, every row
dated 2024-10-01. The route's own docstring says "nothing refreshes it". This is
the thing that refreshes it.

**Only columns that map exactly are filled.** Four are deliberately left NULL,
because the available source answers a *different question* than the column name
asks, and a consumer reading `avg_price_per_sqft` will never see the provenance
note explaining that it is really a median:

    avg_price_per_sqft      Redfin publishes a MEDIAN price per sqft. A median
                            written under a column named `avg` is a quiet
                            misstatement that survives into every API response.
    closed_sales_last_30d   `homes_sold` is a calendar-month count
                            (period_end 2026-05-31), not a rolling 30 days.
    list_to_sale_ratio      Dividing median sale by median list gives a ratio of
                            medians, which is not the median of ratios.
    yoy_price_change_pct    No year-over-year series is harvested. FHFA's HPI is
                            an index, not a percentage change.

They are NULLed rather than left holding seed values, because a row's
`as_of_date` has to apply to every figure in it — a mix of 2026 and 2024 values
under one date is worse than a gap.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("oracle.state_market_projection")

# Column in state_market_stats <- metric_key in public_market_metrics.
# Every pair here is an exact match of both statistic and unit.
_EXACT_MAPPING: dict[str, str] = {
    "median_list_price": "median_list_price",
    "median_sale_price": "median_sale_price",
    "median_days_on_market": "median_days_on_market",
    "months_of_supply": "months_of_supply",
    "active_listings": "active_inventory",
}

# Filled by nothing, on purpose. Kept as data so the reason travels with the
# result instead of living only in a comment.
UNMAPPED_COLUMNS: dict[str, str] = {
    "avg_price_per_sqft": (
        "the harvested figure is a MEDIAN price per sqft; writing it under a "
        "column named 'avg' would misstate the statistic"
    ),
    "closed_sales_last_30d": (
        "homes_sold is a calendar-month count, not a rolling 30-day window"
    ),
    "list_to_sale_ratio": (
        "sale/list of two medians is a ratio of medians, not the median of ratios"
    ),
    "yoy_price_change_pct": (
        "no year-over-year series is harvested; FHFA HPI is an index level"
    ),
}

_PREFERRED_SOURCE = "redfin_market_tracker"


async def project_state_market_stats(conn) -> dict[str, Any]:
    """Refresh state_market_stats from the newest harvested observations.

    Returns a report rather than raising: this runs inside a scheduled task
    whose other work should not be lost because a projection found nothing.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (state_code, metric_key)
               state_code, metric_key, value, period_end, source_url,
               retrieved_at, source_key
          FROM public_market_metrics
         WHERE geography_type = 'state'
           AND state_code IS NOT NULL
           AND metric_key = ANY($1::text[])
           AND source_key = $2
         ORDER BY state_code, metric_key, period_end DESC NULLS LAST, retrieved_at DESC
        """,
        list(_EXACT_MAPPING.values()), _PREFERRED_SOURCE,
    )
    if not rows:
        return {
            "updated": 0,
            "skipped_reason": (
                f"no state-level observations from {_PREFERRED_SOURCE!r} are "
                f"loaded; state_market_stats keeps whatever it already had"
            ),
        }

    by_state: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_state.setdefault(row["state_code"], {"metrics": {}})
        entry["metrics"][row["metric_key"]] = row["value"]
        # as_of_date is the period the figures DESCRIBE, never when they were
        # fetched. Redfin publishes with roughly a three-month lag, so using
        # retrieved_at would claim a freshness the data does not have.
        period = row["period_end"]
        if period and (entry.get("period") is None or period > entry["period"]):
            entry["period"] = period
        entry.setdefault("source_url", row["source_url"])
        fetched = row["retrieved_at"]
        if fetched and (entry.get("fetched") is None or fetched > entry["fetched"]):
            entry["fetched"] = fetched

    inverse = {metric: column for column, metric in _EXACT_MAPPING.items()}
    updated = 0
    for state_code, entry in sorted(by_state.items()):
        values = {inverse[k]: v for k, v in entry["metrics"].items() if k in inverse}
        if not values:
            continue
        result = await conn.execute(
            """
            UPDATE state_market_stats
               SET median_list_price     = $2,
                   median_sale_price     = $3,
                   median_days_on_market = $4,
                   months_of_supply      = $5,
                   active_listings       = $6,
                   -- Cleared, not carried over: a row's as_of_date has to
                   -- apply to every figure in it, and these four have no
                   -- exact source. See UNMAPPED_COLUMNS.
                   avg_price_per_sqft    = NULL,
                   closed_sales_last_30d = NULL,
                   list_to_sale_ratio    = NULL,
                   yoy_price_change_pct  = NULL,
                   as_of_date            = $7,
                   source_key            = $8,
                   source_url            = $9,
                   source_fetched_at     = $10,
                   verification_status   = 'machine',
                   updated_at            = now()
             WHERE state_code = $1
            """,
            state_code,
            values.get("median_list_price"),
            values.get("median_sale_price"),
            values.get("median_days_on_market"),
            values.get("months_of_supply"),
            values.get("active_listings"),
            entry.get("period"),
            _PREFERRED_SOURCE,
            entry.get("source_url"),
            entry.get("fetched"),
        )
        if result and result.endswith(" 1"):
            updated += 1

    report = {
        "updated": updated,
        "states_seen": len(by_state),
        "columns_filled": sorted(_EXACT_MAPPING),
        "columns_left_null": UNMAPPED_COLUMNS,
        "source": _PREFERRED_SOURCE,
    }
    logger.info(
        "state_market_stats projection: %d/%d states refreshed from %s",
        updated, len(by_state), _PREFERRED_SOURCE,
    )
    return report
