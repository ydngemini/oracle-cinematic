"""Regression coverage for the post-signup NEOH policy gate."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, Response

import auth
import db.connection
from policy_acceptance import (
    ACCOUNT_SECURITY_ESA_VERSION,
    PLATFORM_POLICY_VERSION,
    AccountSecurityAcceptanceRequest,
    PolicyAcceptanceRequest,
    accept_account_security_esa,
    accept_policy,
    policy_acceptance_status,
    account_security_esa,
)
from tenancy import Role, TenantContext, require_context, require_policy_context


TENANT_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"
CTX = TenantContext(agent_id="new@broker.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)


class _PolicyConn:
    def __init__(self, *, required: bool = True):
        self.user = {"id": USER_ID, "policy_acceptance_required": required}
        self.acceptance = None
        self.security_acceptance = None

    async def fetchrow(self, query, *args):
        if "SELECT id, policy_acceptance_required" in query:
            return self.user
        if "SELECT policy_version, accepted_at" in query:
            return self.acceptance
        if "SELECT agreement_version, accepted_at" in query:
            return self.security_acceptance
        if "INSERT INTO user_policy_acceptances" in query:
            self.acceptance = {
                "policy_version": args[2],
                "accepted_at": datetime.now(timezone.utc),
            }
            return self.acceptance
        if "INSERT INTO account_security_acceptances" in query:
            self.security_acceptance = {
                "agreement_version": args[2],
                "accepted_at": datetime.now(timezone.utc),
            }
            return self.security_acceptance
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query, *_args):
        if "UPDATE users SET policy_acceptance_required = false" in query:
            self.user["policy_acceptance_required"] = False
            return "UPDATE 1"
        raise AssertionError(f"Unexpected execute query: {query}")


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


def test_pending_token_is_rejected_except_by_policy_route():
    token = auth._issue_jwt(CTX.agent_id, CTX.tenant_id, CTX.role.value, extra={"policy_pending": True})

    with pytest.raises(HTTPException) as exc:
        require_context(f"Bearer {token}")
    assert exc.value.status_code == 403
    assert require_policy_context(f"Bearer {token}") == CTX


def test_stale_policy_token_is_rejected_except_by_policy_route():
    token = auth._issue_jwt(CTX.agent_id, CTX.tenant_id, CTX.role.value, extra={"policy_version": "prior-policy"})

    with pytest.raises(HTTPException) as exc:
        require_context(f"Bearer {token}")
    assert exc.value.status_code == 403
    assert require_policy_context(f"Bearer {token}") == CTX


def test_password_reset_token_cannot_open_policy_or_application_routes():
    token = auth._issue_jwt(CTX.agent_id, CTX.tenant_id, CTX.role.value, extra={"purpose": "pwreset"})

    for route in (require_context, require_policy_context):
        with pytest.raises(HTTPException) as exc:
            route(f"Bearer {token}")
        assert exc.value.status_code == 401


def test_policy_status_and_acceptance_exchange_pending_token(monkeypatch):
    conn = _PolicyConn(required=True)
    monkeypatch.setattr(db.connection, "tenant_tx", _fake_tenant_tx(conn))

    before = asyncio.run(policy_acceptance_status(CTX))
    assert before.required is True
    assert before.accepted_at is None
    assert before.account_security_required is True

    accepted = asyncio.run(accept_policy(PolicyAcceptanceRequest(
        policy_version=PLATFORM_POLICY_VERSION,
        account_security_version=ACCOUNT_SECURITY_ESA_VERSION,
    ), CTX))
    assert accepted.accepted is True
    assert accepted.required is False
    assert accepted.account_security_required is False
    assert conn.user["policy_acceptance_required"] is False
    assert auth.decode_token(accepted.token).get("policy_pending") is None
    assert auth.decode_token(accepted.token)["policy_version"] == PLATFORM_POLICY_VERSION

    after = asyncio.run(policy_acceptance_status(CTX))
    assert after.required is False
    assert after.accepted_at is not None
    assert after.account_security_accepted_at is not None
    assert after.policy.title == "NEOH™ Platform Use Policy"
    assert after.token is not None


def test_current_policy_version_requires_reacceptance_when_missing(monkeypatch):
    conn = _PolicyConn(required=False)
    monkeypatch.setattr(db.connection, "tenant_tx", _fake_tenant_tx(conn))

    status = asyncio.run(policy_acceptance_status(CTX))

    assert status.required is True
    assert status.accepted_at is None
    assert status.token is None
    assert len(status.policy.sections) >= 8


def test_policy_acceptance_rejects_stale_version(monkeypatch):
    conn = _PolicyConn(required=True)
    monkeypatch.setattr(db.connection, "tenant_tx", _fake_tenant_tx(conn))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(accept_policy(PolicyAcceptanceRequest(
            policy_version="old-policy",
            account_security_version=ACCOUNT_SECURITY_ESA_VERSION,
        ), CTX))
    assert exc.value.status_code == 422


def test_account_security_acceptance_is_persisted_server_side(monkeypatch):
    conn = _PolicyConn(required=False)
    monkeypatch.setattr(db.connection, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(accept_account_security_esa(
        AccountSecurityAcceptanceRequest(agreement_version=ACCOUNT_SECURITY_ESA_VERSION),
        CTX,
    ))

    assert result.accepted is True
    assert result.required is False
    assert result.agreement_version == ACCOUNT_SECURITY_ESA_VERSION
    assert conn.security_acceptance is not None


class _RegistrationConn:
    def __init__(self):
        self.user_insert = ""

    async def fetchval(self, _query, _email):
        return None

    async def fetchrow(self, query, *_args):
        assert "INSERT INTO tenants" in query
        return {"id": TENANT_ID}

    async def execute(self, query, *_args):
        self.user_insert = query
        return "INSERT 0 1"


def test_registration_marks_the_new_account_policy_pending(monkeypatch):
    conn = _RegistrationConn()
    monkeypatch.setattr(db.connection, "tenant_tx", _fake_tenant_tx(conn))
    auth._session_registry.clear()

    result = asyncio.run(
        auth.register(
            auth.RegisterRequest(email="new@broker.test", password="a-strong-password", full_name="New Broker"),
            Response(),
        )
    )

    assert "policy_acceptance_required" in conn.user_insert
    assert result.policy_acceptance_required is True
    assert auth.decode_token(result.token)["policy_pending"] is True


def test_account_security_esa_endpoint_returns_account_security_form():
    response = asyncio.run(account_security_esa())

    assert response.agreement_version == ACCOUNT_SECURITY_ESA_VERSION
    assert response.agreement.title == "NEOH™ Account Security Agreement (ESA)"
    assert response.agreement.operator == "NEOH™ is operated by YDN LLC."
    assert response.agreement.sections
    assert response.agreement.sections[0].heading == "1. Account integrity"
