"""The SQLite audit fallback is bounded and does not keep personal data
indefinitely (L17-12, docs/privacy-data-map.md §7). No database here; the
drain into PostgreSQL and the scheduled chain verifier run against real
PostgreSQL in tests/test_audit_fallback_drain_live.py."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import audit_ledger
from audit_ledger import AuditCategory, AuditLedger


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_ledger._dbc, "get_pool", lambda: None)
    alerts = []
    monkeypatch.setattr(audit_ledger, "_alert_audit_loss", lambda kind, n: alerts.append((kind, n)))
    instance = AuditLedger(sqlite_path=str(tmp_path / "audit.db"))
    instance.alerts = alerts
    return instance


def _record(ledger, n=1, **meta):
    for i in range(n):
        asyncio.run(ledger.record(AuditCategory.LOGIN, "POST /auth/login", user_id="jane@example.test",
                                  metadata={"client_ip": "203.0.113.9", "i": i, **meta}))


def _rows(ledger):
    conn = sqlite3.connect(ledger._sqlite_path)
    try:
        return conn.execute("SELECT id, created_at FROM audit_logs ORDER BY id").fetchall()
    finally:
        conn.close()


def test_without_postgres_rows_go_to_sqlite_and_are_marked_pending(ledger):
    assert ledger._fallback_pending is False
    _record(ledger, 3)
    assert len(_rows(ledger)) == 3 and ledger._fallback_pending is True
    assert ledger._sqlite_verify_chain() is True


def test_the_fallback_is_capped_and_a_refused_write_is_alerted(ledger, monkeypatch):
    monkeypatch.setenv("ORACLE_AUDIT_SQLITE_MAX_ROWS", "100")
    _record(ledger, 100)
    _record(ledger, 2)
    assert len(_rows(ledger)) == 100
    assert ledger.alerts == [("LOST: SQLite fallback full", 1)] * 2


def test_rows_past_the_age_limit_are_purged_with_an_alert_and_the_rest_still_verify(ledger, monkeypatch):
    _record(ledger, 4)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    conn = sqlite3.connect(ledger._sqlite_path)
    conn.execute("UPDATE audit_logs SET created_at = ? WHERE id <= 2", (old,))
    conn.commit()
    conn.close()
    # The rows were re-dated after hashing, so recompute nothing: purge looks
    # only at age, then the remaining two must chain from the stored anchor.
    result = asyncio.run(ledger.drain_fallback())
    assert result == {"skipped": "no database pool", "purged": 2}
    assert len(_rows(ledger)) == 2
    assert ledger.alerts and "purged after 30 days" in ledger.alerts[0][0] and ledger.alerts[0][1] == 2
    assert ledger._sqlite_verify_chain() is True
    # The chain continues from the remaining head, not from genesis.
    _record(ledger, 1)
    assert ledger._sqlite_verify_chain() is True


def test_a_fully_released_fallback_restarts_from_its_anchor_and_vacuums(ledger):
    _record(ledger, 3)
    rows, anchor = ledger._sqlite_batch(10)
    assert anchor == "0" * 64 and audit_ledger._fallback_rows_intact(rows, anchor)
    assert ledger._sqlite_release(rows[-1][0], rows[-1][9], len(rows)) == 0
    assert _rows(ledger) == []
    _record(ledger, 1)
    rows2, anchor2 = ledger._sqlite_batch(10)
    assert anchor2 == rows[-1][9] and rows2[0][8] == anchor2
    assert ledger._sqlite_verify_chain() is True


def test_a_tampered_fallback_row_is_detected(ledger):
    _record(ledger, 2)
    conn = sqlite3.connect(ledger._sqlite_path)
    conn.execute("UPDATE audit_logs SET action = 'nothing to see' WHERE id = 1")
    conn.commit()
    conn.close()
    rows, anchor = ledger._sqlite_batch(10)
    assert audit_ledger._fallback_rows_intact(rows, anchor) is False


def test_pg_failure_logs_the_error_type_not_its_text(ledger, monkeypatch, caplog):
    import logging

    class _Boom(Exception):
        pass

    def _conn():
        raise _Boom("DETAIL: Key (agent_id)=(jane@example.test) already exists")

    monkeypatch.setattr(audit_ledger._dbc, "get_pool", lambda: object())
    monkeypatch.setattr(audit_ledger, "_global_chain_conn", _conn)
    with caplog.at_level(logging.WARNING, logger="oracle.audit"):
        _record(ledger, 1)
    assert "_Boom" in caplog.text and "jane@" not in caplog.text
    assert len(_rows(ledger)) == 1


def test_the_verifier_is_scheduled():
    from data_integrations.periodic import build_default_scheduler

    sched = build_default_scheduler()
    task = sched._tasks["audit_chain_verify"]
    assert task.enabled and task.interval_s >= 300


def test_uuid_columns_never_block_a_drain():
    assert audit_ledger._uuid_or_none("not-a-uuid") is None
    assert audit_ledger._uuid_or_none("") is None
    u = "3FA85F64-5717-4562-B3FC-2C963F66AFA6"
    assert audit_ledger._uuid_or_none(u) == u.lower()
