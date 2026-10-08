"""What an agent sees on Connections: everything they may manage themselves."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import commands_api as ca


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []


def test_an_agent_sees_their_own_smtp_identity_not_only_google(monkeypatch):
    conn = _Conn()

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(ca, "tenant_tx", fake_tx)
    monkeypatch.setattr(ca, "require_feature", lambda *_a, **_k: None)
    ctx = ca.TenantContext(agent_id="avery", tenant_id="00000000-0000-0000-0000-0000000000aa", role=ca.Role.AGENT)
    assert asyncio.run(ca.provider_status(ctx=ctx)) == {"providers": []}
    sql, args = conn.calls[0]
    assert "account_label=$1" in sql, "an agent sees only rows labelled with their own id"
    assert args[0] == "avery"
    assert set(args[1]) == ca._AGENT_SELF_SERVICE_PROVIDERS == {"google", "smtp"}
