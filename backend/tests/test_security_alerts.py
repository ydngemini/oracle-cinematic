"""Privileged activity is reported out of band (ISO 27001 A.8.15/A.8.16,
ledger L17-09): platform-admin sign-ins, repeated failures, a rejected second
factor, role changes and owner creation, and a changed operator second factor.
The email carries the account id, the IP and the time, never a passphrase or a
code; the log line carries neither the account id nor the IP. No real send:
ops_alerts.send_email is replaced by a recorder."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException, Response

import auth
import ops_alerts
import security_alerts

OPERATOR = "ops.owner@example.test"
PASSPHRASE = "operator-passphrase-correct-horse"
IP = "203.0.113.77"


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def _send(subject, body, *, to=None):
        out.append({"subject": subject, "body": body, "to": to})
        return None

    monkeypatch.setattr(ops_alerts, "send_email", _send)
    monkeypatch.setenv("ORACLE_SECURITY_ALERT_EMAIL", "alerts@example.test")
    monkeypatch.delenv("ORACLE_SECURITY_ALERTS_ENABLED", raising=False)
    monkeypatch.delenv("ORACLE_SECURITY_ALERT_FAILURE_THRESHOLD", raising=False)
    security_alerts._failures.clear()
    security_alerts._last_failure_alert.clear()
    return out


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(auth, "_ADMIN_ID", OPERATOR)
    monkeypatch.setitem(auth.DEMO_CREDENTIALS, OPERATOR, PASSPHRASE)
    monkeypatch.setitem(auth.DEMO_TENANCY, OPERATOR, (auth.PLATFORM_TENANT_ID, "platform_admin"))
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "")

    async def _limit(_agent, _client=None):
        return 10, 9, 0

    async def _otp(_agent, otp):
        if otp != "123456":
            raise HTTPException(401, auth._OTP_INVALID)

    async def _no_user(_agent):
        return None

    import rate_limit_middleware

    monkeypatch.setattr(auth, "_check_rate_limit", _limit)
    monkeypatch.setattr(auth, "_require_operator_otp", _otp)
    monkeypatch.setattr(auth, "_lookup_user", _no_user)
    monkeypatch.setattr(rate_limit_middleware, "_get_client_ip", lambda _r: IP)


def _login(agent_id, passphrase, otp=None):
    from fastapi import Request

    request = Request({"type": "http", "headers": [], "client": (IP, 1234)})

    async def run():
        try:
            return await auth.login(auth.LoginRequest(agent_id=agent_id, passphrase=passphrase, otp=otp),
                                    Response(), request)
        finally:
            await security_alerts.drain()

    return asyncio.run(run())


def _assert_no_secrets(text):
    for secret in (PASSPHRASE, "123456", "654321", "JBSWY3DPEHPK3PXP"):
        assert secret not in text


def test_operator_sign_in_is_alerted_with_id_ip_and_time_only(sent, operator, caplog):
    with caplog.at_level(logging.WARNING, logger="oracle.security.alert"):
        result = _login(OPERATOR, PASSPHRASE, "123456")
    assert result.role == "platform_admin"
    (mail,) = sent
    assert mail["to"] == "alerts@example.test" and "platform_admin signed in" in mail["subject"]
    assert OPERATOR in mail["body"] and IP in mail["body"] and "Time (UTC)" in mail["body"]
    _assert_no_secrets(mail["body"])
    line = json.loads(caplog.records[-1].getMessage())
    assert line["security_alert"] == "privileged_sign_in" and line["actor"].startswith("actor:")
    assert OPERATOR not in caplog.text and IP not in caplog.text


def test_a_rejected_second_factor_alerts_at_once(sent, operator):
    with pytest.raises(HTTPException):
        _login(OPERATOR, PASSPHRASE, "654321")
    (mail,) = sent
    assert "second factor rejected after a correct passphrase" in mail["subject"]
    _assert_no_secrets(mail["body"])


def test_repeated_wrong_passphrases_alert_once_per_window(sent, operator):
    for _ in range(4):
        with pytest.raises(HTTPException):
            _login(OPERATOR, "wrong-guess-1")
    assert sent == []                                  # below the threshold of 5
    for _ in range(6):
        with pytest.raises(HTTPException):
            _login(OPERATOR, "wrong-guess-2")
    (mail,) = sent                                     # one email, not seven
    assert "5 failed platform-admin sign-ins" in mail["subject"]
    assert "wrong-guess" not in mail["body"]


def test_a_db_platform_admin_counts_and_an_ordinary_account_does_not(sent, operator, monkeypatch):
    monkeypatch.setenv("ORACLE_SECURITY_ALERT_FAILURE_THRESHOLD", "1")
    rows = {"admin2@example.test": {"role": "platform_admin", "password_hash": auth._DUMMY_PASSWORD_HASH},
            "agent@example.test": {"role": "agent", "password_hash": auth._DUMMY_PASSWORD_HASH}}

    async def _lookup(agent_id):
        return rows.get(agent_id)

    monkeypatch.setattr(auth, "_lookup_user", _lookup)
    for who in rows:
        with pytest.raises(HTTPException):
            _login(who, "not-the-password")
    assert [m["body"].count("admin2@example.test") for m in sent] == [1]


def test_the_operator_hitting_the_login_rate_limit_is_alerted(sent, operator, monkeypatch):
    async def _limited(_agent, _client=None):
        raise HTTPException(429, "Too many login attempts. Try again later.")

    monkeypatch.setattr(auth, "_check_rate_limit", _limited)
    for _ in range(3):
        with pytest.raises(HTTPException):
            _login(OPERATOR, "x")
    (mail,) = sent
    assert "rate-limited" in mail["subject"]


def test_role_changes_and_new_owners_are_alerted(sent, monkeypatch):
    async def run():
        security_alerts.role_changed(account="agent@example.test", prior_role="agent", new_role="broker_owner",
                                     changed_by="owner@example.test", tenant_id=str(uuid.uuid4()), ip=IP,
                                     via="two-person role change executed")
        auth._alert_new_owner("new.owner@example.test", str(uuid.uuid4()), None, via="self-serve signup")
        await security_alerts.drain()

    asyncio.run(run())
    change, signup = sent
    assert "agent -> broker_owner" in change["subject"] and "owner@example.test" in change["body"]
    assert "(new account) -> broker_owner" in signup["body"] and "self-serve signup" in signup["body"]


def test_the_executed_role_change_route_alerts():
    import inspect

    import admin_ops

    src = inspect.getsource(admin_ops.execute_role_change)
    assert "security_alerts.role_changed(" in src and "business_reason" not in src.split("role_changed(")[1]
    accept = inspect.getsource(auth.accept_invite)
    assert '_alert_new_owner(email, tenant_id, request, via="owner invitation accepted")' in accept
    assert "_alert_new_owner(" in inspect.getsource(auth.register)


def test_alerts_can_be_switched_off(sent, operator, monkeypatch):
    monkeypatch.setenv("ORACLE_SECURITY_ALERTS_ENABLED", "0")
    _login(OPERATOR, PASSPHRASE, "123456")
    assert sent == []


# ── operator second factor changes between boots ─────────────────────────

class _Ledger:
    def __init__(self):
        self.rows = []

    async def record(self, **kw):
        self.rows.append(kw)


def _factor_env(monkeypatch, ledger):
    import audit_ledger
    import db.connection as dbc

    class _Conn:
        async def fetchval(self, query, *args):
            if "advisory" in query:
                return True
            if "FROM audit_ledger" in query:
                return json.dumps(ledger.rows[-1]["metadata"]) if ledger.rows else None
            raise AssertionError(query)

    class _Pool:
        @asynccontextmanager
        async def acquire(self, timeout=None):
            yield _Conn()

    @asynccontextmanager
    async def chain():
        yield _Conn()

    monkeypatch.setattr(dbc, "get_platform_pool", lambda: _Pool())
    monkeypatch.setattr(audit_ledger, "_global_chain_conn", chain)
    monkeypatch.setattr(audit_ledger, "ledger", ledger)


def test_a_changed_operator_factor_is_recorded_and_alerted_without_the_secret(sent, monkeypatch):
    ledger = _Ledger()
    _factor_env(monkeypatch, ledger)

    def check(factor, material):
        return asyncio.run(security_alerts.check_operator_factor(
            admin_id=OPERATOR, factor=factor, material=material))

    assert check("totp", "JBSWY3DPEHPK3PXP") == "baseline" and sent == []
    assert check("totp", "JBSWY3DPEHPK3PXP") == "unchanged"
    assert check("totp", "KRSXG5CTMVRXEZLU") == "changed"      # re-provisioned seed
    assert check("email", "ops.owner@example.test") == "changed"
    assert [m["subject"] for m in sent] == ["[Neoh security] operator second factor changed"] * 2
    assert "totp -> email" in sent[1]["body"]
    stored = json.dumps([r["metadata"] for r in ledger.rows])
    for material in ("JBSWY3DPEHPK3PXP", "KRSXG5CTMVRXEZLU"):
        assert material not in stored and material not in json.dumps(sent)
