"""Operator functions stay operator-only (prompt 17 AC17.07, F17.11).

Every route behind admin_ops.require_platform_admin is called, through the
real application with CSRF satisfied, as a broker owner and as an agent of a
brokerage: each must answer 403 "Platform admin only." and nothing else. The
route list is read from the application's dependency graph, so a new operator
route is covered the day it is mounted. A platform admin passing the same gate
is the positive control: its answer is never that 403.

Every /api/admin or /admin route must also make an explicit choice: either it
is behind the platform-admin gate, or it is listed below as a route a broker
owner may use, with the reason.
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import admin_ops
import auth
import server

TENANT = "33333333-3333-4333-8333-333333333333"
ANY_ID = "44444444-4444-4444-8444-444444444444"
CSRF = "csrf-test-token-0123456789abcdef"
DENIED = {"detail": "Platform admin only."}

# /api/admin routes a broker owner may call on purpose: brokerage-scoped
# governance under require_broker_owner_or_admin (role-change approvals, the
# permission policy, the brokerage's own anomaly feed).
BROKER_OWNER_ADMIN_ROUTES = {
    ("GET", "/api/admin/permission-policy"),
    ("POST", "/api/admin/role-changes"),
    ("POST", "/api/admin/role-changes/{approval_id}/execute"),
    ("GET", "/api/admin/anomalies"),
}


def _calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def _api_routes():
    return [r for r in server.app.routes if isinstance(r, APIRoute)]


def _operator_routes() -> list[tuple[str, str]]:
    found = []
    for route in _api_routes():
        if admin_ops.require_platform_admin in set(_calls(route.dependant)):
            found.extend((method, route.path) for method in sorted(route.methods))
    return found


OPERATOR_ROUTES = _operator_routes()


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", ANY_ID, path)


def _call(method: str, path: str, role: str):
    client = TestClient(server.app, raise_server_exceptions=False)
    client.cookies.set("csrf_token", CSRF)
    token = auth._issue_jwt(f"{role}@brokerage.test", TENANT, role)
    return client.request(method, _url(path), json={}, headers={
        "Authorization": f"Bearer {token}", "X-CSRF-Token": CSRF})


def test_the_operator_surface_was_found():
    # 28 when this test was written (2026-11-08); fewer means the scan broke.
    assert len(OPERATOR_ROUTES) >= 28, OPERATOR_ROUTES
    assert ("GET", "/api/admin/brokerages") in OPERATOR_ROUTES
    assert ("POST", "/api/admin/privacy/erasures/{tenant_id}/start") in OPERATOR_ROUTES


@pytest.mark.parametrize("role", ["broker_owner", "agent"])
@pytest.mark.parametrize("method,path", OPERATOR_ROUTES, ids=[f"{m} {p}" for m, p in OPERATOR_ROUTES])
def test_a_brokerage_role_is_refused_every_operator_route(method, path, role):
    response = _call(method, path, role)
    assert response.status_code == 403, (method, path, role, response.text[:200])
    assert response.json() == DENIED, response.text[:200]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/admin/brokerages"),
    ("POST", "/api/admin/users/{user_id}/mfa/reset"),
    ("POST", "/api/admin/privacy/legal-holds"),
    ("PUT", "/api/admin/ai-spend"),
    ("POST", "/admin/simulate-surge"),
])
def test_a_platform_admin_gets_past_the_same_gate(method, path):
    """Positive control: with CSRF satisfied, the 403 above is the role check,
    not the CSRF guard or a missing route. (Without a database the operator's
    request then fails later, which is fine here.)"""
    assert (method, path) in OPERATOR_ROUTES
    response = _call(method, path, "platform_admin")
    assert response.status_code != 404
    assert not (response.status_code == 403 and response.json() == DENIED), response.text[:200]
    assert "CSRF" not in response.text


def test_every_admin_path_is_gated_or_explicitly_a_broker_owner_route():
    gated = set(OPERATOR_ROUTES)
    undecided = []
    for route in _api_routes():
        if not route.path.startswith(("/api/admin", "/admin")):
            continue
        for method in sorted(route.methods):
            if (method, route.path) not in gated and (method, route.path) not in BROKER_OWNER_ADMIN_ROUTES:
                undecided.append(f"{method} {route.path}")
    assert not undecided, (
        "Put these behind admin_ops.require_platform_admin, or add them to "
        "BROKER_OWNER_ADMIN_ROUTES with the reason:\n  " + "\n  ".join(undecided))


def test_an_unreadable_alert_table_is_not_reported_as_no_alerts(monkeypatch):
    """OBS-001: the health response said open_alerts: [] whether there were
    none or the query failed. It now says which."""
    import asyncio
    from contextlib import asynccontextmanager

    import component_health
    import db.connection
    from tenancy import Role, TenantContext

    async def snapshot(fresh=False):
        return {"state": "healthy", "components": {}}

    @asynccontextmanager
    async def broken_tx(_ctx):
        raise OSError("ops_alerts unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(component_health, "snapshot", snapshot)
    monkeypatch.setattr(db.connection, "tenant_tx", broken_tx)
    ctx = TenantContext(agent_id="ops@platform.test", tenant_id=TENANT, role=Role.PLATFORM_ADMIN)
    out = asyncio.run(admin_ops.health_components(ctx=ctx))
    assert out["alerts_status"] == "unavailable" and out["open_alerts"] == []
