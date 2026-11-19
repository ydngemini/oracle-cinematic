"""Audit 30.9: a running voice walkthrough bumps updated_at while it works
(a long transcription, or one waiting on the shared whisper lock), so the
media_jobs alert fires only when its worker loop has stopped."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import voice_intel


def test_a_running_walkthrough_heartbeats_until_it_finishes(monkeypatch):
    beats = []

    class Conn:
        async def execute(self, sql, *args):
            beats.append((sql, args))

    @asynccontextmanager
    async def tenant_tx(ctx):
        yield Conn()

    async def slow_job(job):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(voice_intel, "tenant_tx", tenant_tx)
    monkeypatch.setattr(voice_intel, "_run_job", slow_job)
    monkeypatch.setattr(voice_intel, "HEARTBEAT_SECONDS", 0.03)

    async def scenario():
        await voice_intel._run_with_heartbeat(SimpleNamespace(job_id="job-1"))
        done = len(beats)
        await asyncio.sleep(0.1)
        return done

    done = asyncio.run(scenario())
    assert done >= 2, "the job beat while it ran"
    assert len(beats) == done, "the beat stops with the job"
    sql, args = beats[0]
    assert "SET updated_at = now()" in sql and "status = 'running'" in sql and args == ("job-1",)
