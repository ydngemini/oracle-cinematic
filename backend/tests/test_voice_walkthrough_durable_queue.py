"""Walkthrough transcription is queued in the database, audio included.

The route runs on API replicas; transcription runs in the worker container.
The old design staged the audio on the API replica's disk and queued it in
that process's memory — neither of which the worker can reach, so every
walkthrough was accepted and never processed (Mission 8 capacity audit).
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib

import voice_intel

MIGRATION = (pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
             / "0114_voice_walkthrough_jobs.sql").read_text()


def test_the_route_writes_the_audio_into_a_job_row():
    src = inspect.getsource(voice_intel.log_walkthrough)
    assert "INSERT INTO voice_walkthrough_jobs" in src
    assert "put_nowait" not in src
    # Backpressure counts the durable backlog, per tenant (RLS).
    assert "count(*) FROM voice_walkthrough_jobs WHERE status = 'queued'" in src
    # The staged temp file never outlives the request.
    assert "staged.unlink" in src


def test_the_worker_claims_rows_with_skip_locked():
    src = inspect.getsource(voice_intel._claim_next)
    assert "FOR UPDATE SKIP LOCKED" in src and "SET status = 'running'" in src
    assert "_queue" not in inspect.getsource(voice_intel)


def test_terminal_jobs_release_their_audio():
    assert "audio = NULL" in inspect.getsource(voice_intel._finish)
    assert "CHECK (status IN ('queued', 'running') OR audio IS NULL)" in MIGRATION


def test_the_table_is_tenant_isolated_and_usable_by_the_app():
    assert "FORCE ROW LEVEL SECURITY" in MIGRATION
    assert "app_is_platform_admin() OR tenant_id = app_current_tenant()" in MIGRATION
    assert "TO oracle_app" in MIGRATION


class _Conn:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, *args):
        self.sql.append((sql, args))


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __call__(self, ctx):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def test_an_interrupted_job_is_retried_with_its_audio_then_given_up(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(voice_intel, "tenant_tx", _Tx(conn))
    asyncio.run(voice_intel.recover_interrupted_jobs())
    sql, args = conn.sql[0]
    assert "WHERE status = 'running' AND updated_at < $1" in sql
    assert "ELSE 'queued'" in sql          # retried: the audio is still there
    assert args[1] == voice_intel.MAX_ATTEMPTS
