"""The geocode backfill as a scheduled task rather than a babysat script.

Only 4.3% of 8.59M public records carried a coordinate, so the map had nothing
to plot and the radius comps search could see 4% of the corpus. The Census batch
geocoder is keyless and free — and intermittent: it returned 89.9% on Delaware,
refused every request minutes later, then recovered.

`run()` stops on an outage by design rather than walking its cursor over rows it
never asked about. That is correct, and it is also why the work has to be
periodic: otherwise "stopped partway" is something an operator has to notice.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from data_integrations import periodic


def test_the_task_is_registered_and_on_by_default(monkeypatch):
    monkeypatch.setenv("ORACLE_INGEST_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    scheduler = periodic.build_default_scheduler()
    tasks = scheduler._tasks
    tasks = list(tasks.values()) if isinstance(tasks, dict) else list(tasks)

    task = next((t for t in tasks if t.name == "property_coordinate_backfill"), None)
    assert task is not None, "the geocode backfill must be scheduled, not run by hand"
    assert task.enabled
    assert task.interval_s == 3600


def test_a_pass_is_small_and_never_dry(monkeypatch):
    """Each pass converges a little. Nothing here should be the reason a free
    public endpoint has a bad day."""
    seen = {}

    async def _fake_run(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(
        periodic, "_property_coordinate_backfill_task",
        periodic._property_coordinate_backfill_task,
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "backfill_property_coordinates",
        type("m", (), {"run": staticmethod(_fake_run)}),
    )

    outcome = asyncio.run(periodic._property_coordinate_backfill_task())

    assert outcome["exit_code"] == 0
    assert seen["dry_run"] is False
    assert seen["limit_batches"] == 4
    assert seen["batch_size"] == 2000
    # The scheduler IS the retry; a preflight here would cost an extra request
    # to a service that may already be struggling, to learn what run() reports.
    assert seen["skip_preflight"] is True


def test_an_outage_is_partial_not_a_failure(monkeypatch):
    """run() exits 1 both when the geocoder is down and when there is nothing
    left to do. Neither deserves an alert; the next pass settles which."""
    async def _stopped(**_kwargs):
        return 1

    monkeypatch.setitem(
        __import__("sys").modules,
        "backfill_property_coordinates",
        type("m", (), {"run": staticmethod(_stopped)}),
    )

    outcome = asyncio.run(periodic._property_coordinate_backfill_task())

    assert outcome["exit_code"] == 1
    assert outcome["_terminal_state"] == "partial"


@pytest.mark.parametrize(
    "env,expected_batches,expected_size",
    [
        ({"ORACLE_GEOCODE_BATCHES_PER_RUN": "50"}, 20, 2000),   # clamped up
        ({"ORACLE_GEOCODE_BATCHES_PER_RUN": "0"}, 1, 2000),     # clamped down
        ({"ORACLE_GEOCODE_BATCH_SIZE": "99999"}, 4, 10_000),    # API limit
        ({"ORACLE_GEOCODE_BATCH_SIZE": "1"}, 4, 100),           # floor
    ],
)
def test_operator_overrides_are_clamped(monkeypatch, env, expected_batches, expected_size):
    """A typo in an env var must not turn a courteous job into a hammer."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = {}

    async def _fake_run(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setitem(
        __import__("sys").modules,
        "backfill_property_coordinates",
        type("m", (), {"run": staticmethod(_fake_run)}),
    )

    asyncio.run(periodic._property_coordinate_backfill_task())

    assert seen["limit_batches"] == expected_batches
    assert seen["batch_size"] == expected_size


def test_a_missing_backfill_module_is_skipped_not_crashed(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "backfill_property_coordinates", None)
    outcome = asyncio.run(periodic._property_coordinate_backfill_task())
    assert "skipped" in outcome
