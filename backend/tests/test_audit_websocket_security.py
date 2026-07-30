"""Security regression coverage for the global audit WebSocket."""

from __future__ import annotations

import asyncio
import json

from fastapi import WebSocketDisconnect

import audit_ledger
from auth import _issue_jwt
from tenancy import Role


class _FakeLedger:
    def __init__(self) -> None:
        self.subscribed = []
        self.unsubscribed = []
        self.history_reads = 0

    def subscribe(self, websocket) -> None:
        self.subscribed.append(websocket)

    def unsubscribe(self, websocket) -> None:
        self.unsubscribed.append(websocket)

    async def get_entries(self, limit: int):
        self.history_reads += 1
        assert limit == 50
        return [{"action": "safe-test-entry"}]


class _FakeWebSocket:
    def __init__(self, protocols: str = "") -> None:
        self.headers = {"sec-websocket-protocol": protocols}
        self.accepted_protocol = None
        self.closed_code = None
        self.frames: list[dict] = []

    async def accept(self, subprotocol=None) -> None:
        self.accepted_protocol = subprotocol

    async def close(self, code: int) -> None:
        self.closed_code = code

    async def send_text(self, payload: str) -> None:
        self.frames.append(json.loads(payload))

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(code=1000)


def test_audit_websocket_rejects_missing_credentials_before_history(monkeypatch):
    fake_ledger = _FakeLedger()
    monkeypatch.setattr(audit_ledger, "ledger", fake_ledger)
    websocket = _FakeWebSocket()

    asyncio.run(audit_ledger.audit_trail_ws(websocket))

    assert websocket.accepted_protocol is None
    assert websocket.closed_code == 4401
    assert fake_ledger.history_reads == 0
    assert fake_ledger.subscribed == []


def test_audit_websocket_rejects_non_admin_before_history(monkeypatch):
    fake_ledger = _FakeLedger()
    monkeypatch.setattr(audit_ledger, "ledger", fake_ledger)
    token = _issue_jwt(
        "agent",
        "11111111-1111-1111-1111-111111111111",
        Role.AGENT.value,
    )
    websocket = _FakeWebSocket(f"oracle.jwt, {token}")

    asyncio.run(audit_ledger.audit_trail_ws(websocket))

    assert websocket.accepted_protocol is None
    assert websocket.closed_code == 4403
    assert fake_ledger.history_reads == 0
    assert fake_ledger.subscribed == []


def test_audit_websocket_allows_platform_admin_and_unsubscribes(monkeypatch):
    fake_ledger = _FakeLedger()
    monkeypatch.setattr(audit_ledger, "ledger", fake_ledger)
    token = _issue_jwt(
        "operator",
        "00000000-0000-0000-0000-000000000000",
        Role.PLATFORM_ADMIN.value,
    )
    websocket = _FakeWebSocket(f"oracle.jwt, {token}")

    asyncio.run(audit_ledger.audit_trail_ws(websocket))

    assert websocket.accepted_protocol == "oracle.jwt"
    assert websocket.closed_code is None
    assert fake_ledger.history_reads == 1
    assert websocket.frames == [
        {"type": "AUDIT_HISTORY", "data": [{"action": "safe-test-entry"}]}
    ]
    assert fake_ledger.subscribed == [websocket]
    assert fake_ledger.unsubscribed == [websocket]
