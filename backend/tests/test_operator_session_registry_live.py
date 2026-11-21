"""The environment operator's sessions and the emailed-code cool-down, on real
PostgreSQL (external audit 28.3; ledger L17-32; migration 0181).

Runs the real login, logout, session probe, HTTP middleware, WebSocket
admission and tenant transaction against a database built from every
migration, as the application's own logins (oracle_app_login,
oracle_platform_login). Skipped without ORACLE_LIVE_DB_ADMIN_DSN. No mail is
sent (the code sender is replaced; security alerts are off).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

PASSPHRASE = "operator-passphrase-correct-horse"
TENANT = "11111111-1111-1111-1111-111111111111"


class _Socket:
    def __init__(self):
        self.closed = None

    async def close(self, code):
        self.closed = code


def _cookie(response) -> str:
    return re.search(r"oracle_session=([^;]+)", response.headers["set-cookie"]).group(1)


def _request(token, path="/auth/logout"):
    return Request({"type": "http", "method": "POST", "path": path,
                    "headers": [(b"cookie", f"oracle_session={token}".encode())]})


@pytest.fixture
def operator(monkeypatch):
    import auth

    account = f"ops-{uuid.uuid4().hex[:8]}@live.test"
    monkeypatch.setattr(auth, "_ADMIN_ID", account)
    monkeypatch.setitem(auth.DEMO_CREDENTIALS, account, PASSPHRASE)
    monkeypatch.setitem(auth.DEMO_TENANCY, account, (auth.PLATFORM_TENANT_ID, "platform_admin"))
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "")
    monkeypatch.setenv("ORACLE_SECURITY_ALERTS_ENABLED", "0")

    async def _limit(_agent, _client=None):
        return 10, 9, 0

    monkeypatch.setattr(auth, "_check_rate_limit", _limit)
    return account


def test_operator_sessions_are_server_revocable_in_postgres(operator, monkeypatch):
    asyncio.run(_sessions(operator, monkeypatch))


async def _sessions(account, monkeypatch):
    import asyncpg

    import auth
    import operator_sessions
    import tenancy
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)

    async def login() -> tuple[str, str]:
        """Sign in with whatever credential is configured now."""
        response = Response()
        await auth.login(auth.LoginRequest(agent_id=account, passphrase=auth.DEMO_CREDENTIALS[account]), response)
        token = _cookie(response)
        return token, auth.decode_token(token)["jti"]

    async def state(token) -> tuple:
        """(gate, probe, middleware, stream) for one token."""
        ctx = tenancy.require_context(f"Bearer {token}")
        try:
            await tenancy.verify_session_current(ctx)
            gate = 200
        except HTTPException as exc:
            gate = exc.status_code
        except Exception:  # noqa: BLE001 - not a 401: the server's 503/500 handlers answer
            gate = "error"
        try:
            await auth._live_session(token)
            probe = 200
        except HTTPException as exc:
            probe = exc.status_code
        except auth.SessionUnverifiable:
            probe = 503
        refusal = await operator_sessions._verdict(f"Bearer {token}")
        middleware = 200 if refusal is None else refusal.status_code
        socket = _Socket()
        stream = "open" if await tenancy.admit_operator_stream(socket, ctx) else socket.closed
        return gate, probe, middleware, stream

    live = (200, 200, 200, "open")
    ended = (401, 401, 401, 4401)
    try:
        token1, jti1 = await login()
        row = await admin.fetchrow(
            "SELECT account, credential_fingerprint, expires_at - issued_at AS ttl, revoked_at "
            "FROM operator_sessions WHERE jti_hash = $1", hashlib.sha256(jti1.encode()).hexdigest())
        assert row["account"] == account and row["revoked_at"] is None
        assert re.fullmatch(r"[0-9a-f]{64}", row["credential_fingerprint"])
        assert abs(row["ttl"].total_seconds() - auth.TOKEN_TTL_SECONDS) < 5
        assert PASSPHRASE not in row["credential_fingerprint"]
        assert await admin.fetchval("SELECT count(*) FROM operator_sessions WHERE jti_hash = $1", jti1) == 0
        assert await state(token1) == live

        # A data transaction runs as the platform admin, through the registry check.
        ctx1 = tenancy.require_context(f"Bearer {token1}")
        async with connection.tenant_tx(ctx1) as conn:
            assert await conn.fetchval("SELECT current_setting('app.current_role')") == "platform_admin"
            assert await conn.fetchval("SELECT app_is_platform_admin()") is True

        def revoked(jti):
            return admin.fetchval("SELECT revoked_at IS NOT NULL FROM operator_sessions WHERE jti_hash = $1",
                                  hashlib.sha256(jti.encode()).hexdigest())

        # Rotating the passphrase ends the session at once.
        monkeypatch.setitem(auth.DEMO_CREDENTIALS, account, PASSPHRASE + "-rotated")
        assert await state(token1) == ended
        with pytest.raises(asyncpg.exceptions.InvalidAuthorizationSpecificationError):
            async with connection.tenant_tx(ctx1):
                pass
        # A process starting with the new credential revokes what the old one
        # issued, so rolling the credential back does not revive it (review r1).
        assert not await revoked(jti1)
        await operator_sessions.boot()
        assert await revoked(jti1)
        monkeypatch.setitem(auth.DEMO_CREDENTIALS, account, PASSPHRASE)
        assert await state(token1) == ended
        # The same through a sign-in under the new credential, with no restart.
        token_a, jti_a = await login()
        assert await state(token_a) == live
        monkeypatch.setitem(auth.DEMO_CREDENTIALS, account, PASSPHRASE + "-rotated-again")
        token_b, jti_b = await login()
        assert await revoked(jti_a) and not await revoked(jti_b)
        monkeypatch.setitem(auth.DEMO_CREDENTIALS, account, PASSPHRASE)
        assert await state(token_a) == ended and await state(token_b) == ended

        # Logout ends this session and the operator's other one.
        token1, _ = await login()
        token2, jti2 = await login()
        assert await state(token1) == live and await state(token2) == live
        out = await auth.logout(_request(token1), Response())
        assert out.status_code == 204
        assert await state(token1) == ended and await state(token2) == ended
        assert await admin.fetchval(
            "SELECT count(*) FROM operator_sessions WHERE account = $1 AND revoked_at IS NULL", account) == 0

        # A dead token cannot sign the operator out of a new session.
        token3, jti3 = await login()
        await auth.logout(_request(token1), Response())
        assert await state(token3) == live

        # A row only moves towards ended (review r1): the app may set
        # revoked_at and nothing else, and no role revives or extends one.
        h2, h3 = (hashlib.sha256(j.encode()).hexdigest() for j in (jti2, jti3))
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="cannot be revived"):
            async with connection.tenant_tx(auth._admin_ctx()) as conn:
                await conn.execute("UPDATE operator_sessions SET revoked_at = NULL WHERE jti_hash = $1", h2)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="permission denied"):
            async with connection.tenant_tx(auth._admin_ctx()) as conn:
                await conn.execute("UPDATE operator_sessions SET expires_at = expires_at + interval '7 days' "
                                   "WHERE jti_hash = $1", h3)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="cannot be revived"):
            await admin.execute("UPDATE operator_sessions SET revoked_at = NULL WHERE jti_hash = $1", h2)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="cannot be extended"):
            await admin.execute("UPDATE operator_sessions SET expires_at = expires_at + interval '7 days' "
                                "WHERE jti_hash = $1", h3)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="identity cannot change"):
            await admin.execute("UPDATE operator_sessions SET credential_fingerprint = repeat('0', 64) "
                                "WHERE jti_hash = $1", h3)
        assert await state(token2) == ended and await state(token3) == live

        # Expiry is the database's too (shortening is allowed).
        await admin.execute("UPDATE operator_sessions SET expires_at = issued_at + interval '1 millisecond' "
                            "WHERE jti_hash = $1", h3)
        assert await state(token3) == ended

        # No jti (a token from before 0181), or a jti with no row: not a session.
        with pytest.raises(HTTPException) as exc:
            tenancy.require_context(f"Bearer {auth._issue_jwt(account, auth.PLATFORM_TENANT_ID, 'platform_admin')}")
        assert exc.value.status_code == 401
        forged = auth._issue_jwt(account, auth.PLATFORM_TENANT_ID, "platform_admin", extra={"jti": "f" * 43})
        assert await state(forged) == ended

        # The registry cannot be read: refused, and not reported as signed out
        # (the gate's error reaches the server's 503/500 handlers, never a 200).
        token4, _ = await login()
        await admin.execute("REVOKE SELECT ON operator_sessions FROM oracle_app")
        try:
            assert await state(token4) == ("error", 503, 503, 1011)
        finally:
            await admin.execute("GRANT SELECT ON operator_sessions TO oracle_app")
        assert await state(token4) == live

        # Platform-only: a brokerage context sees no row and cannot write one,
        # even after forging the admin GUC on its own connection.
        broker = tenancy.TenantContext("owner@live.test", TENANT, tenancy.Role.BROKER_OWNER)
        async with connection.tenant_tx(broker) as conn:
            assert await conn.fetchval("SELECT count(*) FROM operator_sessions") == 0
            await conn.execute("SELECT set_config('app.current_role', 'platform_admin', true)")
            assert await conn.fetchval("SELECT count(*) FROM operator_sessions") == 0
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with connection.tenant_tx(broker) as conn:
                await conn.execute(
                    "INSERT INTO operator_sessions (jti_hash, account, credential_fingerprint, expires_at) "
                    "VALUES (repeat('a', 64), 'x', repeat('b', 64), now() + interval '1 hour')")
        # The app never deletes a row (no DELETE grant), not even as the platform.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with connection.tenant_tx(auth._admin_ctx()) as conn:
                await conn.execute("DELETE FROM operator_sessions WHERE account = $1", account)
    finally:
        await admin.execute("DELETE FROM operator_sessions WHERE account = $1", account)
        await admin.close()
        await connection.close_pool()


def test_a_retired_code_pauses_new_codes_for_15_minutes_in_postgres(monkeypatch):
    asyncio.run(_cooldown(monkeypatch))


async def _cooldown(monkeypatch):
    import asyncpg

    import auth
    import tenancy
    from db import connection

    account = f"ops-{uuid.uuid4().hex[:8]}@otp.test"
    other = f"ops-{uuid.uuid4().hex[:8]}@otp.test"
    sent: list[str] = []
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "owner@example.test")
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_send_operator_code", lambda _to, code: sent.append(code))

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    lock_key = "SELECT {}(hashtextextended('operator-otp:' || $1, 0))"
    try:
        # Two code requests at once (review r1): both used to see "no recent
        # code", both inserted, two emails went out and the lock later retired
        # only one challenge. Hold the per-account lock so they overlap.
        await admin.execute(lock_key.format("pg_advisory_lock"), account)
        racers = [asyncio.create_task(auth._require_operator_email_otp(account, None)) for _ in range(2)]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in racers), "both requests must wait for the account's lock"
        await admin.execute(lock_key.format("pg_advisory_unlock"), account)
        outcomes = await asyncio.gather(*racers, return_exceptions=True)
        assert all(isinstance(o, HTTPException) and o.detail["code"] == "OTP_REQUIRED" for o in outcomes), outcomes
        assert len(sent) == 1, "one code, one email"
        assert await admin.fetchval("SELECT count(*) FROM operator_otp_challenges "
                                    "WHERE account = $1 AND consumed_at IS NULL", account) == 1
        # And the database refuses a second live challenge on any path.
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await admin.execute("INSERT INTO operator_otp_challenges (account, code_hmac, expires_at) "
                                "VALUES ($1, 'x', now() + interval '10 minutes')", account)

        wrong = "000000" if sent[0] != "000000" else "111111"
        statuses = []
        for _ in range(5):
            with pytest.raises(HTTPException) as refused:
                await auth._require_operator_email_otp(account, wrong)
            statuses.append(refused.value.status_code)
        assert statuses == [401, 401, 401, 401, 429]
        assert await admin.fetchval(
            "SELECT locked_at IS NOT NULL FROM operator_otp_challenges WHERE account = $1", account)
        # Five more guesses (the old ten-guess path): each refused 429 and not counted.
        for _ in range(5):
            with pytest.raises(HTTPException) as more:
                await auth._require_operator_email_otp(account, wrong)
            assert more.value.status_code == 429
        # A live challenge that escaped the lock (planted with triggers off,
        # as a racing twin would have been) does not take a code, not even its right one.
        async with admin.transaction():
            await admin.execute("SET LOCAL session_replication_role = replica")
            twin = await admin.fetchval(
                "INSERT INTO operator_otp_challenges (account, code_hmac, expires_at) "
                "VALUES ($1, '', now() + interval '10 minutes') RETURNING id::text", account)
            await admin.execute("UPDATE operator_otp_challenges SET code_hmac = $2 WHERE id = $1::uuid",
                                twin, auth._otp_hmac(twin, "424242"))
        with pytest.raises(HTTPException) as twin_refused:
            await auth._require_operator_email_otp(account, "424242")
        assert twin_refused.value.status_code == 429
        assert await admin.fetchrow("SELECT attempts, consumed_at FROM operator_otp_challenges WHERE id = $1::uuid",
                                    twin) == (0, None)
        await admin.execute("UPDATE operator_otp_challenges SET consumed_at = now() WHERE id = $1::uuid", twin)

        # The second-factor tables are platform-only (review r1): a brokerage
        # connection reads no code HMAC and cannot clear the lock.
        broker = tenancy.TenantContext("owner@live.test", TENANT, tenancy.Role.BROKER_OWNER)
        async with connection.tenant_tx(broker) as conn:
            assert await conn.fetchval("SELECT count(*) FROM operator_otp_challenges") == 0
            assert await conn.fetchval("SELECT count(*) FROM operator_totp_uses") == 0
            assert await conn.execute("UPDATE operator_otp_challenges SET locked_at = NULL "
                                      "WHERE account = $1", account) == "UPDATE 0"
            await conn.execute("SELECT set_config('app.current_role', 'platform_admin', true)")
            assert await conn.fetchval("SELECT count(*) FROM operator_otp_challenges") == 0
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with connection.tenant_tx(broker) as conn:
                await conn.execute("INSERT INTO operator_otp_challenges (account, code_hmac, expires_at) "
                                   "VALUES ('x@x.test', 'x', now() + interval '10 minutes')")
        assert await admin.fetchval("SELECT count(*) FROM operator_otp_challenges "
                                    "WHERE account = $1 AND locked_at IS NOT NULL", account) == 1

        # A fresh code is refused, on any replica: the database says no.
        with pytest.raises(HTTPException) as paused:
            await auth._require_operator_email_otp(account, None)
        assert paused.value.status_code == 429 and paused.value.detail == auth._OTP_LOCKED
        assert 1 <= int(paused.value.headers["Retry-After"]) <= auth._EMAIL_OTP_COOLDOWN_SECONDS
        assert len(sent) == 1
        with pytest.raises(asyncpg.PostgresError) as raw:
            async with connection.tenant_tx(auth._admin_ctx()) as conn:
                await conn.execute(
                    "INSERT INTO operator_otp_challenges (account, code_hmac, expires_at) "
                    "VALUES ($1, 'x', now() + interval '10 minutes')", account)
        assert raw.value.sqlstate == "OTPLK"
        assert re.search(r"retry_after_seconds=\d+", raw.value.detail or "")

        # The cool-down is the account's, not everyone's.
        with pytest.raises(HTTPException) as fresh_other:
            await auth._require_operator_email_otp(other, None)
        assert fresh_other.value.detail["code"] == "OTP_REQUIRED" and len(sent) == 2

        # Fifteen minutes later a code is sent again.
        await admin.execute("UPDATE operator_otp_challenges SET locked_at = now() - interval '901 seconds' "
                            "WHERE account = $1 AND locked_at IS NOT NULL", account)
        with pytest.raises(HTTPException) as fresh:
            await auth._require_operator_email_otp(account, None)
        assert fresh.value.status_code == 401 and fresh.value.detail["code"] == "OTP_REQUIRED"
        assert len(sent) == 3
    finally:
        await admin.execute("DELETE FROM operator_otp_challenges WHERE account = ANY($1::text[])",
                            [account, other])
        await admin.close()
        await connection.close_pool()


def test_the_totp_path_still_records_steps_under_rls(monkeypatch):
    asyncio.run(_totp(monkeypatch))


async def _totp(monkeypatch):
    import time

    import asyncpg

    import auth
    import tenancy
    import totp
    from db import connection

    account = f"ops-{uuid.uuid4().hex[:8]}@totp.test"
    secret = "JBSWY3DPEHPK3PXP"
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", secret)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    try:
        code = totp.code_at(secret, int(time.time() // 30))
        await auth._require_operator_otp(account, code)          # the platform context writes the step
        with pytest.raises(HTTPException) as replay:
            await auth._require_operator_otp(account, code)
        assert replay.value.status_code == 401
        assert await admin.fetchval("SELECT count(*) FROM operator_totp_uses WHERE account = $1", account) == 1
        broker = tenancy.TenantContext("owner@live.test", TENANT, tenancy.Role.BROKER_OWNER)
        async with connection.tenant_tx(broker) as conn:
            assert await conn.fetchval("SELECT count(*) FROM operator_totp_uses") == 0
    finally:
        await admin.execute("DELETE FROM operator_totp_uses WHERE account = $1", account)
        await admin.close()
        await connection.close_pool()
