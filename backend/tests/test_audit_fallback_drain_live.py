"""Audit fallback drain and the scheduled chain verifier against real
PostgreSQL (L17-12, migration 0148; skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Proves:
- rows written to the SQLite fallback while Postgres was unavailable are
  appended to the PG chain in order, keep their time and content, and are
  deleted locally; a second drain of the same rows appends nothing;
- the bounded verifier walks the chain from a cursor, persists it, and reports
  (and alerts once on) a forged link; the forged row is rolled back;
- audit_chain_verification is invisible and unwritable to a request
  connection (only the platform login sees it).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_fallback_rows_drain_into_the_pg_chain_and_the_verifier_catches_a_forgery(tmp_path, monkeypatch):
    asyncio.run(_scenario(tmp_path, monkeypatch))


async def _scenario(tmp_path, monkeypatch):
    import asyncpg

    import audit_ledger
    from audit_ledger import AuditCategory, AuditLedger
    from db import connection
    from tenancy import Role, TenantContext

    alerts = []

    async def _notify(subject, body):
        alerts.append(subject)

    import ops_alerts

    monkeypatch.setattr(ops_alerts, "_notify", _notify)
    action = f"live-drain-{uuid.uuid4().hex[:12]}"
    ledger = AuditLedger(sqlite_path=str(tmp_path / "fallback.db"))
    # 1. Postgres "down": no pool yet, so record() writes to SQLite.
    assert audit_ledger._dbc.get_pool() is None
    tenant = str(uuid.uuid4())
    for i in range(3):
        await ledger.record(AuditCategory.ADMIN_ACTION, action, tenant_id=tenant if i else "not-a-uuid",
                            user_id="live-drain@example.test", metadata={"i": i})
    pending, _anchor = ledger._sqlite_batch(10)
    assert len(pending) == 3 and ledger._fallback_pending

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    try:
        # 2. Postgres back: drain.
        result = await ledger.drain_fallback()
        assert result["drained"] == 3 and result["remaining"] == 0 and result["chain_intact"] is True
        assert ledger._fallback_pending is False and ledger._sqlite_batch(10)[0] == []
        rows = await admin.fetch(
            "SELECT seq, created_at, tenant_id, metadata, prev_hash, entry_hash FROM audit_ledger "
            "WHERE action = $1 ORDER BY seq", action)
        assert len(rows) == 3
        metas = [json.loads(r["metadata"]) for r in rows]
        assert [m["i"] for m in metas] == [0, 1, 2]
        assert [m["_fallback"]["entry_hash"] for m in metas] == [p[9] for p in pending]
        assert [r["created_at"].isoformat() for r in rows] == [p[1] for p in pending]
        assert rows[0]["tenant_id"] is None and metas[0]["_fallback"]["tenant_id"] == "not-a-uuid"
        assert str(rows[1]["tenant_id"]) == tenant
        assert rows[1]["prev_hash"] == rows[0]["entry_hash"] and rows[2]["prev_hash"] == rows[1]["entry_hash"]

        # 3. A crash between the PG commit and the SQLite delete: the same rows
        #    again append nothing.
        await ledger._append_fallback_rows(pending, True)
        assert await admin.fetchval("SELECT count(*) FROM audit_ledger WHERE action = $1", action) == 3

        # 4. The verifier, from a cursor just before our rows (in a rolled-back
        #    transaction, so the shared dev chain is never left forged).
        before = await admin.fetchrow(
            "SELECT seq, entry_hash FROM audit_ledger WHERE seq < $1 ORDER BY seq DESC LIMIT 1", rows[0]["seq"])
        tx = admin.transaction()
        await tx.start()
        try:
            await admin.execute(
                "INSERT INTO audit_chain_verification (id, cursor_seq, cursor_hash, rows_in_pass) "
                "VALUES (1, $1, $2, 0) ON CONFLICT (id) DO UPDATE SET cursor_seq = EXCLUDED.cursor_seq, "
                "cursor_hash = EXCLUDED.cursor_hash, rows_in_pass = 0, broken_seq = NULL, "
                "alerted_broken_seq = NULL", before["seq"], before["entry_hash"])
            ok, alert = await ledger._verify_segment_on(admin, max_rows=3, page=2)
            assert ok["checked"] == 3 and ok["status"] == "intact" and alert is False
            state = await admin.fetchrow("SELECT cursor_seq, cursor_hash FROM audit_chain_verification")
            assert state["cursor_seq"] == rows[2]["seq"] and state["cursor_hash"] == rows[2]["entry_hash"]
            forged_seq = await admin.fetchval(
                "INSERT INTO audit_ledger (category, action, metadata, prev_hash, entry_hash) "
                "VALUES ('ADMIN_ACTION', $1, '{}'::jsonb, $2, $3) RETURNING seq",
                action + "-forged", "f" * 64, "e" * 64)
            prior = await admin.fetchrow(
                "SELECT seq, entry_hash FROM audit_ledger WHERE seq < $1 ORDER BY seq DESC LIMIT 1", forged_seq)
            await admin.execute("UPDATE audit_chain_verification SET cursor_seq = $1, cursor_hash = $2",
                                prior["seq"], prior["entry_hash"])
            broken, alert = await ledger._verify_segment_on(admin, max_rows=1, page=1)
            assert broken["status"] == "broken" and broken["broken_seq"] == forged_seq and alert is True
            again, alert_again = await ledger._verify_segment_on(admin, max_rows=1, page=1)
            assert alert_again is False                      # one alert per break
        finally:
            await tx.rollback()
        assert await admin.fetchval("SELECT count(*) FROM audit_ledger WHERE action = $1", action + "-forged") == 0

        # 5. Through the app's platform pool (FORCE RLS, platform login): bounded
        #    and persisted.
        result = await ledger.verify_chain_segment(max_rows=500, page=250)
        assert result["checked"] <= 500 and result["status"] in {"intact", "broken"}
        assert await admin.fetchval("SELECT last_run_at IS NOT NULL FROM audit_chain_verification")

        # 6. A request connection can neither see nor move the cursor.
        ctx = TenantContext(agent_id="live-drain@example.test", tenant_id=tenant, role=Role.BROKER_OWNER)
        async with connection.tenant_tx(ctx) as conn:
            assert await conn.fetchval("SELECT count(*) FROM audit_chain_verification") == 0
            assert await conn.execute("UPDATE audit_chain_verification SET cursor_seq = 0") == "UPDATE 0"
    finally:
        await admin.execute("DELETE FROM audit_chain_verification")
        await connection.close_pool()
        await admin.close()
