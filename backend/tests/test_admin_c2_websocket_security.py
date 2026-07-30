import asyncio

from fastapi import WebSocketDisconnect

import admin_c2
from tenancy import Role, TenantContext


ADMIN = TenantContext(
    agent_id="admin",
    tenant_id="00000000-0000-0000-0000-000000000000",
    role=Role.PLATFORM_ADMIN,
)
AGENT = TenantContext(
    agent_id="agent",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class FakeWebSocket:
    def __init__(self, protocols=""):
        self.headers = {"sec-websocket-protocol": protocols}
        self.query_params = {"token": "must-not-be-used"}
        self.accepted_protocol = None
        self.closed_code = None

    async def accept(self, *, subprotocol):
        self.accepted_protocol = subprotocol

    async def close(self, *, code):
        self.closed_code = code

    async def receive_text(self):
        raise WebSocketDisconnect()


def test_surge_websocket_rejects_query_credentials(monkeypatch):
    monkeypatch.setattr(
        admin_c2,
        "require_context",
        lambda _authorization: (_ for _ in ()).throw(AssertionError("not called")),
    )
    ws = FakeWebSocket()

    asyncio.run(admin_c2.surge_telemetry_ws(ws))

    assert ws.accepted_protocol is None
    assert ws.closed_code == 4401


def test_surge_websocket_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(admin_c2, "require_context", lambda _authorization: AGENT)
    ws = FakeWebSocket("oracle.jwt, valid-agent-token")

    asyncio.run(admin_c2.surge_telemetry_ws(ws))

    assert ws.accepted_protocol is None
    assert ws.closed_code == 4403


def test_surge_websocket_accepts_admin_and_cleans_up(monkeypatch):
    monkeypatch.setattr(admin_c2, "require_context", lambda _authorization: ADMIN)
    admin_c2._surge_subscribers.clear()
    ws = FakeWebSocket("oracle.jwt, valid-admin-token")

    asyncio.run(admin_c2.surge_telemetry_ws(ws))

    assert ws.accepted_protocol == "oracle.jwt"
    assert ws.closed_code is None
    assert ws not in admin_c2._surge_subscribers
