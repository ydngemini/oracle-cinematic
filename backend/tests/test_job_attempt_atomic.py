"""A job's terminal state and its attempt audit commit together (JOB-01).

complete_job / fail_job updated the job in one transaction and closed the
attempt row in another, so a crash between them left a finished job with an
open attempt (or the reverse) and the audit no longer matched the job.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import automation_jobs

JOB = {"id": "11111111-1111-4111-8111-111111111111", "lease_token": "22222222-2222-4222-8222-222222222222",
       "attempt_count": 1, "max_attempts": 3, "job_type": "test"}


def _db(monkeypatch, *, lease_held=True, finish_fails=False):
    transactions = []

    class Conn:
        def __init__(self):
            self.statements = []

        async def fetchrow(self, sql, *args):
            self.statements.append("update")
            return {"id": JOB["id"]} if lease_held else None

        async def fetchval(self, sql, *args):
            self.statements.append("finish")
            if finish_fails:
                raise OSError("connection lost")
            return True

    @asynccontextmanager
    async def tx(_ctx):
        conn = Conn()
        transactions.append(conn.statements)
        yield conn

    monkeypatch.setattr(automation_jobs.dbc, "tenant_tx", tx)
    return transactions


def test_completion_and_its_attempt_share_one_transaction(monkeypatch):
    txs = _db(monkeypatch)
    asyncio.run(automation_jobs.complete_job(JOB, "w1", {"ok": True}))
    assert txs == [["update", "finish"]]


def test_failure_and_its_attempt_share_one_transaction(monkeypatch):
    txs = _db(monkeypatch)
    asyncio.run(automation_jobs.fail_job(JOB, "w1", RuntimeError("boom")))
    assert txs == [["update", "finish"]]


def test_an_attempt_that_cannot_be_closed_fails_the_whole_completion(monkeypatch):
    """The transaction rolls back, so the job is not left 'succeeded' with an
    open attempt; the lease lapses and the job is reclaimed."""
    _db(monkeypatch, finish_fails=True)
    with pytest.raises(OSError):
        asyncio.run(automation_jobs.complete_job(JOB, "w1", {"ok": True}))


def test_a_lost_lease_closes_no_attempt(monkeypatch):
    txs = _db(monkeypatch, lease_held=False)
    with pytest.raises(automation_jobs.JobLeaseLost):
        asyncio.run(automation_jobs.complete_job(JOB, "w1", {"ok": True}))
    assert txs == [["update"]]
