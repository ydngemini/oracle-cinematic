"""Reconstruction jobs are claimed from their rows, not an in-process queue.

In production the POST lands on an API replica (ORACLE_PROCESS_ROLE=web),
which runs no reconstruction consumer. With the old asyncio.Queue every
capture sat in that replica's memory forever while its row read "queued"
(Mission 8 capacity audit). These tests pin the replacement.
"""

from __future__ import annotations

import asyncio
import inspect

import reconstruction_worker as worker
import tour_api
from tenancy import Role


class _ClaimConn:
    def __init__(self, row, role="broker_owner"):
        self.row, self.role, self.sql = row, role, []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return self.row

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return self.role


class _Tx:
    def __init__(self, conn):
        self.conn, self.ctxs = conn, []

    def __call__(self, ctx):
        self.ctxs.append(ctx)
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def test_the_claim_is_an_atomic_skip_locked_update_of_the_oldest_queued_row(monkeypatch):
    row = {"id": "j1", "tenant_id": "t1", "lead_id": "l1", "listing_id": None, "created_by": "a@b.test"}
    conn = _ClaimConn(row)
    monkeypatch.setattr(worker, "tenant_tx", _Tx(conn))
    job = asyncio.run(worker._claim_next())
    claim = conn.sql[0]
    assert "FOR UPDATE SKIP LOCKED" in claim
    assert "status = 'queued'" in claim and "SET status = 'running'" in claim
    assert "ORDER BY created_at" in claim
    # The job runs as its submitter, in the submitter's tenant, at their role.
    assert (job.job_id, job.ctx.tenant_id, job.ctx.agent_id) == ("j1", "t1", "a@b.test")
    assert job.ctx.role is Role.BROKER_OWNER


def test_an_empty_queue_claims_nothing(monkeypatch):
    monkeypatch.setattr(worker, "tenant_tx", _Tx(_ClaimConn(None)))
    assert asyncio.run(worker._claim_next()) is None


def test_an_unknown_role_runs_with_least_privilege(monkeypatch):
    row = {"id": "j1", "tenant_id": "t1", "lead_id": None, "listing_id": "x", "created_by": "gone@b.test"}
    monkeypatch.setattr(worker, "tenant_tx", _Tx(_ClaimConn(row, role=None)))
    assert asyncio.run(worker._claim_next()).ctx.role is Role.AGENT


def test_the_worker_loop_takes_work_from_the_database_only():
    src = inspect.getsource(worker._worker_loop)
    assert "_claim_next()" in src
    assert "_queue" not in inspect.getsource(worker)


def test_the_route_admits_by_counting_queued_rows_not_by_a_memory_queue():
    src = inspect.getsource(tour_api.enqueue_reconstruction)
    assert "count(*) FROM reconstruction_jobs WHERE status = 'queued'" in src
    assert "QueueFull" not in src
    # The row is committed before the (optional) local wake-up.
    assert src.index("INSERT INTO reconstruction_jobs") < src.index("enqueue(")


def test_the_stale_status_constraint_is_dropped():
    # 0101 added the wider CHECK under a new name; the old one still vetoed
    # 'failed_quality_gate' until 0115.
    import pathlib
    mig = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
    assert "DROP CONSTRAINT IF EXISTS chk_recon_status" in (mig / "0115_recon_status_constraint.sql").read_text()
    assert "'failed_quality_gate'" in (mig / "0101_reconstruction_diagnostics.sql").read_text()
