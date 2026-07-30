"""Regression coverage for operator accounts without a mutable users row."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import agent_profile
from tenancy import Role, TenantContext


CTX = TenantContext(
    agent_id="operator@neoh.test",
    tenant_id="00000000-0000-0000-0000-000000000000",
    role=Role.PLATFORM_ADMIN,
)


class _NoUserConn:
    async def fetchrow(self, query, *_args):
        assert "FROM users" in query
        return None


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


def test_personal_ai_onboarding_returns_empty_profile_for_operator_without_user_row(monkeypatch):
    monkeypatch.setattr(agent_profile, "tenant_tx", _fake_tenant_tx(_NoUserConn()))

    result = asyncio.run(agent_profile.my_brokerage_onboarding(CTX))

    assert result == {
        "user_role": "platform_admin",
        "membership": None,
        "licenses": [],
        "ai_settings": None,
        "google_connected": False,
        "style_training_examples": 0,
    }
