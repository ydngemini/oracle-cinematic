"""GET /version — the release this process is actually running.

A deploy script needs this to confirm the right image is live before running
a smoke test, and to know what to roll back TO. Never :latest as the answer.
"""

from __future__ import annotations

import asyncio

import server


def test_reports_the_baked_release_identity(monkeypatch):
    monkeypatch.setenv("ORACLE_GIT_SHA", "abc1234")
    monkeypatch.setenv("ORACLE_APP_VERSION", "2026.09.21")
    monkeypatch.setenv("ORACLE_BUILD_TIMESTAMP", "2026-09-21T21:00:00Z")
    monkeypatch.setattr(server, "get_pool", lambda: None)

    response = asyncio.run(server.version())
    import json
    body = json.loads(response.body)

    assert body["git_sha"] == "abc1234"
    assert body["app_version"] == "2026.09.21"
    assert body["built_at"] == "2026-09-21T21:00:00Z"
    assert body["migration_head"] is None
    assert body["process_role"] == server.config.PROCESS_ROLE


def test_defaults_are_honest_not_a_lie_by_omission(monkeypatch):
    """A manual `docker build` with no --build-arg must not silently claim a
    fake version — "unknown" is the correct answer, not a stale/guessed one."""
    monkeypatch.delenv("ORACLE_GIT_SHA", raising=False)
    monkeypatch.delenv("ORACLE_APP_VERSION", raising=False)
    monkeypatch.delenv("ORACLE_BUILD_TIMESTAMP", raising=False)
    monkeypatch.setattr(server, "get_pool", lambda: None)

    response = asyncio.run(server.version())
    import json
    body = json.loads(response.body)

    assert body["git_sha"] == "unknown"
    assert body["app_version"] == "unknown"
    assert body["built_at"] == "unknown"


def test_migration_head_reads_live_from_the_database_not_a_baked_value(monkeypatch):
    """Migrations can be applied independently of which image build is
    running — a baked migration head would go stale the moment that happens."""
    calls = []

    class _FakeConn:
        async def fetchval(self, query):
            calls.append(query)
            return "0105_mission_digest_sent_event.sql"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakePool:
        def acquire(self):
            return _FakeConn()

    monkeypatch.setattr(server, "get_pool", lambda: _FakePool())

    import json
    response = asyncio.run(server.version())
    body = json.loads(response.body)
    assert body["migration_head"] == "0105_mission_digest_sent_event.sql"
    assert calls and "schema_migrations" in calls[0]
