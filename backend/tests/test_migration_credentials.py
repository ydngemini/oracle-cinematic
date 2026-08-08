"""Where run_migrations.py gets its administrator credential and CA bundle.

Migrations run with the one credential that can rewrite the schema, so the
resolution order is security-relevant: an explicit local password, then Key
Vault, then the legacy AWS secret — never a silent fallback to a cloud the
deployment isn't on.
"""

from __future__ import annotations

import json
import ssl
import sys
import types

import pytest

import run_migrations

_ENV = (
    "ORACLE_DB_ADMIN_PASSWORD",
    "ORACLE_DB_ADMIN_USER",
    "ORACLE_DB_ADMIN_SECRET",
    "ORACLE_KEY_VAULT_URI",
    "DB_MASTER_SECRET_ARN",
    "ORACLE_DB_CA_BUNDLE",
    "ORACLE_RDS_CA_BUNDLE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)


def _fake_keyvault(monkeypatch, secret_value):
    """Install fake azure.identity / azure.keyvault.secrets for the lazy import."""
    seen = {}

    class _SecretClient:
        def __init__(self, vault_url, credential):
            seen["vault_url"] = vault_url

        def get_secret(self, name):
            seen["secret_name"] = name
            return types.SimpleNamespace(value=secret_value)

    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda *a, **k: object()
    keyvault = types.ModuleType("azure.keyvault.secrets")
    keyvault.SecretClient = _SecretClient
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.keyvault.secrets", keyvault)
    return seen


# --- resolution order ---------------------------------------------------------

def test_explicit_password_wins(monkeypatch):
    monkeypatch.setenv("ORACLE_DB_ADMIN_PASSWORD", "local-dev")
    monkeypatch.setenv("ORACLE_KEY_VAULT_URI", "https://vault.example/")

    assert run_migrations._admin_credentials() == ("postgres", "local-dev")


def test_key_vault_is_used_before_the_aws_secret(monkeypatch):
    seen = _fake_keyvault(monkeypatch, "kv-password")
    monkeypatch.setenv("ORACLE_KEY_VAULT_URI", "https://neoh-kv.vault.azure.net/")
    monkeypatch.setenv("DB_MASTER_SECRET_ARN", "arn:aws:secretsmanager:...")

    assert run_migrations._admin_credentials() == ("postgres", "kv-password")
    assert seen["vault_url"] == "https://neoh-kv.vault.azure.net/"
    assert seen["secret_name"] == "oracle-db-admin-password"


def test_key_vault_secret_name_is_configurable(monkeypatch):
    seen = _fake_keyvault(monkeypatch, "kv-password")
    monkeypatch.setenv("ORACLE_KEY_VAULT_URI", "https://neoh-kv.vault.azure.net/")
    monkeypatch.setenv("ORACLE_DB_ADMIN_SECRET", "pg-admin")

    run_migrations._admin_credentials()
    assert seen["secret_name"] == "pg-admin"


def test_no_source_configured_fails_loudly(monkeypatch):
    with pytest.raises(RuntimeError, match="ORACLE_KEY_VAULT_URI"):
        run_migrations._admin_credentials()


# --- secret shapes ------------------------------------------------------------

def test_bare_password_secret_uses_the_configured_admin_user(monkeypatch):
    """Azure stores just the password; the login lives in its own variable."""
    _fake_keyvault(monkeypatch, "  just-a-password  ")
    monkeypatch.setenv("ORACLE_KEY_VAULT_URI", "https://neoh-kv.vault.azure.net/")
    monkeypatch.setenv("ORACLE_DB_ADMIN_USER", "neohadmin")

    assert run_migrations._admin_credentials() == ("neohadmin", "just-a-password")


def test_json_secret_is_still_understood(monkeypatch):
    """RDS-managed secrets are always a JSON document."""
    _fake_keyvault(
        monkeypatch, json.dumps({"username": "master", "password": "json-password"})
    )
    monkeypatch.setenv("ORACLE_KEY_VAULT_URI", "https://neoh-kv.vault.azure.net/")

    assert run_migrations._admin_credentials() == ("master", "json-password")


# --- transport ----------------------------------------------------------------

def test_ssl_context_defaults_to_the_system_trust_store():
    """Regression: this defaulted to the RDS .pem, so it raised FileNotFoundError
    on any host that doesn't ship that bundle — i.e. every Azure host."""
    ctx = run_migrations._ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_explicit_ca_bundle_is_honoured(monkeypatch):
    monkeypatch.setenv("ORACLE_DB_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")

    assert run_migrations._ssl_context().verify_mode == ssl.CERT_REQUIRED


def test_missing_pinned_bundle_still_raises(monkeypatch):
    """A CA bundle that was asked for but isn't there must not be ignored."""
    monkeypatch.setenv("ORACLE_DB_CA_BUNDLE", "/nonexistent/bundle.pem")

    with pytest.raises(FileNotFoundError):
        run_migrations._ssl_context()
