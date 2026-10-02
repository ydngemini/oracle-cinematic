"""Handlers are called as handler(payload, reporter); the job row is
reporter.job. Three handlers once treated their first argument as the job and
would have failed every run in production (caught 2026-10-02)."""

from __future__ import annotations

import asyncio


class Reporter:
    def __init__(self, tenant_id="11111111-1111-1111-1111-111111111111"):
        self.job = {"id": "j1", "tenant_id": tenant_id, "payload": {}}

    async def progress(self, *a, **k):
        return None


def test_email_outbox_handler_reads_tenant_from_the_job(monkeypatch):
    import email_outbox

    seen = {}

    async def deliver(tenant_id, outbox_id):
        seen.update(tenant=tenant_id, outbox=outbox_id)
        return {"status": "sent"}

    monkeypatch.setattr(email_outbox, "deliver", deliver)
    asyncio.run(email_outbox._job({"outbox_id": "o1"}, Reporter()))
    assert seen == {"tenant": "11111111-1111-1111-1111-111111111111", "outbox": "o1"}


def test_privacy_handlers_take_the_payload(monkeypatch):
    import privacy_export
    import privacy_lifecycle

    calls = []

    async def run_erasure(op):
        calls.append(("erase", op))
        return {"state": "succeeded", "operation_id": op}

    async def build_export(op):
        calls.append(("export", op))
        return {"state": "succeeded"}

    monkeypatch.setattr(privacy_lifecycle, "run_erasure", run_erasure)
    monkeypatch.setattr(privacy_export, "build_export", build_export)
    asyncio.run(privacy_lifecycle._erase_job({"operation_id": "e1"}, Reporter()))
    asyncio.run(privacy_export._export_job({"operation_id": "x1"}, Reporter()))
    assert calls == [("erase", "e1"), ("export", "x1")]


def test_every_registered_handler_takes_payload_and_reporter():
    import inspect

    import automation_jobs
    import server  # noqa: F401  — registers every handler

    for name, handler in automation_jobs._HANDLERS.items():
        params = list(inspect.signature(handler).parameters)
        assert len(params) == 2, (name, params)
        assert params[0] != "job", f"{name}: first argument is the payload, not the job"
