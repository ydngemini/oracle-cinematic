"""Regression coverage for the tenant-scoped Slot-2 portfolio summary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import portfolio_api
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(
    agent_id="agent@tenant.test",
    tenant_id=TENANT_ID,
    role=Role.PLATFORM_ADMIN,
)


class _SummaryConn:
    def __init__(self):
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls = 0
        self.fetch_calls = 0

    def _record(self, query: str, args: tuple[object, ...]) -> None:
        self.queries.append((query, args))

    async def fetchrow(self, query, *args):
        self._record(query, args)
        self.fetchrow_calls += 1
        if self.fetchrow_calls == 1:
            return {
                "active_volume": Decimal("2450000.00"),
                "transactions_under_contract": 12,
            }
        return {"response_rate": Decimal("88.5")}

    async def fetchval(self, query, *args):
        self._record(query, args)
        return 2

    async def fetch(self, query, *args):
        self._record(query, args)
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            return [
                {
                    "id": "client-1",
                    "full_name": "John Doe",
                    "client_type": "seller",
                    "stage": "active",
                    "last_contacted_at": datetime.now(timezone.utc),
                    "hours_silent": Decimal("94"),
                }
            ]
        if self.fetch_calls == 2:
            return [
                {"party_role": "seller", "status": "under_contract", "count": 3},
                {"party_role": "buyer", "status": "closed", "count": 4},
            ]
        return []


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(received_ctx):
        assert received_ctx == CTX
        yield conn

    return tx


def test_summary_is_explicitly_tenant_scoped_for_platform_admin(monkeypatch):
    conn = _SummaryConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(portfolio_api.portfolio_summary(CTX))

    assert result["tenant_id"] == TENANT_ID
    assert result["metrics"] == {
        "active_contracts": 12,
        "total_volume": 2450000.0,
        "response_rate_30d": 88.5,
        "ghosting_alerts_count": 1,
    }
    assert result["ghosting_clients"][0]["last_contact_hours"] == 94
    assert result["milestone_breakdown"]["sellers"]["under_contract"] == 3
    assert result["milestone_breakdown"]["buyers"]["closed"] == 4

    assert len(conn.queries) == 9
    for query, args in conn.queries:
        assert "tenant_id=$1::uuid" in query
        assert args == (TENANT_ID,)
    combined_sql = "\n".join(query for query, _ in conn.queries)
    assert "FROM leads" not in combined_sql


class _TitleFlagConn(_SummaryConn):
    """The summary with one title finding awaiting review."""

    async def fetch(self, query, *args):
        if "FROM title_findings" in query:
            self._record(query, args)
            return [{
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "property_key": "DE-NCC-0142",
                "finding_type": "lien",
                "review_status": "required",
                "match_status": "possible_match",
                "record_id": "2024-001234",
                "amount": Decimal("12500.00"),
                "recorded_at": date(2024, 3, 1),
                "released_at": None,
            }]
        return await super().fetch(query, *args)


def test_a_title_flag_carries_what_a_reviewer_decides_on(monkeypatch):
    """Confirm / Dismiss act on one finding by id (T11.03, F11.04), and the
    reviewer sees the record's evidence and the submitter's match claim: a
    confirmed possible match still reads "Possible match" to the homeowner."""
    conn = _TitleFlagConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(portfolio_api.portfolio_summary(CTX))

    [flag] = [f for f in result["intelligence_flags"] if f["type"] == "TITLE"]
    assert flag == {
        "type": "TITLE",
        "property_key": "DE-NCC-0142",
        "label": "lien",
        "review_status": "required",
        "finding_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "match_status": "possible_match",
        "record_id": "2024-001234",
        "amount": 12500.0,
        "recorded_at": "2024-03-01",
        "released_at": None,
    }
    title_sql = next(q for q, _ in conn.queries if "FROM title_findings" in q)
    # Only findings still awaiting a person's decision are listed; no notes.
    assert "review_status='required'" in title_sql
    assert "notes" not in title_sql


class _PulseConn(_SummaryConn):
    """The summary with a middleware audit row and a decision audit row."""

    LEAD = "11111111-2222-4333-8444-555555555555"

    async def fetch(self, query, *args):
        if "FROM audit_ledger" in query:
            self._record(query, args)
            return [
                {"event_id": "e1", "category": "USER_STATE_CHANGE",
                 "action": f"POST /api/crm/houses/{self.LEAD}/record-match", "target_id": self.LEAD,
                 "metadata": {"path": f"/api/crm/houses/{self.LEAD}/record-match", "client_ip": "203.0.113.7"},
                 "created_at": datetime(2026, 10, 17, tzinfo=timezone.utc)},
                {"event_id": "e2", "category": "user_state_change", "action": "property_match_confirmed",
                 "target_id": "99999999-8888-4777-8666-555555555555", "metadata": {"decision": "confirmed"},
                 "created_at": datetime(2026, 10, 17, tzinfo=timezone.utc)},
            ]
        return await super().fetch(query, *args)


def test_the_activity_pulse_carries_only_what_the_page_shows(monkeypatch):
    """Every agent of the tenant reads this pulse, including agents walled
    off from a record by 0143; the ledger's metadata (paths, client IPs,
    record ids) and target ids stay out, and ids inside a logged request
    path are masked (review L11-15)."""
    conn = _PulseConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    pulse = asyncio.run(portfolio_api.portfolio_summary(CTX))["activity_pulse"]

    assert [set(entry) for entry in pulse] == [{"event_id", "category", "action", "created_at"}] * 2
    assert pulse[0]["action"] == "POST /api/crm/houses/:id/record-match"
    assert pulse[1]["action"] == "property_match_confirmed"
    dumped = json.dumps(pulse, default=str)
    assert _PulseConn.LEAD not in dumped and "203.0.113.7" not in dumped and "99999999" not in dumped
    (sql,) = [q for q, _ in conn.queries if "FROM audit_ledger" in q]
    assert "metadata" not in sql and "target_id" not in sql
