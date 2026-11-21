"""The voice-session stream is admitted like /ws and re-checked while open
(external audit 27.2, 27.8).

It used to check identity and call visibility once, at the handshake, and then
stream that call's transcript events for as long as the client stayed
connected: after the token expired, the account was revoked, a share was
withdrawn or the client reassigned. It also ignored the replica's connection
ceiling and had no idle limit, so voice sockets could starve /ws.

These drive the real handler through a fake socket with tiny intervals.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException, WebSocketDisconnect

import server
from tenancy import Role, TenantContext

TENANT = "00000000-0000-0000-0000-0000000000c1"
SESSION = "00000000-0000-0000-0000-00000000a001"


class FakeSocket:
    def __init__(self, *, pings: bool = False):
        self.headers: dict[str, str] = {}
        self.accepted = False
        self.closed_with: int | None = None
        self.sent: list = []
        self.pings = pings

    async def accept(self, subprotocol=None):
        self.accepted = True

    async def send_json(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        while self.closed_with is None:
            await asyncio.sleep(0.02)
            if self.pings:
                return "PING"
        raise WebSocketDisconnect(self.closed_with)

    async def close(self, code=1000, reason=None):
        if self.closed_with is None:
            self.closed_with = code


class World:
    def __init__(self):
        self.session_live = True
        self.visible = True
        self.at_capacity = False
        self.recheck_error = False
        self.visibility_error = False
        self.visibility_stalls = False
        self.admission_db_down = False
        self.visibility_checks = 0
        self.registered: list = []
        self.unregistered: list = []


@pytest.fixture
def world(monkeypatch):
    import config
    import record_access
    import tenancy
    import voice_intel
    from db import connection

    w = World()
    monkeypatch.setattr(config, "IS_DEV", True)
    monkeypatch.setattr(server, "_WS_IDLE_TIMEOUT", 0.3)
    monkeypatch.setattr(tenancy, "SESSION_RECHECK_SECONDS", 0.0)
    monkeypatch.setattr(server, "_VOICE_RECHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(server, "record_platform_websocket", lambda *_a: None)
    monkeypatch.setattr(server.ws_hub, "at_capacity", lambda: w.at_capacity)
    monkeypatch.setattr(server.ws_hub, "register", lambda *a, **_k: w.registered.append(a))
    monkeypatch.setattr(server.ws_hub, "unregister", lambda *a, **_k: w.unregistered.append(a))

    async def verify(_ctx):
        if w.admission_db_down or (w.recheck_error and w.visibility_checks):
            raise ConnectionError("database unavailable")
        if not w.session_live:
            raise HTTPException(401, "session ended")

    async def may_read(tenant_id, session_id, agent_id):
        assert (tenant_id, session_id) == (TENANT, SESSION)
        w.visibility_checks += 1
        if w.visibility_checks > 1 and w.visibility_error:
            raise ConnectionError("database unavailable")
        if w.visibility_checks > 1 and w.visibility_stalls:
            await asyncio.sleep(30)
        return w.visible

    class Conn:
        async def fetchrow(self, *_a):
            return {"id": SESSION, "consent_recorded": True, "transcript_status": "live",
                    "started_at": None}

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def events(*_a, **_k):
        return []

    monkeypatch.setattr(server, "verify_session_current", verify)   # the watchdog
    monkeypatch.setattr(tenancy, "verify_session_current", verify)  # admit_operator_stream
    monkeypatch.setattr(record_access, "may_read_call_content", may_read)
    monkeypatch.setattr(connection, "tenant_tx", tx)
    monkeypatch.setattr(voice_intel, "load_session_events", events)
    return w


def _identity(monkeypatch, *, role=Role.AGENT, expires_in: float | None = 3600):
    ctx = TenantContext(agent_id="agent@voice.test", tenant_id=TENANT, role=role,
                        expires_at=None if expires_in is None else time.time() + expires_in)
    monkeypatch.setattr(server, "_resolve_websocket_identity", lambda _ws: (ctx, False))
    return ctx


def _run(ws, *, during=None, timeout=3.0):
    async def go():
        handler = asyncio.create_task(server.voice_telemetry_websocket(ws, SESSION))
        if during is not None:
            await asyncio.sleep(0.05)
            during()
        await asyncio.wait_for(handler, timeout)

    asyncio.run(go())


def test_a_revoked_session_is_refused_before_the_upgrade(world, monkeypatch):
    _identity(monkeypatch)
    world.session_live = False
    ws = FakeSocket()
    _run(ws)
    assert (ws.accepted, ws.closed_with) == (False, 4401)


def test_an_unreachable_database_at_the_handshake_fails_closed(world, monkeypatch):
    _identity(monkeypatch)
    world.admission_db_down = True
    ws = FakeSocket()
    _run(ws)  # returns, never raises
    assert (ws.accepted, ws.closed_with) == (False, 1011)


def test_a_full_replica_refuses_a_voice_socket(world, monkeypatch):
    _identity(monkeypatch)
    world.at_capacity = True
    ws = FakeSocket()
    _run(ws)
    assert (ws.accepted, ws.closed_with) == (False, 1013)


def test_losing_access_to_the_call_closes_the_stream(world, monkeypatch):
    """A share revoked, the client reassigned or the user demoted after joining."""
    _identity(monkeypatch)
    ws = FakeSocket(pings=True)

    def revoke():
        assert ws.accepted
        world.visible = False

    _run(ws, during=revoke)
    assert ws.closed_with == 4404
    assert ws.sent[0]["type"] == "VOICE_SESSION_HYDRATED"
    # The socket leaves the call's broadcast group it joined.
    assert len(world.registered) == 1 and world.unregistered == world.registered
    # It joined as its member, so a push reaches it only while that member is
    # in the call record's audience (ws_hub.broadcast_record stream=; owner
    # decision 2026-11-20), not merely because it is in the group.
    group, _ws, member = world.registered[0]
    assert group == f"voice:{TENANT}:{SESSION}" and member == "agent@voice.test"


def test_an_ended_account_session_closes_the_stream(world, monkeypatch):
    _identity(monkeypatch)
    ws = FakeSocket(pings=True)
    _run(ws, during=lambda: setattr(world, "session_live", False))
    assert ws.closed_with == 4401


def test_an_expired_token_closes_the_stream(world, monkeypatch):
    _identity(monkeypatch, expires_in=0.2)
    ws = FakeSocket(pings=True)
    _run(ws)
    assert ws.closed_with == 4401


def test_a_recheck_that_cannot_complete_fails_closed(world, monkeypatch):
    _identity(monkeypatch)
    world.recheck_error = True
    ws = FakeSocket(pings=True)
    _run(ws)
    assert ws.closed_with == 1011


@pytest.mark.parametrize("failure", ["visibility_error", "visibility_stalls"])
def test_a_visibility_check_that_fails_or_hangs_closes_the_stream(world, monkeypatch, failure):
    """A hung check must not also stall the expiry and idle checks (review)."""
    _identity(monkeypatch)
    setattr(world, failure, True)
    ws = FakeSocket(pings=True)
    _run(ws)
    assert ws.closed_with == 1011
    assert world.unregistered == world.registered


def test_the_recheck_runs_at_the_operator_stream_cadence():
    """Call content is re-proved every 60 s like operator streams, not /ws's 300 s."""
    import inspect

    import tenancy

    assert tenancy.SESSION_RECHECK_SECONDS == 60.0
    src = inspect.getsource(server._watch_voice_socket)
    assert "tenancy.SESSION_RECHECK_SECONDS" in src and "_WS_SESSION_RECHECK_SECONDS" not in src


def test_a_silent_client_is_closed_and_a_pinging_one_is_not(world, monkeypatch):
    _identity(monkeypatch)
    silent = FakeSocket()
    _run(silent)
    assert silent.closed_with == 1001

    pinging = FakeSocket(pings=True)

    async def go():
        handler = asyncio.create_task(server.voice_telemetry_websocket(pinging, SESSION))
        await asyncio.sleep(1.0)  # > 3 idle timeouts
        assert pinging.closed_with is None
        assert "PONG" in pinging.sent
        await pinging.close(1000)
        await asyncio.wait_for(handler, 2)

    asyncio.run(go())


def test_a_platform_admin_stream_is_not_gated_on_record_visibility(world, monkeypatch):
    _identity(monkeypatch, role=Role.PLATFORM_ADMIN, expires_in=0.4)
    world.visible = False
    ws = FakeSocket(pings=True)
    _run(ws)
    assert ws.closed_with == 4401  # only the token ended it
    assert world.visibility_checks == 0
