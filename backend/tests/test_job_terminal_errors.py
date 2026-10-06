"""An error that retrying cannot fix is dead-lettered on its first attempt.

Staging, 2026-10-06: an approved text refused before any carrier request
("connect and finish setting up text messages…") was retried five times, then
dead-lettered — and had texting been set up meanwhile, a message the agent was
told "not sent" would have gone out minutes later, unannounced.
"""

from __future__ import annotations

import asyncio

import pytest

import automation_jobs
from command_providers import ProviderConfigurationError


def _one_iteration(monkeypatch, exc: Exception) -> dict:
    seen: dict = {}
    workers = automation_jobs.DurableJobWorkers(worker_count=1, interactive_count=0)
    job = {"id": "j-1", "job_type": "test:boom", "payload": {}, "attempt_count": 1,
           "max_attempts": 5, "tenant_id": "t", "lease_token": "l"}
    calls = {"n": 0}

    async def claim(worker_id, queue_name="default"):
        calls["n"] += 1
        if calls["n"] > 1:
            workers._running = False
            return None
        return job

    async def handler(payload, reporter):
        raise exc

    async def fail_job(j, worker_id, e, *, error_code="JOB_HANDLER_ERROR", terminal=False):
        seen.update(error_code=error_code, terminal=terminal, exc=e)

    async def noop(*a, **k):
        return None

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(automation_jobs, "claim_next_job", claim)
    monkeypatch.setattr(automation_jobs, "fail_job", fail_job)
    monkeypatch.setattr(automation_jobs, "mark_running", noop)
    monkeypatch.setattr(automation_jobs, "heartbeat_job", noop)
    monkeypatch.setattr(automation_jobs, "_keep_lease", noop)
    monkeypatch.setitem(automation_jobs._HANDLERS, "test:boom", handler)
    monkeypatch.setattr(automation_jobs.asyncio, "sleep", no_sleep)
    workers._running = True
    asyncio.run(workers._loop("w-1"))
    return seen


def test_a_configuration_error_is_terminal_on_the_first_attempt(monkeypatch):
    seen = _one_iteration(monkeypatch, ProviderConfigurationError("set up texting first"))
    assert seen["terminal"] is True and seen["error_code"] == "NOT_CONFIGURED"


def test_an_ordinary_error_still_retries(monkeypatch):
    seen = _one_iteration(monkeypatch, RuntimeError("transient"))
    assert seen["terminal"] is False and seen["error_code"] == "JOB_HANDLER_ERROR"


def test_the_refused_text_is_a_configuration_error():
    import inspect

    import commands_api

    src = inspect.getsource(commands_api)
    at = src.index("Text message blocked: connect and finish setting up")
    assert "raise ProviderConfigurationError(" in src[at - 120:at]
