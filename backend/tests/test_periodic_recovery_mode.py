"""Recovery mode runs only internal-only periodic tasks (owner 2026-10-14).

A restored or non-production copy must never contact anyone from stale data,
so ORACLE_RECOVERY_MODE kept the whole scheduler off — which also left
plaintext unsealed and the audit chain unverified on staging (L17-03, L17-12).
Tasks that touch only this database now still run there; nothing else does.
"""
import asyncio

import pytest

from data_integrations import periodic


def _sched(monkeypatch, recovery: bool):
    monkeypatch.setattr(periodic, "SCHEDULER_ENABLED", True)
    import recovery_mode
    monkeypatch.setattr(recovery_mode, "is_recovery_mode", lambda: recovery)
    monkeypatch.setattr(periodic.PeriodicScheduler, "_register_job_handler", staticmethod(lambda task: None))
    sched = periodic.PeriodicScheduler(tick_s=5)
    ran = []

    async def fake_run_task(task):
        ran.append(task.name)
        task.schedule_next(float("inf"))

    async def no_beat(_tick):
        return None

    monkeypatch.setattr(sched, "_run_task", fake_run_task)
    monkeypatch.setattr(periodic, "_scheduler_beat", no_beat)

    async def nothing():
        return {}

    sched.register(periodic.PeriodicTask(name="county_harvest", interval_s=60, run=nothing))
    sched.register(periodic.PeriodicTask(name="seal", interval_s=60, run=nothing, internal_only=True))
    return sched, ran


def _one_tick(sched):
    async def go():
        await sched.start()
        if sched._loop_task is None:
            return
        await asyncio.sleep(0.05)
        await sched.stop()
    asyncio.run(go())


def test_recovery_mode_runs_only_internal_tasks(monkeypatch):
    sched, ran = _sched(monkeypatch, recovery=True)
    _one_tick(sched)
    assert ran == ["seal"], "a task that contacts the world ran on a recovery copy"


def test_normal_mode_runs_everything(monkeypatch):
    sched, ran = _sched(monkeypatch, recovery=False)
    _one_tick(sched)
    assert sorted(ran) == ["county_harvest", "seal"]


def test_recovery_mode_with_no_internal_task_stays_off(monkeypatch):
    sched, ran = _sched(monkeypatch, recovery=True)
    sched._tasks.pop("seal")
    _one_tick(sched)
    assert ran == [] and sched._loop_task is None


def test_only_database_only_tasks_are_marked_internal(monkeypatch):
    # The registry is the review point: each internal task must be one that
    # never reaches a provider, a mailbox or a meter.
    monkeypatch.setattr(periodic.PeriodicScheduler, "_register_job_handler", staticmethod(lambda task: None))
    sched = periodic.build_default_scheduler()
    internal = sorted(t.name for t in sched._tasks.values() if t.internal_only)
    assert internal == ["audit_chain_verify", "client_note_seal", "sealed_text_seal"], internal
