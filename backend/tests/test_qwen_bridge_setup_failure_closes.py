"""NQ-09 (23.1): a failed Qwen session setup must close the provider socket.

run() opens the provider websocket, then sends session.update and waits for
readiness. A failure in either step used to skip the close, leaking the socket.
No network and no real provider: websockets.connect is patched to a fake.
"""
import asyncio
import json

import pytest

import qwen_omni_realtime
from qwen_omni_realtime import (
    QwenOmniRealtimeBridge,
    QwenRealtimeError,
    QwenRealtimeSettings,
)


class _FakeProviderSocket:
    """Records sends and close(); iteration blocks, so no event ever arrives."""

    def __init__(self, *, fail_session_update: bool = False) -> None:
        self.fail_session_update = fail_session_update
        self.sent: list[dict] = []
        self.close_awaited = 0

    async def send(self, raw: str) -> None:
        event = json.loads(raw)
        if self.fail_session_update and event.get("type") == "session.update":
            raise ConnectionError("provider socket dropped during send")
        self.sent.append(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # never set: readiness never arrives

    async def close(self) -> None:
        self.close_awaited += 1


def _bridge_with_fake_socket(monkeypatch, fake: _FakeProviderSocket):
    async def fake_connect(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(qwen_omni_realtime.websockets, "connect", fake_connect)
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="ws")
    return QwenOmniRealtimeBridge(object(), "call-setup-failure", settings=settings)


def test_a_failed_session_setup_closes_the_provider_socket_on_send_failure(
    monkeypatch,
):
    fake = _FakeProviderSocket(fail_session_update=True)
    bridge = _bridge_with_fake_socket(monkeypatch, fake)

    with pytest.raises(ConnectionError, match="dropped during send"):
        asyncio.run(bridge.run())

    assert fake.close_awaited == 1, "provider socket leaked after session.update failed"


def test_a_failed_session_setup_closes_the_provider_socket_on_readiness_timeout(
    monkeypatch,
):
    fake = _FakeProviderSocket()
    bridge = _bridge_with_fake_socket(monkeypatch, fake)
    monkeypatch.setattr(qwen_omni_realtime, "_SESSION_READY_TIMEOUT", 0.01)

    with pytest.raises(QwenRealtimeError, match="configuration timed out"):
        asyncio.run(bridge.run())

    assert fake.close_awaited == 1, "provider socket leaked after readiness timeout"
