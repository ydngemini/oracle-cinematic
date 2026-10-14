"""A platform admin's reads of tenant data are audited (ISO 27001 A.8.2/A.8.15, L17-17).

platform_admin bypasses tenant isolation, and the audit middleware recorded
mutations only — so an operator could read every brokerage's clients and leave
no trace. Now each platform-admin GET writes one ledger row (who, which tenant,
which route, when), with no query string and no response content; identical
repeats inside the window coalesce, so a polling dashboard cannot flood the
ledger. Nobody else's reads are recorded (their own tenant's data is theirs).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import audit_middleware
import auth
from audit_ledger import AuditCategory

PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"
OTHER_TENANT = "33333333-3333-3333-3333-333333333333"
CLIENT_ID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def harness(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(audit_middleware, "_spawn_record", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(audit_middleware, "_platform_read_last", {})

    app = FastAPI()
    app.add_middleware(audit_middleware.AuditMiddleware)

    @app.get("/api/clients/{client_id}")
    async def one_client(client_id: str):
        return {"full_name": "Jane Customer", "phone": "+13025550100"}

    @app.get("/api/admin/tenants/{tenant_id}/usage")
    async def tenant_usage(tenant_id: str):
        return {"calls": 3}

    @app.get("/api/clients")
    async def list_clients():
        return [{"full_name": "Jane Customer"}, {"full_name": "John Customer"}]

    @app.get("/auth/session")
    async def session():
        return {"ok": True}

    @app.post("/api/clients")
    async def create_client():
        return {"ok": True}

    return TestClient(app), recorded


def _bearer(role: str, tenant: str = PLATFORM_TENANT, sub: str = "operator@neoh.test") -> dict:
    return {"Authorization": "Bearer " + auth._issue_jwt(sub, tenant, role)}


def _reads(recorded):
    return [r for r in recorded if r["action"].startswith("platform_read ")]


def test_a_platform_admin_read_of_a_tenant_record_is_recorded_once(harness):
    client, recorded = harness

    response = client.get(f"/api/clients/{CLIENT_ID}?search=Jane", headers=_bearer("platform_admin"))

    assert response.status_code == 200
    rows = _reads(recorded)
    assert len(rows) == 1
    row = rows[0]
    assert row["category"] is AuditCategory.ADMIN_ACTION
    assert row["user_id"] == "operator@neoh.test"
    assert row["tenant_id"] == PLATFORM_TENANT
    meta = row["metadata"]
    assert meta["route"] == "/api/clients/{client_id}"
    assert meta["path"] == f"/api/clients/{CLIENT_ID}"
    assert meta["target_tenant_id"] == "all"
    assert meta["status_code"] == 200
    # Identifiers only: no query string, no response content.
    flat = repr(row)
    assert "search" not in flat and "Jane" not in flat and "+1302" not in flat


def test_a_route_that_names_the_tenant_records_that_tenant(harness):
    client, recorded = harness

    client.get(f"/api/admin/tenants/{OTHER_TENANT}/usage", headers=_bearer("platform_admin"))
    client.get("/api/clients", params={"tenant_id": OTHER_TENANT}, headers=_bearer("platform_admin"))

    targets = [r["metadata"]["target_tenant_id"] for r in _reads(recorded)]
    assert targets == [OTHER_TENANT, OTHER_TENANT]


def test_one_row_per_request_not_per_record_and_polling_coalesces(harness):
    client, recorded = harness
    headers = _bearer("platform_admin")

    for _ in range(5):
        client.get("/api/clients", headers=headers)
    client.get(f"/api/clients/{CLIENT_ID}", headers=headers)

    paths = [r["metadata"]["path"] for r in _reads(recorded)]
    # The list returned two clients and was polled five times: one row. A read
    # of a different record is a different path and is never coalesced away.
    assert paths == ["/api/clients", f"/api/clients/{CLIENT_ID}"]


def test_the_window_expiring_records_the_read_again(monkeypatch):
    monkeypatch.setattr(audit_middleware, "_platform_read_last", {})
    window = audit_middleware._PLATFORM_READ_WINDOW_SECONDS

    assert audit_middleware._platform_read_is_new("op", "/api/clients", 1000.0)
    assert not audit_middleware._platform_read_is_new("op", "/api/clients", 1000.0 + window - 1)
    assert audit_middleware._platform_read_is_new("op", "/api/clients", 1000.0 + window + 1)
    # Another operator reading the same path is their own read.
    assert audit_middleware._platform_read_is_new("op2", "/api/clients", 1000.0 + window + 2)


@pytest.mark.parametrize("role", ["broker_owner", "agent"])
def test_tenant_users_reads_are_not_recorded(harness, role):
    client, recorded = harness

    client.get(f"/api/clients/{CLIENT_ID}", headers=_bearer(role, OTHER_TENANT, "agent@b.test"))

    assert _reads(recorded) == []


def test_anonymous_and_sign_in_reads_are_not_recorded(harness):
    client, recorded = harness

    client.get(f"/api/clients/{CLIENT_ID}")
    client.get("/auth/session", headers=_bearer("platform_admin"))

    assert _reads(recorded) == []


def test_a_platform_admin_mutation_is_still_recorded_as_a_mutation(harness):
    client, recorded = harness

    client.post("/api/clients", headers=_bearer("platform_admin"))

    assert _reads(recorded) == []
    assert [r["action"] for r in recorded] == ["POST /api/clients"]


def test_the_coalescing_state_is_bounded(monkeypatch):
    monkeypatch.setattr(audit_middleware, "_platform_read_last", {})
    monkeypatch.setattr(audit_middleware, "_MAX_ANOMALY_KEYS", 10)

    for index in range(50):
        assert audit_middleware._platform_read_is_new("op", f"/api/clients/{index}", 100.0)

    assert len(audit_middleware._platform_read_last) <= 10
