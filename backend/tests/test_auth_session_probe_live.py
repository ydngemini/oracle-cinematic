"""/auth/session and /auth/verify judge a session by the protected-route rules
on real PostgreSQL (audits 28.5, 28.6): epoch, active account and role through
app_begin_session. Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def _request(token):
    return Request({"type": "http", "method": "GET", "path": "/auth/session",
                    "headers": [(b"cookie", f"oracle_session={token}".encode())]})


def test_session_probe_and_verify_follow_the_account(monkeypatch):
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import auth
    from db import connection

    tenant, uid = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"probe-{tenant[:8]}@live.test"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)

    async def state(token):
        result = await auth.session_status(_request(token), Response())
        try:
            await auth.verify(_request(token), authorization=f"Bearer {token}")
            verified = 200
        except HTTPException as exc:
            verified = exc.status_code
        return result.authenticated, verified

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Probe live')", tenant, f"pr-{tenant[:8]}")
        await admin.execute("INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,$3,'agent',$3,'x')", uid, tenant, email)
        token = auth._issue_jwt(email, tenant, "agent", user_id=uid, session_epoch=0)
        assert await state(token) == (True, 200)
        # A token without uid for a real account is checked by agent_id, not trusted.
        no_uid = auth._issue_jwt(email, tenant, "agent")
        assert await state(no_uid) == (True, 200)
        # Claiming a role the account does not have is refused.
        assert await state(auth._issue_jwt(email, tenant, "broker_owner", user_id=uid)) == (False, 401)
        # Logged out elsewhere (epoch moved): both refuse, including the uid-less token.
        await admin.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id=$1", uid)
        assert await state(token) == (False, 401)
        assert await state(auth._issue_jwt(email, tenant, "agent", user_id=uid, session_epoch=1)) == (True, 200)
        # Deactivated.
        await admin.execute("UPDATE users SET is_active = false WHERE id=$1", uid)
        assert await state(auth._issue_jwt(email, tenant, "agent", user_id=uid, session_epoch=1)) == (False, 401)
    finally:
        await admin.execute("DELETE FROM users WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()
