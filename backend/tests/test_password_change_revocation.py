"""Changing or resetting a password must retire outstanding reset links.

Changing a password is what someone does when they believe their account is at
risk. It used to leave live reset tokens untouched, so an attacker who had
already obtained one — from a compromised mailbox, say — could still use it
afterwards and take the account straight back.

The reset path had the narrower version of the same hole: it consumed only the
token it was handed. Requesting a reset twice, which people do when the first
mail is slow, left every earlier link valid for its full 30 minutes after the
account had already been recovered.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import auth

AGENT = "agent@tenant.test"
USER_ID = "22222222-2222-2222-2222-222222222222"


class _Conn:
    def __init__(self, row=None):
        self.executed: list[tuple[str, tuple]] = []
        self._row = row

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        self.executed.append((query, args))
        return self._row

    def statements(self, needle):
        return [q for q, _ in self.executed if needle in q]


def _fake_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


def test_changing_a_password_revokes_outstanding_reset_links(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr("db.connection.tenant_tx", _fake_tx(conn), raising=False)
    monkeypatch.setattr(auth, "decode_token", lambda _t: {"sub": AGENT})
    monkeypatch.setattr(auth, "_verify_pw", lambda _p, _h: True)
    monkeypatch.setattr(auth, "_hash_pw", lambda _p: "new-hash")

    async def _user(_agent_id):
        return {"id": USER_ID, "password_hash": "old-hash"}

    monkeypatch.setattr(auth, "_lookup_user", _user)

    class _Req:
        cookies: dict = {}

    body = auth.ChangePasswordRequest(
        current_password="old-password-1", new_password="new-password-12"
    )
    result = asyncio.run(
        auth.change_password(body, _Req(), authorization="Bearer token")
    )

    assert result["status"] == "ok"
    revocations = conn.statements("password_reset_tokens")
    assert revocations, "a password change left every outstanding reset link live"
    revocation = revocations[0]
    assert "consumed_at = now()" in revocation
    # Only links that could still be used — already-consumed and expired rows
    # are not ours to restamp.
    assert "consumed_at IS NULL" in revocation
    assert "expires_at > now()" in revocation


def test_the_password_write_and_the_revocation_share_one_transaction(monkeypatch):
    """Two transactions could leave the hash changed and the links still live."""
    conn = _Conn()
    entered = 0

    @asynccontextmanager
    async def tx(_ctx):
        nonlocal entered
        entered += 1
        yield conn

    monkeypatch.setattr("db.connection.tenant_tx", tx, raising=False)
    monkeypatch.setattr(auth, "decode_token", lambda _t: {"sub": AGENT})
    monkeypatch.setattr(auth, "_verify_pw", lambda _p, _h: True)
    monkeypatch.setattr(auth, "_hash_pw", lambda _p: "new-hash")

    async def _user(_agent_id):
        return {"id": USER_ID, "password_hash": "old-hash"}

    monkeypatch.setattr(auth, "_lookup_user", _user)

    class _Req:
        cookies: dict = {}

    asyncio.run(
        auth.change_password(
            auth.ChangePasswordRequest(
                current_password="old-password-1", new_password="new-password-12"
            ),
            _Req(),
            authorization="Bearer token",
        )
    )

    assert entered == 1
    assert conn.statements("UPDATE users SET password_hash")
    assert conn.statements("password_reset_tokens")


def test_a_wrong_current_password_changes_nothing(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr("db.connection.tenant_tx", _fake_tx(conn), raising=False)
    monkeypatch.setattr(auth, "decode_token", lambda _t: {"sub": AGENT})
    monkeypatch.setattr(auth, "_verify_pw", lambda _p, _h: False)

    async def _user(_agent_id):
        return {"id": USER_ID, "password_hash": "old-hash"}

    monkeypatch.setattr(auth, "_lookup_user", _user)

    class _Req:
        cookies: dict = {}

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            auth.change_password(
                auth.ChangePasswordRequest(
                    current_password="wrong-password", new_password="new-password-12"
                ),
                _Req(),
                authorization="Bearer token",
            )
        )

    assert excinfo.value.status_code == 403
    # Neither the hash nor the reset links may move on a failed attempt.
    assert conn.executed == []


def test_the_reset_path_retires_sibling_links():
    """Pinned against the source, since the CTE is one statement."""
    import inspect

    source = inspect.getsource(auth.reset_password)
    assert "WHERE user_id = $1 AND consumed_at IS NULL" in source, (
        "using one reset link must retire the rest"
    )
