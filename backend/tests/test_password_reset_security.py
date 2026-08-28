"""Focused security coverage for password-reset capability tokens."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from fastapi import HTTPException, Response

import auth
import db.connection


TENANT_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"
EMAIL = "broker@example.test"
NEW_PASSWORD = "new-password-2026"
INVALID_RESET_DETAIL = "Reset link is invalid or has expired."


class _PasswordResetStore:
    """Small transactional model for the exact auth.py queries under test."""

    def __init__(self, *, role: str = "agent", policy_required: bool = False):
        self.user = {
            "id": USER_ID,
            "agent_id": EMAIL,
            "tenant_id": TENANT_ID,
            "role": role,
            "password_hash": "old-password-hash",
            "policy_acceptance_required": policy_required,
            "has_current_policy_acceptance": True,
            "is_active": True,
        }
        self.reset_tokens: dict[str, dict] = {}
        self.password_updates = 0
        self.siblings_revoked = 0
        self.consume_query = ""
        self.raise_on_lookup = False

    async def fetchrow(self, query: str, *args):
        if "FROM users WHERE lower(users.agent_id)" in query:
            if self.raise_on_lookup:
                raise RuntimeError("database unavailable")
            if not self.user["is_active"] or self.user["agent_id"].lower() != args[0].lower():
                return None
            return dict(self.user)

        if "WITH consumed_reset AS" in query:
            self.consume_query = query
            jti_hash, tenant_id, agent_id, password_hash, _policy_version = args
            record = self.reset_tokens.get(jti_hash)
            now = datetime.now(timezone.utc)
            if (
                record is None
                or record["consumed_at"] is not None
                or record["expires_at"] <= now
                or record["tenant_id"] != tenant_id
                or record["user_id"] != self.user["id"]
                or self.user["tenant_id"] != tenant_id
                or self.user["agent_id"].lower() != agent_id.lower()
                or not self.user["is_active"]
            ):
                return None

            record["consumed_at"] = now
            self.user["password_hash"] = password_hash
            self.password_updates += 1
            return dict(self.user)

        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query: str, *args):
        # Using one reset link retires the rest, so an account recovered after
        # two "forgot password" clicks does not leave the earlier link live for
        # the remainder of its 30 minutes.
        if "SET consumed_at = now()" in query and "WHERE user_id" in query:
            (user_id,) = args
            now = datetime.now(timezone.utc)
            for record in self.reset_tokens.values():
                if (
                    record["user_id"] == user_id
                    and record["consumed_at"] is None
                    and record["expires_at"] > now
                ):
                    record["consumed_at"] = now
                    self.siblings_revoked += 1
            return "UPDATE"

        if "INSERT INTO password_reset_tokens" not in query:
            raise AssertionError(f"Unexpected execute query: {query}")

        jti_hash, user_id, tenant_id, expires_at = args
        assert len(jti_hash) == 64
        assert all(character in "0123456789abcdef" for character in jti_hash)
        self.reset_tokens[jti_hash] = {
            "user_id": user_id,
            "tenant_id": str(tenant_id),
            "expires_at": expires_at,
            "consumed_at": None,
        }
        return "INSERT 0 1"


def _fake_tenant_tx(store: _PasswordResetStore):
    @asynccontextmanager
    async def tx(_ctx):
        yield store

    return tx


def _install_store(monkeypatch, store: _PasswordResetStore) -> list[tuple[str, str]]:
    sent_links: list[tuple[str, str]] = []
    monkeypatch.setattr(db.connection, "tenant_tx", _fake_tenant_tx(store))
    monkeypatch.setattr(
        auth,
        "_send_reset_email",
        lambda recipient, link: sent_links.append((recipient, link)),
    )
    monkeypatch.setattr(auth, "_hash_pw", lambda password: f"test-hash:{password}")
    return sent_links


def _request_reset_link(store: _PasswordResetStore, sent_links: list[tuple[str, str]]) -> str:
    result = asyncio.run(auth.forgot_password(auth.ForgotRequest(email=store.user["agent_id"])))
    assert result == auth._FORGOT_RESPONSE
    assert len(sent_links) == 1
    recipient, link = sent_links[0]
    assert recipient == store.user["agent_id"]
    return parse_qs(urlsplit(link).query)["reset"][0]


def _reset(token: str):
    return asyncio.run(
        auth.reset_password(
            auth.ResetRequest(token=token, new_password=NEW_PASSWORD),
            Response(),
        )
    )


def _assert_invalid_reset(token: str) -> None:
    with pytest.raises(HTTPException) as error:
        _reset(token)
    assert error.value.status_code == 400
    assert error.value.detail == INVALID_RESET_DETAIL


def test_password_reset_token_is_hashed_and_consumed_with_password_update(monkeypatch):
    store = _PasswordResetStore()
    sent_links = _install_store(monkeypatch, store)
    reset_token = _request_reset_link(store, sent_links)
    reset_claims = auth.decode_token(reset_token)

    assert reset_claims["role"] == "password_reset"
    assert reset_claims["purpose"] == "pwreset"
    assert reset_claims["jti"] not in store.reset_tokens
    assert reset_token not in store.reset_tokens
    jti_hash = auth._hash_reset_jti(reset_claims["jti"])
    assert jti_hash in store.reset_tokens
    assert store.reset_tokens[jti_hash]["consumed_at"] is None

    result = _reset(reset_token)

    assert store.reset_tokens[jti_hash]["consumed_at"] is not None
    assert store.user["password_hash"] == f"test-hash:{NEW_PASSWORD}"
    assert store.password_updates == 1
    assert "UPDATE password_reset_tokens" in store.consume_query
    assert "UPDATE users AS account" in store.consume_query
    assert result.agent_id == EMAIL


def test_replayed_password_reset_token_is_rejected(monkeypatch):
    store = _PasswordResetStore()
    reset_token = _request_reset_link(store, _install_store(monkeypatch, store))

    _reset(reset_token)
    _assert_invalid_reset(reset_token)

    assert store.password_updates == 1


def test_post_reset_role_and_policy_gate_come_from_current_user_row(monkeypatch):
    store = _PasswordResetStore(role="agent")
    reset_token = _request_reset_link(store, _install_store(monkeypatch, store))

    # The account's authorization changes after issuance. The reset capability
    # remains identity-bound, but it cannot restore or mint the stale role.
    store.user["role"] = "broker_owner"
    store.user["policy_acceptance_required"] = True
    result = _reset(reset_token)
    login_claims = auth.decode_token(result.token)

    assert result.role == "broker_owner"
    assert login_claims["role"] == "broker_owner"
    assert result.policy_acceptance_required is True
    assert login_claims["policy_pending"] is True


def test_expired_mismatched_role_and_legacy_reset_tokens_fail_uniformly(monkeypatch):
    store = _PasswordResetStore()
    reset_token = _request_reset_link(store, _install_store(monkeypatch, store))
    claims = auth.decode_token(reset_token)
    reset_identity = {"purpose": "pwreset", "jti": claims["jti"]}

    expired = auth._issue_jwt(
        EMAIL,
        TENANT_ID,
        "password_reset",
        ttl=-60,
        extra=reset_identity,
    )
    mismatched_role = auth._issue_jwt(
        EMAIL,
        TENANT_ID,
        "platform_admin",
        extra=reset_identity,
    )
    legacy_pre_migration = auth._issue_jwt(
        EMAIL,
        TENANT_ID,
        "agent",
        extra={"purpose": "pwreset"},
    )

    for invalid_token in (expired, mismatched_role, legacy_pre_migration):
        _assert_invalid_reset(invalid_token)

    assert store.password_updates == 0


def test_forgot_is_always_202_without_account_enumeration(monkeypatch):
    store = _PasswordResetStore()
    _install_store(monkeypatch, store)
    known = asyncio.run(auth.forgot_password(auth.ForgotRequest(email=EMAIL)))
    unknown = asyncio.run(
        auth.forgot_password(auth.ForgotRequest(email="missing@example.test"))
    )
    store.raise_on_lookup = True
    unavailable = asyncio.run(auth.forgot_password(auth.ForgotRequest(email=EMAIL)))

    route = next(route for route in auth.router.routes if route.path == "/auth/forgot")
    assert route.status_code == 202
    assert known == unknown == unavailable == auth._FORGOT_RESPONSE


_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _import_auth_in_subprocess(**settings: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in (
        "ORACLE_ENV",
        "ORACLE_SECRET_KEY",
        "ORACLE_JWT_ISSUER",
        "ORACLE_JWT_AUDIENCE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONPATH": str(_BACKEND_DIR),
            "ORACLE_SECRET_KEY": "test-only-secret-key-with-at-least-32-bytes",
            **settings,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", "import auth"],
        cwd=_BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"ORACLE_ENV": "dev", "ORACLE_JWT_ISSUER": "https://neohrs.com"},
        {"ORACLE_ENV": "dev", "ORACLE_JWT_AUDIENCE": "neoh-web"},
        {"ORACLE_ENV": "prod", "ORACLE_JWT_ISSUER": "https://neohrs.com"},
        {"ORACLE_ENV": "prod", "ORACLE_JWT_AUDIENCE": "neoh-web"},
    ],
)
def test_partial_jwt_issuer_audience_configuration_fails_everywhere(settings):
    result = _import_auth_in_subprocess(**settings)

    assert result.returncode != 0
    assert "must be configured together" in result.stderr


def test_missing_jwt_issuer_audience_configuration_fails_in_production():
    result = _import_auth_in_subprocess(ORACLE_ENV="prod")

    assert result.returncode != 0
    assert "are required outside development" in result.stderr


def test_complete_jwt_issuer_audience_configuration_starts_in_production():
    result = _import_auth_in_subprocess(
        ORACLE_ENV="prod",
        ORACLE_JWT_ISSUER="https://neohrs.com",
        ORACLE_JWT_AUDIENCE="neoh-web",
    )

    assert result.returncode == 0, result.stderr


def test_configured_jwt_issuer_and_audience_are_minted_and_enforced(monkeypatch):
    monkeypatch.setattr(auth, "_JWT_ISSUER", "https://neohrs.com")
    monkeypatch.setattr(auth, "_JWT_AUDIENCE", "neoh-web")
    token = auth._issue_jwt(EMAIL, TENANT_ID, "agent")

    claims = auth.decode_token(token)
    assert claims["iss"] == "https://neohrs.com"
    assert claims["aud"] == "neoh-web"

    for claim, wrong_value in (
        ("iss", "https://attacker.invalid"),
        ("aud", "other-service"),
    ):
        mismatched_claims = dict(claims)
        mismatched_claims[claim] = wrong_value
        mismatched_token = jwt.encode(
            mismatched_claims,
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        with pytest.raises(HTTPException) as error:
            auth.decode_token(mismatched_token)
        assert error.value.status_code == 401


def test_development_may_omit_jwt_issuer_and_audience():
    result = _import_auth_in_subprocess(ORACLE_ENV="dev")

    assert result.returncode == 0, result.stderr


def test_password_reset_migration_enforces_rls_and_least_privilege():
    migration = (
        _BACKEND_DIR / "db" / "migrations" / "0041_password_reset_tokens.sql"
    ).read_text(encoding="utf-8")

    assert "jti_hash    char(64)" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "GRANT UPDATE (consumed_at)" in migration
    assert "REVOKE ALL ON password_reset_tokens FROM oracle_app" in migration


def test_using_one_reset_link_retires_the_others(monkeypatch):
    """People click "forgot password" twice when the first mail is slow.

    Only the token presented was consumed, so every earlier link stayed usable
    for the rest of its 30-minute life after the account had already been
    recovered — an intercepted copy of any of them still worked.
    """
    store = _PasswordResetStore()
    sent_links = _install_store(monkeypatch, store)

    superseded = _request_reset_link(store, sent_links)
    # _request_reset_link asserts exactly one mail, so clear before the second.
    sent_links.clear()
    current = _request_reset_link(store, sent_links)
    assert superseded != current

    _reset(current)

    assert store.siblings_revoked >= 1
    live = [r for r in store.reset_tokens.values() if r["consumed_at"] is None]
    assert live == [], "an earlier reset link survived the reset that superseded it"
    # And it is genuinely dead, not merely marked.
    _assert_invalid_reset(superseded)
