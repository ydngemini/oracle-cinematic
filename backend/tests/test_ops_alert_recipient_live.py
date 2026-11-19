"""An incident first seen with no alert recipient is sent once one is set
(audit 30.3), on real PostgreSQL: notified_at is a delivery, not a log line.
Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_unsent_incident_is_delivered_after_a_recipient_is_set(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import ops_alerts

    name = f"test-30-3-{uuid.uuid4().hex[:8]}"
    comp = {"state": "UNAVAILABLE", "summary": "drill: nothing answers"}
    sent = []

    async def relay(subject, body, *, to=None):
        sent.append((subject, to or ops_alerts._recipient()))
        return None

    async def row(conn):
        return await conn.fetchrow(
            "SELECT notified_at, notify_error FROM ops_alerts WHERE component = $1 AND resolved_at IS NULL", name)

    conn = await asyncpg.connect(DSN)
    try:
        monkeypatch.delenv("ORACLE_ALERT_EMAIL", raising=False)
        await ops_alerts._record(conn, {name: comp}, {name: comp}, {})
        first = await row(conn)
        assert first["notified_at"] is None and first["notify_error"] == ops_alerts.RECIPIENT_MISSING
        await ops_alerts._record(conn, {name: comp}, {name: comp}, {})
        assert (await row(conn))["notified_at"] is None

        monkeypatch.setenv("ORACLE_ALERT_EMAIL", "ops@example.test")
        monkeypatch.setattr(ops_alerts, "send_email", relay)
        await ops_alerts._record(conn, {name: comp}, {name: comp}, {})
        delivered = await row(conn)
        assert delivered["notified_at"] is not None and delivered["notify_error"] is None
        assert sent == [(f"[Neoh] {name} UNAVAILABLE", "ops@example.test")]
        await ops_alerts._record(conn, {name: comp}, {name: comp}, {})
        assert len(sent) == 1, "one incident, one email"
    finally:
        await conn.execute("DELETE FROM ops_alerts WHERE component = $1", name)
        await conn.close()
