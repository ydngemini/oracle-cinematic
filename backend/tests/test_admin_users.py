"""/api/admin/users lists every real account.

It used to merge only the built-in demo identity map and user_profiles, so a
real brokerage user with no profile row never appeared in the operator console.
"""

from __future__ import annotations

import asyncio

import admin_ops
from tenancy import Role, TenantContext

ADMIN = TenantContext(agent_id="ops@x.test", tenant_id="00000000-0000-0000-0000-000000000000",
                      role=Role.PLATFORM_ADMIN)


def _run(monkeypatch, accounts, profiles, demo=None, sessions=()):
    queries = []

    async def fake_fetch(_ctx, query, *args):
        queries.append(query)
        return accounts if "FROM users u" in query else profiles

    monkeypatch.setattr(admin_ops, "_fetch", fake_fetch)
    monkeypatch.setattr(admin_ops, "DEMO_TENANCY", demo or {})
    monkeypatch.setattr(admin_ops, "active_sessions", lambda: list(sessions))
    return asyncio.run(admin_ops.users(ADMIN)), queries


def test_real_account_without_profile_is_listed(monkeypatch):
    out, queries = _run(monkeypatch,
                        accounts=[{"agent_id": "Jordan@Northstar.test", "tenant_id": "t1", "tenant_name": "Northstar",
                                   "role": "broker_owner", "is_active": True, "full_name": "Jordan Lee"}],
                        profiles=[])
    [u] = out["users"]
    assert u["agent_id"] == "Jordan@Northstar.test" and u["role"] == "broker_owner"
    assert u["display_name"] == "Jordan Lee" and u["has_profile"] is False and u["is_active"] is True
    assert not any("password_hash" in q for q in queries)


def test_profile_joins_its_account_case_insensitively(monkeypatch):
    out, _ = _run(monkeypatch,
                  accounts=[{"agent_id": "jordan@northstar.test", "tenant_id": "t1", "tenant_name": "Northstar",
                             "role": "agent", "is_active": False, "full_name": None}],
                  profiles=[{"user_id": "Jordan@Northstar.test", "tenant_id": "t1", "tenant_name": "Northstar",
                             "display_name": "Jordan", "public_email": "j@northstar.test"}])
    [u] = out["users"]
    assert u["has_profile"] is True and u["display_name"] == "Jordan" and u["is_active"] is False


def test_demo_identity_and_orphan_profile_still_listed(monkeypatch):
    out, _ = _run(monkeypatch, accounts=[],
                  profiles=[{"user_id": "orphan@x.test", "tenant_id": "t2", "tenant_name": None, "display_name": "Orphan"}],
                  demo={"demo-operator": ("t0", "platform_admin")},
                  sessions=[{"agent_id": "demo-operator", "issued_at": 1, "expires_at": 2}])
    ids = {u["agent_id"]: u for u in out["users"]}
    assert ids["demo-operator"]["online"] is True and ids["demo-operator"]["role"] == "platform_admin"
    assert ids["orphan@x.test"]["role"] == "unknown" and out["online"] == 1
