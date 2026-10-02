"""Expiring old audit rows must leave the chain verifiable — and must not
make a tampered chain look intact.

privacy_expire_audit (0122) deletes a prefix of the chain and records the
entry_hash of the last removed row in audit_chain_checkpoints;
verify_chain() then starts from that anchor instead of the genesis zeros.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import audit_ledger


def _chain(n: int) -> list[dict]:
    rows, prev = [], "0" * 64
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        created = start + timedelta(days=i)
        meta = json.dumps({"i": i}, separators=(",", ":"), sort_keys=True)
        entry = audit_ledger._compute_hash(created.isoformat(), "ADMIN_ACTION", f"a{i}", "t", "u", "", meta, prev)
        rows.append({"created_at": created, "category": "ADMIN_ACTION", "action": f"a{i}", "tenant_id": "t",
                     "user_id": "u", "target_id": "", "metadata": meta, "prev_hash": prev, "entry_hash": entry})
        prev = entry
    return rows


def _verify(monkeypatch, rows, anchor):
    class Conn:
        async def fetch(self, sql, *a):
            return rows

        async def fetchval(self, sql, *a):
            return anchor

    @asynccontextmanager
    async def fake_conn():
        yield Conn()

    monkeypatch.setattr(audit_ledger, "_global_chain_conn", fake_conn)
    monkeypatch.setattr(audit_ledger._dbc, "get_pool", lambda: object())
    return asyncio.run(audit_ledger.ledger.verify_chain())


def test_full_chain_verifies_from_genesis(monkeypatch):
    assert _verify(monkeypatch, _chain(5), None) is True


def test_expired_prefix_verifies_from_its_anchor(monkeypatch):
    rows = _chain(6)
    assert _verify(monkeypatch, rows[3:], rows[2]["entry_hash"]) is True


def test_expired_prefix_without_its_anchor_fails(monkeypatch):
    rows = _chain(6)
    assert _verify(monkeypatch, rows[3:], None) is False


def test_tampering_after_the_anchor_is_still_caught(monkeypatch):
    rows = _chain(6)
    kept = [dict(r) for r in rows[3:]]
    kept[1]["action"] = "rewritten"
    assert _verify(monkeypatch, kept, rows[2]["entry_hash"]) is False


def test_a_wrong_anchor_is_caught(monkeypatch):
    rows = _chain(6)
    assert _verify(monkeypatch, rows[3:], rows[1]["entry_hash"]) is False
