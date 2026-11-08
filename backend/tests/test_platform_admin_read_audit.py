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
from fastapi import FastAPI, WebSocket
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


# ---------------------------------------------------------------------------
# WebSocket subscriptions (L17-17 follow-up, 2026-11-08). BaseHTTPMiddleware
# never sees a WebSocket, so a platform admin's live streams (the main /ws, a
# voice session, the audit trail, observability, surge telemetry) were not
# recorded. Each handler now records one row once the socket is accepted.
# ---------------------------------------------------------------------------

def _ws_app():
    app = FastAPI()

    @app.websocket("/ws/stream/{tenant_id}")
    async def stream(websocket: WebSocket, tenant_id: str):
        from tenancy import require_context

        ctx = require_context(websocket.headers["authorization"])
        await websocket.accept()
        audit_middleware.record_platform_websocket(websocket, ctx)
        await websocket.send_json({"ok": True})
        await websocket.close()

    return TestClient(app)


def test_a_platform_admin_websocket_subscription_is_recorded_once(harness):
    _, recorded = harness
    client = _ws_app()
    url = f"/ws/stream/{OTHER_TENANT}?token=s3cret-value&q=Jane"
    with client.websocket_connect(url, headers=_bearer("platform_admin")) as ws:
        assert ws.receive_json() == {"ok": True}
    reads = _reads(recorded)
    assert len(reads) == 1, recorded
    row = reads[0]
    assert row["category"] is AuditCategory.ADMIN_ACTION
    assert row["action"] == f"platform_read WEBSOCKET /ws/stream/{OTHER_TENANT}"
    assert row["user_id"] == "operator@neoh.test"
    meta = row["metadata"]
    assert meta["method"] == "WEBSOCKET"
    assert meta["route"] == "/ws/stream/{tenant_id}"
    assert meta["target_tenant_id"] == OTHER_TENANT
    # Never the query string: it can carry a token or a search term.
    assert "s3cret" not in repr(row) and "Jane" not in repr(row)

    # A reconnect inside the window is the same subscription.
    with client.websocket_connect(url, headers=_bearer("platform_admin")) as ws:
        ws.receive_json()
    assert len(_reads(recorded)) == 1


@pytest.mark.parametrize("role", ["agent", "broker_owner"])
def test_tenant_users_websockets_are_not_recorded(harness, role):
    _, recorded = harness
    with _ws_app().websocket_connect(
            f"/ws/stream/{OTHER_TENANT}", headers=_bearer(role, tenant=OTHER_TENANT)) as ws:
        ws.receive_json()
    assert _reads(recorded) == []


# Every WebSocket handler that authenticates a user. The provider media
# bridges (/media/acs, /media/twilio, /media/plivo) carry no user identity.
_USER_WEBSOCKETS = [
    ("server.py", "websocket_endpoint"),
    ("server.py", "voice_telemetry_websocket"),
    ("audit_ledger.py", "audit_trail_ws"),
    ("aws_observability.py", "observability_websocket"),
    ("admin_c2.py", "surge_telemetry_ws"),
]


@pytest.mark.parametrize("filename,handler", _USER_WEBSOCKETS)
def test_every_user_websocket_records_a_platform_admin_after_accepting(filename, handler):
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / filename).read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == handler), None)
    assert fn is not None, f"{filename}: no handler {handler}"

    def lines_of(attr):
        return [c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call)
                and isinstance(c.func, (ast.Attribute, ast.Name))
                and getattr(c.func, "attr", getattr(c.func, "id", None)) == attr]

    accepts, records = lines_of("accept"), lines_of("record_platform_websocket")
    assert accepts, f"{filename}:{handler} never accepts"
    assert records, f"{filename}:{handler} does not record platform-admin subscriptions"
    assert min(records) > min(accepts), "record only a subscription that was accepted"


def test_a_websocket_audit_failure_never_breaks_the_stream(monkeypatch):
    """A bare object (no scope, no query_params), then a recorder that raises:
    the handler must carry on either way; the bare object is still recorded."""
    from tenancy import Role, TenantContext

    monkeypatch.setattr(audit_middleware, "_platform_read_last", {})
    recorded: list[dict] = []
    monkeypatch.setattr(audit_middleware, "_spawn_record", lambda **kw: recorded.append(kw))
    ctx = TenantContext(agent_id="operator@neoh.test", tenant_id=PLATFORM_TENANT,
                        role=Role.PLATFORM_ADMIN)

    class Bare:
        pass

    audit_middleware.record_platform_websocket(Bare(), ctx)
    assert len(recorded) == 1 and recorded[0]["metadata"]["target_tenant_id"] == "all"

    def boom(**kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(audit_middleware, "_spawn_record", boom)
    monkeypatch.setattr(audit_middleware, "_platform_read_last", {})
    audit_middleware.record_platform_websocket(Bare(), ctx)  # must not raise
