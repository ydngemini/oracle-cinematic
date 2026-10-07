import asyncio
"""
Auth token tests — decode_token is the single validation path every protected
endpoint and the WebSocket handlers funnel through, so a forged/expired/garbled
token must be rejected here or the whole tenancy gate is moot. Auth previously
had zero coverage.

No database required.

Runs under pytest, or standalone:
    ORACLE_ENV=dev python3 backend/tests/test_auth.py
"""

import os
import sys
import time

os.environ.setdefault("ORACLE_ENV", "dev")  # auth.py fails fast in prod without a key
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import jwt
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import auth


def _mint(claims: dict, key: str | None = None, algorithm: str | None = None) -> str:
    return jwt.encode(claims, key or auth.SECRET_KEY, algorithm=algorithm or auth.ALGORITHM)


def _valid_claims(**over) -> dict:
    now = int(time.time())
    base = {
        "sub": "agent-1",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "role": "agent",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(over)
    return base


def test_valid_token_roundtrips():
    claims = _mint(_valid_claims())
    decoded = auth.decode_token(claims)
    assert decoded["sub"] == "agent-1"
    assert decoded["role"] == "agent"


def test_expired_token_rejected():
    expired = _mint(_valid_claims(exp=int(time.time()) - 10))
    try:
        auth.decode_token(expired)
        assert False, "expired token was accepted"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_forged_signature_rejected():
    # Signed with a key that is NOT the server's — a token an attacker mints.
    forged = _mint(
        _valid_claims(role="platform_admin"),
        key="ATTACKER_KEY_NOT_THE_SERVER_32_BYTES",
    )
    try:
        auth.decode_token(forged)
        assert False, "forged token was accepted — privilege escalation"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_empty_and_garbage_tokens_rejected():
    for bad in ("", "not.a.jwt", "Bearer x", "a" * 10):
        try:
            auth.decode_token(bad)
            assert False, f"garbage token accepted: {bad!r}"
        except HTTPException as exc:
            assert exc.status_code == 401


def test_oversized_token_rejected():
    # decode_token guards on length before doing any crypto work.
    huge = _mint(_valid_claims(blob="x" * 9000))
    try:
        auth.decode_token(huge)
        assert False, "oversized token accepted"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_none_algorithm_attack_rejected():
    # Classic JWT "alg: none" downgrade — must not be honored.
    unsigned = jwt.encode(_valid_claims(role="platform_admin"), key="", algorithm="none")
    try:
        auth.decode_token(unsigned)
        assert False, "alg=none token accepted — signature bypass"
    except HTTPException as exc:
        assert exc.status_code == 401


def _request_with_cookie(token: str | None = None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"cookie", f"oracle_session={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/auth/session", "headers": headers})


def test_session_probe_reports_signed_out_without_401():
    result = asyncio.run(auth.session_status(_request_with_cookie(), Response()))
    assert result.authenticated is False
    assert result.agent_id is None


def test_session_probe_returns_valid_identity():
    result = asyncio.run(auth.session_status(
        _request_with_cookie(_mint(_valid_claims())),
        Response(),
    ))
    assert result.authenticated is True
    assert result.agent_id == "agent-1"
    assert result.role == "agent"


def test_session_probe_clears_invalid_cookie():
    response = Response()
    result = asyncio.run(auth.session_status(_request_with_cookie("not.a.jwt"), response))
    assert result.authenticated is False
    assert "oracle_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def _probe_with_epoch(monkeypatch, db_row=None, db_error=None, token_epoch=3):
    from contextlib import asynccontextmanager

    class _Conn:
        async def fetchrow(self, sql, *args):
            if db_error:
                raise db_error
            return db_row

    @asynccontextmanager
    async def fake_tx(ctx):
        yield _Conn()

    import db.connection

    monkeypatch.setattr(db.connection, "tenant_tx", fake_tx)
    token = auth._issue_jwt("a@x.test", "aaaaaaaa-0000-4000-8000-00000000000a", "agent",
                            user_id="bbbbbbbb-0000-4000-8000-00000000000b", session_epoch=token_epoch)
    response = Response()
    return asyncio.run(auth.session_status(_request_with_cookie(token), response)), response


def test_session_probe_honours_a_revoked_epoch(monkeypatch):
    """After logout elsewhere the signature is still valid, but the session is
    not; the probe said "authenticated" while every data call 401'd."""
    result, response = _probe_with_epoch(monkeypatch, {"session_epoch": 4, "is_active": True})
    assert result.authenticated is False
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_session_probe_accepts_the_current_epoch(monkeypatch):
    result, _ = _probe_with_epoch(monkeypatch, {"session_epoch": 3, "is_active": True})
    assert result.authenticated is True


def test_session_probe_rejects_a_deactivated_account(monkeypatch):
    result, _ = _probe_with_epoch(monkeypatch, {"session_epoch": 3, "is_active": False})
    assert result.authenticated is False


def test_session_probe_trusts_the_token_when_the_database_is_down(monkeypatch):
    """A failed check must never sign a person out (7ca8940)."""
    result, _ = _probe_with_epoch(monkeypatch, db_error=ConnectionError("db down"))
    assert result.authenticated is True
