"""The environment operator's sessions are server-side (external audit 28.3)
and a retired emailed code pauses new codes for 15 minutes (ledger L17-32).

The operator (ORACLE_ADMIN_ID) has no users row, so 0117's epoch never
covered it: logout only cleared the cookie and a copied operator token stayed
valid until it expired, through logout and a passphrase rotation. Every
operator session is now a row in operator_sessions (0181), proved on every
tenant transaction, every HTTP request (OperatorSessionMiddleware), every
privileged gate and WebSocket admission, and revoked by logout.

These run on fakes, without a database. The SQL itself (the registry check,
RLS, the cool-down trigger) is proved on real PostgreSQL by
tests/test_operator_session_registry_live.py. No mail is sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import jwt
import pytest
from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from fastapi.testclient import TestClient

import auth
import operator_sessions
import tenancy
from tenancy import Role, TenantContext

OPS = "ops.owner@example.test"
PASSPHRASE = "operator-passphrase-correct-horse"
PLATFORM = auth.PLATFORM_TENANT_ID
TENANT = "11111111-1111-1111-1111-111111111111"
UID = "33333333-3333-3333-3333-333333333333"
JTI = "k" * 43
MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0181_operator_session_registry.sql"


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(auth, "_ADMIN_ID", OPS)
    monkeypatch.setitem(auth.DEMO_CREDENTIALS, OPS, PASSPHRASE)
    monkeypatch.setitem(auth.DEMO_TENANCY, OPS, (PLATFORM, "platform_admin"))
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "")


def _operator_token(**extra) -> str:
    return auth._issue_jwt(OPS, PLATFORM, "platform_admin", extra=extra or None)


def _operator_ctx(jti=JTI) -> TenantContext:
    return TenantContext(OPS, PLATFORM, Role.PLATFORM_ADMIN, operator_session=jti,
                         expires_at=time.time() + 3600)


def _fake_tx(monkeypatch, mode: str, entered: list):
    """tenant_tx as the database would answer app_begin_operator_session."""
    import db.connection

    class _Conn:
        async def fetchrow(self, *_a):
            return None

        async def execute(self, *_a):
            return "OK"

    @asynccontextmanager
    async def tx(ctx):
        entered.append(ctx)
        if mode == "revoked":
            raise asyncpg.exceptions.InvalidAuthorizationSpecificationError("session is no longer valid")
        if mode == "db_login_failed":
            raise asyncpg.exceptions.InvalidPasswordError("password authentication failed")
        if mode == "down":
            raise OSError("connection refused")
        yield _Conn()

    monkeypatch.setattr(db.connection, "tenant_tx", tx)


# ── which tokens are operator sessions ───────────────────────────────────

def test_an_operator_token_without_a_jti_is_not_a_session(operator):
    """Tokens issued before 0181 carry no jti and could never be revoked:
    they are refused, so none outlives the deploy."""
    with pytest.raises(HTTPException) as exc:
        tenancy.require_context(f"Bearer {_operator_token()}")
    assert exc.value.status_code == 401
    for bad in ("short", "x" * 200, "has spaces in it and is long enough....", 12345):
        with pytest.raises(HTTPException):
            tenancy.require_context(f"Bearer {_operator_token(jti=bad)}")


def test_an_operator_token_names_its_registry_session(operator):
    ctx = tenancy.require_context(f"Bearer {_operator_token(jti=JTI)}")
    assert ctx.operator_session == JTI
    # No users row: the code that asks "is there an account row?" is unchanged.
    assert ctx.verify_session is False


def test_a_demo_identity_that_is_not_an_operator_keeps_its_behaviour(monkeypatch):
    monkeypatch.setitem(auth.DEMO_TENANCY, "demo@example.test", (TENANT, "agent"))
    ctx = tenancy.require_context(f"Bearer {auth._issue_jwt('demo@example.test', TENANT, 'agent')}")
    assert ctx.operator_session is None and ctx.verify_session is False


def test_a_demo_identity_configured_as_platform_admin_is_an_operator(monkeypatch):
    monkeypatch.setitem(auth.DEMO_TENANCY, "demo-admin@example.test", (TENANT, "platform_admin"))
    with pytest.raises(HTTPException) as exc:
        tenancy.require_context(f"Bearer {auth._issue_jwt('demo-admin@example.test', TENANT, 'platform_admin')}")
    assert exc.value.status_code == 401
    assert operator_sessions.is_registry_identity("demo-admin@example.test", TENANT, Role.PLATFORM_ADMIN)


def test_an_account_sharing_the_operator_address_elsewhere_is_an_ordinary_account(operator):
    ctx = tenancy.require_context(f"Bearer {auth._issue_jwt(OPS, TENANT, 'broker_owner', user_id=UID)}")
    assert ctx.verify_session is True and ctx.operator_session is None


# ── the registry check rides every tenant transaction ────────────────────

def test_every_tenant_transaction_of_an_operator_session_checks_the_registry(operator):
    calls = []

    class _Conn:
        async def execute(self, query, *args):
            calls.append((query, args))

    asyncio.run(tenancy.apply_rls_context(_Conn(), _operator_ctx()))
    (query, args), = calls
    assert "app_begin_operator_session" in query and "app_begin_session(" not in query
    fingerprint = asyncio.run(operator_sessions.credential_fingerprint(OPS))
    assert args == (PLATFORM, "platform_admin", OPS, hashlib.sha256(JTI.encode()).hexdigest(),
                    fingerprint, "actor")
    assert JTI not in repr(args)  # the database sees only the hash


def test_the_fingerprint_follows_the_credential_and_reveals_none_of_it(operator, monkeypatch):
    fp = lambda agent=OPS: asyncio.run(operator_sessions.credential_fingerprint(agent))  # noqa: E731
    first = fp()
    assert re.fullmatch(r"[0-9a-f]{64}", first) and fp() == first
    assert PASSPHRASE not in first
    monkeypatch.setitem(auth.DEMO_CREDENTIALS, OPS, PASSPHRASE + "-rotated")
    rotated = fp()
    assert rotated != first
    monkeypatch.setitem(auth.DEMO_CREDENTIALS, OPS, PASSPHRASE)
    assert fp() == first
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    assert fp() not in (first, rotated)
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "owner@example.test")
    assert fp() != first
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "")
    monkeypatch.setattr(auth, "SECRET_KEY", "another-test-secret-key-with-32-bytes-or-more")
    assert fp() != first
    # No credential configured any more: matches no row.
    assert fp("nobody@example.test") == ""


def test_verify_session_current_proves_an_operator_session(operator, monkeypatch):
    entered = []
    _fake_tx(monkeypatch, "revoked", entered)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tenancy.verify_session_current(_operator_ctx()))
    assert exc.value.status_code == 401 and entered


@pytest.mark.parametrize("mode, code", [("revoked", 4401), ("down", 1011)])
def test_an_operator_stream_is_refused_before_accept(operator, monkeypatch, mode, code):
    entered = []
    _fake_tx(monkeypatch, mode, entered)

    class _Socket:
        closed = None

        async def close(self, code):
            self.closed = code

    socket = _Socket()
    assert asyncio.run(tenancy.admit_operator_stream(socket, _operator_ctx())) is False
    assert socket.closed == code and entered


# ── login, logout, renewal ────────────────────────────────────────────────

def _login_env(monkeypatch, registered: list):
    async def _limit(_agent, _client=None):
        return 10, 9, 0

    async def _register(agent_id):
        registered.append(agent_id)
        return JTI

    async def _no_user(_agent):
        return None

    import security_alerts

    monkeypatch.setattr(auth, "_check_rate_limit", _limit)
    monkeypatch.setattr(auth, "_lookup_user", _no_user)
    monkeypatch.setattr(operator_sessions, "register", _register)
    monkeypatch.setattr(security_alerts, "enabled", lambda: False)


def _login(agent_id, passphrase):
    response = Response()
    result = asyncio.run(auth.login(auth.LoginRequest(agent_id=agent_id, passphrase=passphrase), response))
    cookie = re.search(r"oracle_session=([^;]+)", response.headers["set-cookie"]).group(1)
    return result, jwt.decode(cookie, options={"verify_signature": False})


def test_operator_login_registers_a_session_and_signs_its_jti(operator, monkeypatch):
    registered = []
    _login_env(monkeypatch, registered)
    result, claims = _login(OPS, PASSPHRASE)
    assert registered == [OPS] and result.role == "platform_admin"
    assert claims["jti"] == JTI and "uid" not in claims


def test_operator_login_fails_when_the_registry_cannot_be_written(operator, monkeypatch):
    _login_env(monkeypatch, [])

    async def _down(_agent_id):
        raise OSError("connection refused")

    monkeypatch.setattr(operator_sessions, "register", _down)
    with pytest.raises(OSError):
        _login(OPS, PASSPHRASE)


def test_a_demo_login_that_is_not_an_operator_registers_nothing(monkeypatch):
    registered = []
    _login_env(monkeypatch, registered)
    monkeypatch.setitem(auth.DEMO_CREDENTIALS, "demo@example.test", PASSPHRASE)
    monkeypatch.setitem(auth.DEMO_TENANCY, "demo@example.test", (TENANT, "agent"))
    _result, claims = _login("demo@example.test", PASSPHRASE)
    assert registered == [] and "jti" not in claims


def test_logout_revokes_the_operator_session(operator, monkeypatch):
    revoked = []

    async def _revoke(payload):
        revoked.append(payload)
        return 1

    monkeypatch.setattr(operator_sessions, "revoke", _revoke)
    token = _operator_token(jti=JTI)
    request = Request({"type": "http", "method": "POST", "path": "/auth/logout",
                       "headers": [(b"cookie", f"oracle_session={token}".encode())]})
    response = asyncio.run(auth.logout(request, Response()))
    assert response.status_code == 204
    assert [p["jti"] for p in revoked] == [JTI]
    assert 'oracle_session=""' in response.headers["set-cookie"]


def test_revoke_ignores_a_token_that_is_not_an_operator_session(operator, monkeypatch):
    entered = []
    _fake_tx(monkeypatch, "live", entered)
    user = {"sub": "a@x.test", "tenant_id": TENANT, "role": "agent", "jti": JTI}
    no_jti = {"sub": OPS, "tenant_id": PLATFORM, "role": "platform_admin"}
    assert asyncio.run(operator_sessions.revoke(user)) == 0
    assert asyncio.run(operator_sessions.revoke(no_jti)) == 0
    assert entered == []


def test_policy_renewal_keeps_the_registered_session_and_its_expiry(operator, monkeypatch):
    import policy_acceptance

    _fake_tx(monkeypatch, "live", [])
    ctx = _operator_ctx()
    response = Response()
    asyncio.run(policy_acceptance.policy_acceptance_status(ctx, response))
    cookie = re.search(r"oracle_session=([^;]+)", response.headers["set-cookie"]).group(1)
    claims = jwt.decode(cookie, options={"verify_signature": False})
    assert claims["jti"] == JTI
    assert claims["exp"] <= ctx.expires_at + 1   # never past the registered 24 h


# ── every HTTP request ────────────────────────────────────────────────────

def _client() -> TestClient:
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/thing", ok), Route("/auth/session", ok)])
    app.add_middleware(operator_sessions.OperatorSessionMiddleware)
    return TestClient(app)


@pytest.mark.parametrize("mode, status", [("live", 200), ("revoked", 401), ("down", 503),
                                          ("db_login_failed", 503)])
def test_every_http_request_with_an_operator_token_proves_its_session(operator, monkeypatch, mode, status):
    entered = []
    _fake_tx(monkeypatch, mode, entered)
    client = _client()
    client.cookies.set("oracle_session", _operator_token(jti=JTI))
    response = client.get("/api/thing")
    assert response.status_code == status and len(entered) == 1
    if status == 401:
        assert response.json() == {"detail": "Your session has ended. Sign in again."}
        assert 'oracle_session=""' in response.headers["set-cookie"]
    if status == 503:
        # Fail closed, but not "signed out": the cookie is kept.
        assert response.json()["detail"]["code"] == "session_unverifiable"
        assert "set-cookie" not in response.headers


def test_a_bearer_header_is_judged_before_the_cookie(operator, monkeypatch):
    entered = []
    _fake_tx(monkeypatch, "revoked", entered)
    client = _client()
    response = client.get("/api/thing", headers={"Authorization": f"Bearer {_operator_token(jti=JTI)}"})
    assert response.status_code == 401 and entered


def test_auth_routes_and_other_tokens_are_not_held_up(operator, monkeypatch):
    entered = []
    _fake_tx(monkeypatch, "revoked", entered)
    client = _client()
    # Login and logout must work with a dead cookie; /auth/session judges itself.
    client.cookies.set("oracle_session", _operator_token(jti=JTI))
    assert client.get("/auth/session").status_code == 200
    # A users-table session is the route's business (0117), not this middleware's.
    client.cookies.set("oracle_session", auth._issue_jwt("a@x.test", TENANT, "agent", user_id=UID))
    assert client.get("/api/thing").status_code == 200
    # An unusable token is left to the route, which refuses it itself.
    client.cookies.set("oracle_session", "not-a-token")
    assert client.get("/api/thing").status_code == 200
    client.cookies.clear()
    assert client.get("/api/thing").status_code == 200
    assert entered == []


def test_with_no_operator_configured_nothing_is_decoded(monkeypatch):
    monkeypatch.setattr(auth, "DEMO_TENANCY", {"demo@example.test": (TENANT, "agent")})

    def _boom(*_a, **_k):
        raise AssertionError("decoded a token with no operator configured")

    monkeypatch.setattr(tenancy, "_context_from_authorization", _boom)
    client = _client()
    client.cookies.set("oracle_session", "anything")
    assert client.get("/api/thing").status_code == 200


def test_the_server_installs_the_middleware_inside_the_rate_limiter_and_csrf():
    import server
    from csrf_middleware import CSRFMiddleware
    from rate_limit_middleware import RateLimitMiddleware

    order = [m.cls for m in server.app.user_middleware]   # outermost first
    mine = order.index(operator_sessions.OperatorSessionMiddleware)
    assert mine > order.index(RateLimitMiddleware) and mine > order.index(CSRFMiddleware)


def test_session_probe_proves_an_operator_session(operator, monkeypatch):
    _fake_tx(monkeypatch, "revoked", [])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth._live_session(_operator_token(jti=JTI)))
    assert exc.value.status_code == 401
    _fake_tx(monkeypatch, "down", [])
    with pytest.raises(auth.SessionUnverifiable):
        asyncio.run(auth._live_session(_operator_token(jti=JTI)))


# ── the emailed-code cool-down (L17-32) ──────────────────────────────────

class _CooldownError(Exception):
    sqlstate = "OTPLK"

    def __init__(self, seconds):
        super().__init__("operator sign-in codes are paused after a lockout")
        self.detail = f"retry_after_seconds={seconds}"


class _OtpStore:
    """operator_otp_challenges for the queries auth.py issues, with the 0181
    trigger: an insert within 900 s of a lock raises SQLSTATE OTPLK."""

    def __init__(self):
        self.rows: list[dict] = []
        self.now = 1_000_000.0

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM operator_otp_challenges" in query:
            return None
        if "INSERT INTO operator_otp_challenges" in query:
            locks = [r["locked_at"] for r in self.rows
                     if r["account"] == args[0] and r["locked_at"] and r["locked_at"] > self.now - 900]
            if locks:
                raise _CooldownError(int(max(locks) + 900 - self.now))
            row = {"id": str(uuid.uuid4()), "account": args[0], "code_hmac": "", "attempts": 0,
                   "consumed": False, "locked_at": None}
            self.rows.append(row)
            return row["id"]
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        live = [r for r in self.rows if r["account"] == args[0] and not r["consumed"]]
        return dict(live[-1]) if live else None

    async def execute(self, query, *args):
        if "SET code_hmac" in query:
            next(r for r in self.rows if r["id"] == args[0])["code_hmac"] = args[1]
        elif "SET attempts = attempts + 1" in query:
            assert "locked_at = CASE WHEN attempts + 1 >= $2::int" in query
            r = next(r for r in self.rows if r["id"] == args[0])
            r["attempts"] += 1
            if r["attempts"] >= args[1]:
                r["consumed"], r["locked_at"] = True, self.now
        elif "SET consumed_at = now() WHERE id" in query:
            next(r for r in self.rows if r["id"] == args[0])["consumed"] = True
        elif "SET consumed_at = now()" in query:
            for r in self.rows:
                r["consumed"] = True
        return "OK"


@pytest.fixture
def otp_env(monkeypatch):
    import copy

    import db.connection

    store, sent = _OtpStore(), []

    @asynccontextmanager
    async def tx(_ctx):
        snapshot = copy.deepcopy(store.rows)   # rolled back like PostgreSQL
        try:
            yield store
        except BaseException:
            store.rows[:] = snapshot
            raise

    monkeypatch.setattr(db.connection, "tenant_tx", tx)
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "owner@example.test")
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_send_operator_code", lambda to, code: sent.append(code))
    return store, sent


def _otp(code):
    return asyncio.run(auth._require_operator_email_otp(OPS, code))


def test_the_fifth_wrong_code_pauses_new_codes_for_15_minutes(otp_env, caplog):
    store, sent = otp_env
    with pytest.raises(HTTPException):
        _otp(None)
    code = sent[-1]
    wrong = "000000" if code != "000000" else "111111"
    statuses = []
    with caplog.at_level(logging.WARNING, logger="oracle.auth"):
        for _ in range(5):
            with pytest.raises(HTTPException) as refused:
                _otp(wrong)
            statuses.append(refused.value.status_code)
        assert statuses == [401, 401, 401, 401, 429]
        assert refused.value.detail == auth._OTP_LOCKED
        assert refused.value.headers["Retry-After"] == "900"
        locks = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert locks and "locked" in locks[-1].getMessage()

        store.now += 60
        with pytest.raises(HTTPException) as again:
            _otp(None)                                    # asking for a fresh code
        assert again.value.status_code == 429 and again.value.detail == auth._OTP_LOCKED
        assert again.value.headers["Retry-After"] == "840"
        assert [r for r in caplog.records if r.levelno == logging.WARNING and "paused" in r.getMessage()]
    assert len(sent) == 1, "no code may be sent during the cool-down"
    for record in caplog.records:
        assert OPS not in record.getMessage() and "owner@example.test" not in record.getMessage()


def test_after_the_cool_down_a_new_code_is_sent(otp_env):
    store, sent = otp_env
    with pytest.raises(HTTPException):
        _otp(None)
    wrong = "000000" if sent[-1] != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(HTTPException):
            _otp(wrong)
    store.now += 901
    with pytest.raises(HTTPException) as fresh:
        _otp(None)
    assert fresh.value.status_code == 401 and fresh.value.detail["code"] == "OTP_REQUIRED"
    assert len(sent) == 2


def test_the_lock_sentence_is_fixed_and_names_no_one():
    assert auth._OTP_LOCKED == {"code": "MFA_LOCKED", "message":
                                "Too many wrong codes. Wait 15 minutes, then sign in again for a new code."}
    assert auth._EMAIL_OTP_COOLDOWN_SECONDS // 60 == 15


def test_a_lock_at_sign_in_raises_the_second_factor_alert(operator, monkeypatch):
    import security_alerts

    alerts = []
    _login_env(monkeypatch, [])
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "owner@example.test")

    async def _locked(_agent, _otp):
        raise auth._otp_locked(900)

    monkeypatch.setattr(auth, "_require_operator_email_otp", _locked)
    monkeypatch.setattr(security_alerts, "privileged_sign_in_failed",
                        lambda agent, ip, stage: alerts.append(stage))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.login(auth.LoginRequest(agent_id=OPS, passphrase=PASSPHRASE, otp="123456"), Response()))
    assert exc.value.status_code == 429 and alerts == ["second_factor"]


# ── the migration says what the code relies on ───────────────────────────

def test_the_migration_and_the_code_agree():
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text())
    # The cool-down interval is the constant the 429 sentence and Retry-After use.
    assert re.findall(r"interval '(\d+) seconds'", sql) == [str(auth._EMAIL_OTP_COOLDOWN_SECONDS)] * 2
    assert f"ERRCODE = '{auth._EMAIL_OTP_COOLDOWN_SQLSTATE}'" in sql
    assert "BEFORE INSERT ON operator_otp_challenges" in sql
    # Platform-only, never deleted by the app, the check function granted (0003 revoked PUBLIC).
    assert "ALTER TABLE operator_sessions FORCE ROW LEVEL SECURITY" in sql
    assert "USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin())" in sql
    assert "REVOKE DELETE, TRUNCATE ON operator_sessions FROM oracle_app" in sql
    assert ("GRANT EXECUTE ON FUNCTION app_begin_operator_session(text, text, text, text, text, text) "
            "TO oracle_app") in sql
    # The refusal is 0117's, which the API already maps to 401.
    assert "RAISE EXCEPTION 'session is no longer valid' USING ERRCODE = '28000'" in sql
    assert auth._REVOKED_MESSAGE == "session is no longer valid"
