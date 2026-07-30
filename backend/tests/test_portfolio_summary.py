"""Regression coverage for the tenant-scoped Slot-2 portfolio summary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
