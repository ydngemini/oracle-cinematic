"""Every inline platform-admin check is a stated choice (prompt 17 AC17.07, L17-29).

test_operator_routes_refuse_tenant_roles.py proves the routes behind
admin_ops.require_platform_admin refuse brokerage roles. This file covers the
rest: route handlers that mention the platform-admin role in their own body.
The scan reads each mounted endpoint's source (AWS observability included,
whether or not AWS_OBSERVABILITY_ENABLED mounted it here) and finds any
reference to Role.PLATFORM_ADMIN, is_platform_admin, the "platform_admin"
string or the observability role set. Each hit must sit in exactly one class
below with its reason, so a new inline check fails CI until someone decides
what it is.

The dependency is the preferred gate because it also proves the session is
still current (tenancy.verify_session_current); an inline require_role reads
only the token claim, so a demoted admin keeps access until the token expires.

Decision recorded 2026-11-11: 18 /api/aws read routes admitted broker_owner.
EC2 inventories, the AWS bill and security findings belong to the platform,
not to a brokerage, so all 20 /api/aws routes and the 3 inline operator routes
(harvest run, MLS entitlement grant and revoke) moved behind the dependency.

Limit: the scan reads endpoint bodies only. A check inside a helper the
endpoint calls is not seen.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import admin_ops
import auth
import aws_observability
import server

TENANT = "33333333-3333-4333-8333-333333333333"
ANY_ID = "44444444-4444-4444-8444-444444444444"
CSRF = "csrf-test-token-0123456789abcdef"
DENIED = {"detail": "Platform admin only."}

_NAMES = {"PLATFORM_ADMIN", "is_platform_admin", "require_platform_admin_ctx"}
_ROLE_SETS = {"_OBS_ALLOWED_ROLES"}


def _calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def _behind_gate(route) -> bool:
    return admin_ops.require_platform_admin in set(_calls(route.dependant))


def _mentions_platform_admin(fn) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _NAMES:
            return True
        if isinstance(node, ast.Name) and (node.id in _NAMES or node.id in _ROLE_SETS):
            return True
        if isinstance(node, ast.Constant) and node.value == "platform_admin":
            return True
    return False


def _all_routes():
    seen, out = set(), []
    for route in list(server.app.routes) + list(aws_observability.router.routes):
        if id(route.endpoint if hasattr(route, "endpoint") else route) in seen:
            continue
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            seen.add(id(route.endpoint))
            out.append(route)
    return out


def _inline() -> set[tuple[str, str]]:
    found = set()
    for route in _all_routes():
        if isinstance(route, APIRoute):
            if _behind_gate(route) or not _mentions_platform_admin(route.endpoint):
                continue
            found.update((method, route.path) for method in route.methods)
        elif _mentions_platform_admin(route.endpoint):
            found.add(("WS", route.path))
    return found


INLINE = _inline()

# The stream endpoints cannot take an HTTP dependency the same way; each
# closes the socket for any role but platform_admin.
OPERATOR_WEBSOCKETS = {
    ("WS", "/admin/audit-trail"): "closes unless ctx.role is PLATFORM_ADMIN",
    ("WS", "/admin/surge-telemetry"): "closes unless ctx.role is PLATFORM_ADMIN (dev-only router)",
    ("WS", "/api/aws/ws"): "closes 4403 unless role in _OBS_ALLOWED_ROLES; runtime test below",
}

# Brokerage governance: require_role(BROKER_OWNER, PLATFORM_ADMIN). An agent is
# refused (runtime test below); the operator may act for any brokerage.
OWNER_OR_OPERATOR = {
    ("PATCH", "/api/brokerage/profile"): "edit the brokerage profile",
    ("GET", "/api/brokerage/invitations"): "list pending invitations",
    ("POST", "/api/brokerage/invitations"): "invite; only the operator may invite a broker_owner",
    ("POST", "/api/brokerage/invitations/{invitation_id}/resend"): "resend an invitation",
    ("DELETE", "/api/brokerage/invitations/{invitation_id}"): "revoke an invitation",
    ("POST", "/api/brokerage/team/{user_id}/suspend"): "suspend a member",
    ("POST", "/api/brokerage/team/{user_id}/mfa/reset"): "reset a member's two-step sign-in",
    ("POST", "/api/brokerage/team/{user_id}/reinstate"): "reinstate a member",
    ("PUT", "/api/brokerage/setup/progress"): "record setup progress",
}

# The body names a tenant_id; anyone but the operator must name their own.
OPERATOR_MAY_NAME_ANOTHER_TENANT = {
    ("POST", "/billing/create-checkout-session"): "body.tenant_id must equal ctx.tenant_id",
    ("POST", "/billing/create-portal-session"): "body.tenant_id must equal ctx.tenant_id",
    ("GET", "/billing/status/{tenant_id}"): "path tenant_id must equal ctx.tenant_id",
}

# The handler builds a platform-scoped TenantContext for one narrow lookup the
# caller is entitled to; the caller never receives the role.
SYSTEM_LOOKUP = {
    ("GET", "/api/public/property-upload/{token}"): "resolves the upload token's hash to its tenant",
    ("POST", "/api/public/property-upload/{token}"): "resolves the upload token's hash to its tenant",
    ("POST", "/api/public/lead-intake/{public_id}"): "finds the connector, then verifies its signature",
    ("GET", "/api/commands/providers/google/oauth/callback"): "consumes an HMAC-verified OAuth state",
    ("POST", "/api/marketplace/publications/{publication_id}/bidding-message"):
        "counts recorded offers on a publication the caller's RLS already returned",
}

# The check widens what the caller sees or may change inside their own
# brokerage (RLS still bounds the rows); it is not the route's gate.
WIDER_VIEW = {
    ("PUT", "/api/telephony/routes/me"): "only owner/operator may change the caller-ID identity",
    ("GET", "/api/ai/chat/capabilities"): "owner/operator see operations capabilities",
    ("GET", "/api/licensing/agent/{agent_id}/status"): "an agent reads only their own licence",
    ("POST", "/api/licensing/agent/{agent_id}/ce"): "an agent records only their own CE",
    ("GET", "/api/v1/pipeline/map-clusters"): "operator flag passed to the scoped query",
    ("GET", "/api/commands/providers"): "owner/operator see every provider connection",
    ("PUT", "/api/commands/{command_id}"): "only owner/operator may edit another agent's command",
    ("GET", "/api/sales/plans"): "owner/operator see every plan",
    ("GET", "/api/sales/plans/enrollments"): "owner/operator see every enrollment",
    ("GET", "/api/sites"): "owner/operator see every site",
    ("PUT", "/api/sites/{site_id}/collaborators/{agent_id}"): "site owner, broker owner or operator",
    ("DELETE", "/api/sites/{site_id}/collaborators/{agent_id}"): "site owner, broker owner or operator",
    ("GET", "/api/brokerage/team"): "owner/operator see every member's detail",
}

# The role is named about the target data, not the caller.
NOT_A_CALLER_GATE = {
    ("POST", "/auth/login"): "a platform admin's sign-in is timing-protected and raises a security alert",
    ("POST", "/api/admin/role-changes"): "a platform-admin account cannot be a role-change target",
}

CLASSES = [OPERATOR_WEBSOCKETS, OWNER_OR_OPERATOR, OPERATOR_MAY_NAME_ANOTHER_TENANT,
           SYSTEM_LOOKUP, WIDER_VIEW, NOT_A_CALLER_GATE]
CLASSIFIED = {key for cls in CLASSES for key in cls}

FORMERLY_INLINE_OPERATOR_ROUTES = [
    ("POST", "/api/market/harvest/run/{task_name}"),
    ("POST", "/api/brokerage/mls/entitlements"),
    ("DELETE", "/api/brokerage/mls/entitlements/{mls_id}"),
]


def _aws_http_routes() -> list[tuple[str, str, APIRoute]]:
    return sorted(
        ((m, r.path, r) for r in aws_observability.router.routes
         if isinstance(r, APIRoute) for m in r.methods),
        key=lambda x: (x[1], x[0]))


AWS_HTTP = [(m, p) for m, p, _ in _aws_http_routes()]


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", ANY_ID, path)


def _token(role: str) -> str:
    return auth._issue_jwt(f"{role}@brokerage.test", TENANT, role)


def _call(app, method: str, path: str, role: str, body=None):
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("csrf_token", CSRF)
    return client.request(method, path if "{" not in path else _url(path),
                          json={} if body is None else body,
                          headers={"Authorization": f"Bearer {_token(role)}",
                                   "X-CSRF-Token": CSRF})


def _aws_app() -> FastAPI:
    app = FastAPI()
    app.include_router(aws_observability.router)
    return app


# --------------------------------------------------------------- the registry

def test_the_scan_found_the_inline_surface():
    # 35 when this test was written (2026-11-11); fewer means the scan broke.
    assert len(INLINE) >= 33, sorted(INLINE)


def test_every_inline_platform_admin_check_is_classified():
    unclassified = sorted(INLINE - CLASSIFIED)
    assert not unclassified, (
        "Put these behind admin_ops.require_platform_admin, or add them to a "
        "class in this file with the reason:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in unclassified))


def test_no_classification_is_stale():
    stale = sorted(CLASSIFIED - INLINE)
    assert not stale, f"no longer an inline check; remove from the registry: {stale}"


def test_each_route_is_in_one_class_only():
    keys = [key for cls in CLASSES for key in cls]
    assert len(keys) == len(set(keys))


# ------------------------------------------------------- AWS observability

def test_no_aws_http_route_checks_the_role_inline():
    assert {k for k in INLINE if k[1].startswith("/api/aws") and k[0] != "WS"} == set()


def test_every_aws_route_is_behind_the_operator_dependency():
    assert len(AWS_HTTP) == 20, AWS_HTTP
    ungated = [(m, p) for m, p, r in _aws_http_routes() if not _behind_gate(r)]
    assert not ungated, ungated


def test_the_websocket_admits_the_same_roles_as_the_routes():
    assert aws_observability._OBS_ALLOWED_ROLES == {"platform_admin"}


@pytest.mark.parametrize("role", ["broker_owner", "agent"])
@pytest.mark.parametrize("method,path", AWS_HTTP, ids=[f"{m} {p}" for m, p in AWS_HTTP])
def test_a_brokerage_role_is_refused_every_aws_route(method, path, role):
    response = _call(_aws_app(), method, path, role)
    assert response.status_code == 403, (method, path, role, response.text[:200])
    assert response.json() == DENIED


def test_a_platform_admin_gets_past_the_aws_gate(monkeypatch):
    """Positive control: the operator reaches the handler, whose own name check
    answers 422 before any AWS call."""
    async def current(_ctx):
        return None
    monkeypatch.setattr(admin_ops, "verify_session_current", current)
    response = _call(_aws_app(), "POST", "/api/aws/autoscaling/bad%20group!/scale",
                     "platform_admin", {"desired_capacity": 1})
    assert response.status_code == 422, response.text[:200]


def test_a_demoted_operator_is_refused_by_the_session_check(monkeypatch):
    """What the move bought: the gate asks the database whether the account is
    still an operator; an inline require_role only read the token."""
    async def ended(_ctx):
        raise HTTPException(status_code=401, detail="Your session has ended. Sign in again.")
    monkeypatch.setattr(admin_ops, "verify_session_current", ended)
    response = _call(_aws_app(), "GET", "/api/aws/billing", "platform_admin")
    assert response.status_code == 401, response.text[:200]


@pytest.mark.parametrize("role", ["broker_owner", "agent"])
def test_a_brokerage_role_is_refused_the_aws_stream(role):
    client = TestClient(_aws_app())
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/api/aws/ws", subprotocols=["oracle.jwt", _token(role)]) as ws:
            ws.receive_text()
    assert closed.value.code == 4403


# ------------------------------------------- formerly inline operator routes

def test_formerly_inline_operator_routes_are_behind_the_dependency():
    """Their 403 for brokerage roles is then proven, through the real app, by
    test_operator_routes_refuse_tenant_roles.py."""
    gated = {(m, r.path) for r in server.app.routes
             if isinstance(r, APIRoute) and _behind_gate(r) for m in r.methods}
    missing = [k for k in FORMERLY_INLINE_OPERATOR_ROUTES if k not in gated]
    assert not missing, missing


# -------------------------------------------------- brokerage governance

# Minimal valid bodies, so body validation passes and the role check answers.
_MEMBER_CHANGE = {"reason": "role test"}
GOVERNANCE_BODIES = {
    ("PATCH", "/api/brokerage/profile"): {"name": "Test Brokerage"},
    ("POST", "/api/brokerage/invitations"): {"emails": ["new@brokerage.test"]},
    ("POST", "/api/brokerage/team/{user_id}/suspend"): _MEMBER_CHANGE,
    ("POST", "/api/brokerage/team/{user_id}/mfa/reset"): _MEMBER_CHANGE,
    ("POST", "/api/brokerage/team/{user_id}/reinstate"): _MEMBER_CHANGE,
    ("PUT", "/api/brokerage/setup/progress"): {"capability": "contact_import", "status": "READY"},
}


@pytest.mark.parametrize("method,path", sorted(OWNER_OR_OPERATOR),
                         ids=[f"{m} {p}" for m, p in sorted(OWNER_OR_OPERATOR)])
def test_an_agent_is_refused_brokerage_governance(method, path):
    response = _call(server.app, method, path, "agent", GOVERNANCE_BODIES.get((method, path)))
    assert response.status_code == 403, (method, path, response.text[:200])
    assert "not permitted" in response.json()["detail"]
