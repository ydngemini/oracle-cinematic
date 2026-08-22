"""Connecting the harvested market feed to the table that is actually read.

The scheduled Zillow/Redfin/FHFA/HUD sync had been writing fresh state figures
into `public_market_metrics` for as long as it ran, while `state_market_stats` —
what routes_market.py and the get_market_trends tool read — served migration
0025's 2024-10-01 seed. routes_market.py said so in its own docstring: "nothing
refreshes it".

What is worth pinning is not that the projection copies numbers. It is the two
places where copying the obvious thing would have been wrong.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from state_market_projection import (
    UNMAPPED_COLUMNS,
    _EXACT_MAPPING,
    project_state_market_stats,
)


class _Conn:
    def __init__(self, rows):
        self._rows = rows
        self.updates: list[tuple] = []

    async def fetch(self, query, *args):
        return self._rows

    async def execute(self, query, *args):
        self.updates.append((query, args))
        return "UPDATE 1"


def _obs(state, metric, value, period=date(2026, 5, 31)):
    return {
        "state_code": state, "metric_key": metric, "value": value,
        "period_end": period, "source_url": "https://redfin.example/tracker.tsv",
        "retrieved_at": datetime(2026, 8, 20, 9, 20, tzinfo=timezone.utc),
        "source_key": "redfin_market_tracker",
    }


def _full_state(state="DE"):
    return [
        _obs(state, "median_list_price", 408300),
        _obs(state, "median_sale_price", 384500),
        _obs(state, "median_days_on_market", 33),
        _obs(state, "months_of_supply", 2.3),
        _obs(state, "active_inventory", 1728),
    ]


def _args_of(conn):
    query, args = conn.updates[0]
    return query, args


# ---------------------------------------------------------------------------
# The two judgement calls
# ---------------------------------------------------------------------------

def test_as_of_is_the_period_described_not_the_fetch_time():
    """Redfin publishes a calendar month roughly three months later. Using
    retrieved_at would have claimed 0 days old for data describing May."""
    conn = _Conn(_full_state())
    asyncio.run(project_state_market_stats(conn))

    _, args = _args_of(conn)
    assert date(2026, 5, 31) in args, "as_of_date is not the period_end"
    fetched = datetime(2026, 8, 20, 9, 20, tzinfo=timezone.utc)
    assert fetched in args, "source_fetched_at was not recorded separately"
    # The two are 81 days apart — conflating them overstates freshness.
    assert (fetched.date() - date(2026, 5, 31)).days > 75


def test_columns_whose_source_answers_a_different_question_are_nulled():
    """avg_price_per_sqft is fed by a MEDIAN; closed_sales_last_30d by a
    calendar month. A consumer reads the column name, never the provenance."""
    conn = _Conn(_full_state())
    asyncio.run(project_state_market_stats(conn))

    # Whitespace-normalised: the SQL is column-aligned, so matching exact
    # spacing pins formatting rather than behaviour.
    query = " ".join(_args_of(conn)[0].split())
    for column in UNMAPPED_COLUMNS:
        assert f"{column} = NULL" in query, column
    for column in _EXACT_MAPPING:
        assert f"{column} = NULL" not in query, f"{column} should be filled"


def test_the_unmapped_columns_carry_their_reason_as_data():
    """A comment explaining the gap does not reach the caller; this does."""
    assert set(UNMAPPED_COLUMNS) == {
        "avg_price_per_sqft", "closed_sales_last_30d",
        "list_to_sale_ratio", "yoy_price_change_pct"}
    assert "MEDIAN" in UNMAPPED_COLUMNS["avg_price_per_sqft"]
    assert "calendar-month" in UNMAPPED_COLUMNS["closed_sales_last_30d"]
    assert not set(UNMAPPED_COLUMNS) & set(_EXACT_MAPPING), (
        "a column cannot be both exactly mapped and deliberately unmapped"
    )


def test_stale_values_are_cleared_rather_than_left_beside_fresh_ones():
    """A row's as_of_date has to apply to every figure in it. Leaving the 2024
    seed in four columns under a 2026 date is worse than a gap."""
    conn = _Conn(_full_state())
    result = asyncio.run(project_state_market_stats(conn))

    query, _ = _args_of(conn)
    assert "NULL" in query
    assert result["columns_filled"] == sorted(_EXACT_MAPPING)


# ---------------------------------------------------------------------------
# Provenance and degradation
# ---------------------------------------------------------------------------

def test_a_projected_row_is_marked_machine_harvested():
    """Machine-harvested market data must not be indistinguishable from a
    figure a person verified."""
    conn = _Conn(_full_state())
    asyncio.run(project_state_market_stats(conn))

    query, args = _args_of(conn)
    assert "verification_status   = 'machine'" in query
    assert "redfin_market_tracker" in args
    assert "https://redfin.example/tracker.tsv" in args


def test_an_empty_feed_leaves_the_table_alone_and_says_why():
    """The alternative — wiping 51 rows because a download failed — replaces
    stale data with none."""
    conn = _Conn([])
    result = asyncio.run(project_state_market_stats(conn))

    assert result["updated"] == 0
    assert not conn.updates, "an empty feed still issued an UPDATE"
    assert "no state-level observations" in result["skipped_reason"]


def test_a_state_missing_every_mapped_metric_is_skipped():
    conn = _Conn([_obs("DE", "new_listings", 900)])  # harvested, but unmapped
    result = asyncio.run(project_state_market_stats(conn))

    assert result["updated"] == 0
    assert not conn.updates


def test_every_state_present_in_the_feed_is_refreshed():
    conn = _Conn(_full_state("DE") + _full_state("IL") + _full_state("CA"))
    result = asyncio.run(project_state_market_stats(conn))

    assert result["states_seen"] == 3
    assert result["updated"] == 3


def test_only_the_preferred_source_is_read():
    """Zillow and Redfin both publish a home-value figure; mixing two
    publishers' definitions into one row makes the row mean nothing."""
    conn = _Conn(_full_state())
    asyncio.run(project_state_market_stats(conn))

    assert conn.updates
    result = asyncio.run(project_state_market_stats(_Conn(_full_state())))
    assert result["source"] == "redfin_market_tracker"
