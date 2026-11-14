"""Operator WebSockets prove the session is live, at the handshake and while open
(SEC-001 of the 2026-11-13 audit, CodeRabbit on PR #45; L17-29).

The REST gate admin_ops.require_platform_admin runs verify_session_current, so
a demoted or deactivated operator is refused at once. The three operator
streams (AWS observability, audit trail, surge telemetry) decoded the signed
token and accepted: a still-valid token from a demoted admin opened them, and
an open stream kept receiving pushes. Each now calls
tenancy.admit_operator_stream before accept() and runs
tenancy.watch_operator_stream while open.

verify_session_current is replaced here because it needs PostgreSQL; the live
behaviour of the check itself is proven by the session tests in the live suite.
"""
from __future__ import annotations

import signal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import audit_ledger
import auth
import aws_observability
import server
import tenancy

TENANT = "33333333-3333-4333-8333-333333333333"


def _aws_app() -> FastAPI:
    app = FastAPI()
    app.include_router(aws_observability.router)
    return app


async def _no_history(limit=50):
    return []


STREAMS = {
    "aws": (_aws_app, "/api/aws/ws"),
    "audit": (lambda: server.app, "/admin/audit-trail"),
    "surge": (lambda: server.app, "/admin/surge-telemetry"),
}


def _connect(name: str):
    app_factory, path = STREAMS[name]
    token = auth._issue_jwt("ops@platform.test", TENANT, "platform_admin")
    return TestClient(app_factory()).websocket_connect(path, subprotocols=["oracle.jwt", token])


@pytest.fixture(autouse=True)
def _quiet_audit_history(monkeypatch):
    monkeypatch.setattr(audit_ledger.ledger, "get_entries", _no_history)


class StreamStayedOpen(Exception):
    pass


@pytest.fixture(autouse=True)
def _deadline():
    """A stream that wrongly stays open never sends a frame, and the test
    client's receive has no timeout: fail the test instead of hanging CI."""
    def expire(_signum, _frame):
        raise StreamStayedOpen("the stream stayed open past the deadline")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _session(monkeypatch, *outcomes):
    """Each call to verify_session_current takes the next outcome: None passes,
    an exception is raised. The last outcome repeats."""
    calls = []

    async def verify(ctx):
        calls.append(ctx)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(tenancy, "verify_session_current", verify)
    return calls


ENDED = HTTPException(status_code=401, detail="Your session has ended. Sign in again.")


def test_a_signed_operator_token_asks_for_the_session_check():
    ctx = tenancy.require_context(
        "Bearer " + auth._issue_jwt("ops@platform.test", TENANT, "platform_admin"))
    assert ctx.verify_session, "a user token must be re-proved; this test would prove nothing"


@pytest.mark.parametrize("name", sorted(STREAMS))
def test_an_ended_session_is_refused_before_accept(monkeypatch, name):
    calls = _session(monkeypatch, ENDED)
    with pytest.raises(WebSocketDisconnect) as closed:
        with _connect(name) as ws:
            ws.receive_text()
    assert closed.value.code == 4401
    assert len(calls) == 1


@pytest.mark.parametrize("name", sorted(STREAMS))
def test_an_unverifiable_session_fails_closed_without_blaming_the_token(monkeypatch, name):
    _session(monkeypatch, OSError("database unreachable"))
    with pytest.raises(WebSocketDisconnect) as closed:
        with _connect(name) as ws:
            ws.receive_text()
    assert closed.value.code == 1011


def test_a_current_operator_is_admitted(monkeypatch):
    _session(monkeypatch, None)
    with _connect("aws") as ws:
        assert '"AWS_CONNECTED"' in ws.receive_text()
    with _connect("audit") as ws:
        assert '"AUDIT_HISTORY"' in ws.receive_text()


@pytest.mark.parametrize("name", sorted(STREAMS))
def test_an_open_stream_closes_when_the_session_ends(monkeypatch, name):
    monkeypatch.setattr(tenancy, "SESSION_RECHECK_SECONDS", 0.05)
    calls = _session(monkeypatch, None, ENDED)
    with pytest.raises(WebSocketDisconnect) as closed:
        with _connect(name) as ws:
            for _ in range(3):  # the greeting frame, if any, then the close
                ws.receive_text()
    assert closed.value.code == 4401
    assert len(calls) >= 2


def test_the_watch_stays_quiet_while_the_session_is_current(monkeypatch):
    monkeypatch.setattr(tenancy, "SESSION_RECHECK_SECONDS", 0.02)
    calls = _session(monkeypatch, None)
    with _connect("aws") as ws:
        ws.receive_text()  # AWS_CONNECTED
        for _ in range(5):
            ws.send_text('{"type": "PING"}')
            assert '"PONG"' in ws.receive_text()
    assert len(calls) >= 1
