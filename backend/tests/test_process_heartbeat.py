"""Worker liveness is part of release health.

The release smoke test used to check only the API. The worker serves no route
and nothing it does is guaranteed to be visible inside a release window — job
leases heartbeat only while a job runs, and the scheduler ticks hourly — so a
release whose worker crashed on boot passed while every background job stopped.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

import process_heartbeat as hb

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _row(sha="abc", age=5, role="worker", up=100):
    return {"role": role, "git_sha": sha, "age_seconds": age, "uptime_seconds": up}


def test_a_fresh_worker_is_healthy():
    s = hb.summarize([_row(age=5)])
    assert s["healthy"] is True and s["live_workers"] == 1
    assert s["live_git_shas"] == ["abc"]


def test_a_worker_past_the_stale_threshold_is_dead():
    s = hb.summarize([_row(age=hb.STALE_AFTER + 1)])
    assert s["healthy"] is False and s["live_workers"] == 0
    # Still listed, so an operator can see WHEN it died — just not counted live.
    assert len(s["workers"]) == 1


def test_one_missed_beat_is_not_death():
    """Stale is four intervals, so a slow query or a pool hiccup never reads as
    a dead worker and fails a release that is actually fine."""
    assert hb.STALE_AFTER >= 3 * hb.INTERVAL
    assert hb.summarize([_row(age=hb.INTERVAL * 2)])["healthy"] is True


def test_no_heartbeats_at_all_is_unhealthy():
    assert hb.summarize([])["healthy"] is False


def test_two_live_releases_are_both_reported():
    """A rollout mid-flight — or stuck — shows as two SHAs, which the smoke
    test treats as a failure once its wait expires."""
    s = hb.summarize([_row(sha="new"), _row(sha="old")])
    assert s["live_git_shas"] == ["new", "old"]


def test_the_endpoint_never_exposes_hostnames():
    """Unauthenticated, like /health. Role, SHA and ages only."""
    s = hb.summarize([{**_row(), "hostname": "secret-host"}])
    assert "hostname" not in s["workers"][0]
    assert "secret-host" not in str(s)


def test_a_failed_beat_never_raises(monkeypatch):
    """A heartbeat that can take its worker down would turn a database blip
    into the very outage it exists to report."""
    class Broken:
        def acquire(self):
            raise RuntimeError("pool exploded")

    import db.connection

    monkeypatch.setattr(db.connection, "get_pool", lambda: Broken())
    assert asyncio.run(hb.beat_once()) is False


def test_the_heartbeat_reports_the_images_own_sha(monkeypatch):
    """Same source as GET /version: the image's ENV, never a runtime value the
    platform injects — the lesson of the app-spec override that made /version
    report intent instead of fact."""
    monkeypatch.setenv("ORACLE_GIT_SHA", "a1b2c3")
    assert hb.git_sha() == "a1b2c3"


def test_started_at_is_process_start_not_first_successful_write():
    """The column default would be the first SUCCESSFUL write, so a worker whose
    early beats failed reported too little uptime. Found on the first local run,
    when the table did not yet exist."""
    assert "started_at" in hb._UPSERT and "$5" in hb._UPSERT
    assert hb.STARTED_AT.tzinfo is not None


def test_only_background_work_processes_beat():
    """Web replicas must not beat as workers, or a dead worker is masked by a
    live API."""
    import server

    src = inspect.getsource(server)
    start = src.index("await start_heartbeat()")
    gate = src.rfind("if config.RUNS_BACKGROUND_WORK:", 0, start)
    assert gate != -1 and start - gate < 400, "start_heartbeat must sit under RUNS_BACKGROUND_WORK"


def test_the_read_query_only_counts_worker_roles():
    assert "role IN ('worker', 'all')" in hb.READ_WORKERS_SQL


def test_the_migration_grants_the_app_role():
    """Bitten four times: 0003 revokes PUBLIC, so a table the app cannot write
    is a heartbeat that silently never beats."""
    sql = (REPO / "backend" / "db" / "migrations" / "0112_process_heartbeats.sql").read_text()
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON process_heartbeats TO oracle_app" in sql


def test_the_migration_is_rollback_safe():
    from migration_safety import ADDITIVE, classify_sql

    sql = (REPO / "backend" / "db" / "migrations" / "0112_process_heartbeats.sql").read_text()
    assert classify_sql("0112", sql).classification == ADDITIVE


def test_the_smoke_test_requires_a_worker_on_this_release():
    smoke = (REPO / "infra" / "digitalocean" / "smoke-test.sh").read_text()
    assert "/health/workers" in smoke
    assert "did not roll over" in smoke, "a previous-release worker must fail the check"
    assert "no live worker" in smoke


def test_a_clean_shutdown_removes_its_own_row(monkeypatch):
    """Only processes that did NOT shut down cleanly should linger as stale
    rows — that is the signal worth keeping. Otherwise every rollout shows two
    releases until the old row ages out."""
    executed = []

    class Conn:
        async def execute(self, sql, *args):
            executed.append((sql, args))

    class Pool:
        def acquire(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _a():
                yield Conn()
            return _a()

    import db.connection

    monkeypatch.setattr(db.connection, "get_pool", lambda: Pool())
    asyncio.run(hb.stop_heartbeat())
    assert any("DELETE FROM process_heartbeats" in sql and args == (hb.PROCESS_ID,)
               for sql, args in executed)
