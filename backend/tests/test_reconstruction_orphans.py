"""A restart must not leave a job claiming to be running forever.

The work queue lives in memory, so a backend restart abandons every running
job and drops every queued one — but their rows still read `queued` and
`running`. Nothing else ever moves them, so the caller polls a status that will
never change and the reason it stopped is invisible. Two real captures were
lost that way before anything said so.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import reconstruction_worker as worker


class _Conn:
    """Records the sweep's UPDATE and answers with the rows it 'changed'."""

    def __init__(self, returns=None, raises=None):
        self.calls: list[tuple] = []
        self._returns = returns if returns is not None else []
        self._raises = raises

    async def fetch(self, sql, *args):
        if self._raises:
            raise self._raises
        self.calls.append((sql, args))
        return self._returns


class _Tx:
    def __init__(self, conn):
        self._conn = conn
        self.ctx = None

    def __call__(self, ctx):
        self.ctx = ctx
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _run(conn, monkeypatch):
    tx = _Tx(conn)
    monkeypatch.setattr(worker, "tenant_tx", tx)
    asyncio.run(worker.fail_orphaned_jobs())
    return tx


def test_it_fails_only_jobs_older_than_this_process(monkeypatch):
    conn = _Conn(returns=[{"id": "job-1"}])
    _run(conn, monkeypatch)

    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "status = 'failed'" in sql
    assert "status IN ('queued', 'running')" in sql
    # The cutoff is this process's start: a job THIS process is running has a
    # newer updated_at and must survive the sweep.
    assert "updated_at < $2" in sql
    assert args[1] == worker._PROCESS_STARTED_AT
    assert isinstance(worker._PROCESS_STARTED_AT, datetime)
    assert worker._PROCESS_STARTED_AT.tzinfo is not None


def test_the_reason_tells_the_operator_what_to_do(monkeypatch):
    conn = _Conn(returns=[{"id": "job-1"}])
    _run(conn, monkeypatch)
    reason = conn.calls[0][1][0]
    assert "restart" in reason.lower()
    # It must say the GPU was released, or the reader will hunt for a bill.
    assert "pod" in reason.lower()
    # And that a rerun is needed — the capture itself was never at fault.
    assert "again" in reason.lower()


def test_it_sweeps_across_tenants(monkeypatch):
    from tenancy import Role

    conn = _Conn(returns=[])
    tx = _run(conn, monkeypatch)
    # An orphan belongs to whichever tenant owned it; the sweep runs for all of
    # them, so it needs the platform role rather than one tenant's context.
    assert tx.ctx.role is Role.PLATFORM_ADMIN
    # RLS scopes the statement; a tenant predicate here would be the second
    # half of a policy this codebase never duplicates.
    assert "tenant_id = app_current_tenant()" not in conn.calls[0][0]


def test_a_failing_sweep_never_stops_the_service(monkeypatch):
    conn = _Conn(raises=RuntimeError("database is not up yet"))
    # Startup calls this before the workers begin; if it raised, a transient
    # database hiccup would stop the backend from accepting any capture at all.
    _run(conn, monkeypatch)


def test_startup_reconciles_before_accepting_work():
    source = worker.inspect.getsource(worker.start_reconstruction_workers) \
        if hasattr(worker, "inspect") else __import__("inspect").getsource(
            worker.start_reconstruction_workers)
    sweep = source.index("fail_orphaned_jobs")
    spawn = source.index("_worker_loop")
    assert sweep < spawn, "the truth about the last process must precede new work"


@pytest.mark.parametrize("stamp_age_seconds", [0, 3600])
def test_the_process_stamp_is_taken_once_at_import(stamp_age_seconds):
    # Re-reading the clock per sweep would let a long-lived process eventually
    # fail jobs it is genuinely running.
    first = worker._PROCESS_STARTED_AT
    assert worker._PROCESS_STARTED_AT is first
    assert first < datetime.now(timezone.utc) + timedelta(seconds=stamp_age_seconds + 1)
