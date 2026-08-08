"""Provider selection for the DB connection layer.

The pool itself needs a live server, but the parts that decide *how* we
authenticate and *what* we trust are pure configuration — and they are exactly
the parts that silently pointed at AWS after the move to Azure. These pin the
Azure defaults so a stray env change can't put prod back on the RDS path (or on
the local password escape hatch) without a test failing.
"""

from __future__ import annotations

import asyncio
import importlib
import ssl

import pytest

import db.connection as connection

_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def _reload(monkeypatch, **env):
    """Re-import the module with a patched environment.

    Auth provider, CA bundle and TLS floor are all resolved at import time, so
    monkeypatching os.environ alone would not move them.
    """
    for key in (
        "ORACLE_DB_AUTH",
        "ORACLE_DB_CA_BUNDLE",
        "ORACLE_DB_TLS_MIN",
        "ORACLE_RDS_CA_BUNDLE",
        "ORACLE_DB_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(connection)


@pytest.fixture(autouse=True)
def _restore_module():
    """Leave the module exactly as the rest of the suite expects to find it."""
    yield
    importlib.reload(connection)


def test_defaults_to_azure_entra(monkeypatch):
    mod = _reload(monkeypatch)

    assert mod.DB_AUTH == "azure-entra"
    assert mod._passwordless_credential() is mod._entra_auth_token


def test_azure_uses_system_trust_store_not_the_rds_bundle(monkeypatch):
    """Regression: DB_CA_BUNDLE used to default to the RDS .pem unconditionally,
    so _build_ssl_context() raised FileNotFoundError on any host without it."""
    mod = _reload(monkeypatch)

    assert mod.DB_CA_BUNDLE is None
    ctx = mod._build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_azure_tls_floor_is_1_2_and_aws_keeps_1_3(monkeypatch):
    """Flexible Server may negotiate 1.2; RDS stays on the stricter floor."""
    azure = _reload(monkeypatch)
    assert azure._build_ssl_context().minimum_version == ssl.TLSVersion.TLSv1_2

    # The real RDS bundle isn't on a dev box; any valid PEM exercises the floor.
    aws = _reload(
        monkeypatch, ORACLE_DB_AUTH="aws-iam", ORACLE_DB_CA_BUNDLE=_SYSTEM_CA_BUNDLE
    )
    assert aws._build_ssl_context().minimum_version == ssl.TLSVersion.TLSv1_3


def test_aws_iam_remains_available_opt_in(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_DB_AUTH="aws-iam", ORACLE_RDS_CA_BUNDLE=__file__)

    assert mod._passwordless_credential() is mod._iam_auth_token
    assert mod.DB_CA_BUNDLE == __file__


def test_explicit_ca_bundle_wins_for_either_provider(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_DB_CA_BUNDLE=__file__)
    assert mod.DB_CA_BUNDLE == __file__


def test_unknown_auth_provider_fails_loudly(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_DB_AUTH="gcp-iam")

    with pytest.raises(RuntimeError, match="ORACLE_DB_AUTH"):
        mod._passwordless_credential()


def test_unknown_tls_floor_fails_loudly(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_DB_TLS_MIN="1.1")

    with pytest.raises(RuntimeError, match="ORACLE_DB_TLS_MIN"):
        mod._build_ssl_context()


def test_entra_token_is_minted_per_call_and_off_the_event_loop(monkeypatch):
    """asyncpg re-invokes the callable per connection, so each call must return a
    current token rather than one captured at startup."""
    mod = _reload(monkeypatch)
    issued = []

    class _Token:
        def __init__(self, value):
            self.token = value

    class _Cred:
        def get_token(self, scope):
            issued.append(scope)
            return _Token(f"token-{len(issued)}")

    monkeypatch.setattr(mod, "_azure_cred", _Cred())

    async def _drive():
        return [await mod._entra_auth_token(), await mod._entra_auth_token()]

    assert asyncio.run(_drive()) == ["token-1", "token-2"]
    assert issued == [mod.AZURE_PG_SCOPE] * 2


def test_credential_is_reused_across_connections(monkeypatch):
    """A fresh DefaultAzureCredential per connection would redo IMDS discovery
    and throw away the token cache."""
    mod = _reload(monkeypatch)
    created = []

    class _Cred:
        def __init__(self):
            created.append(1)

    monkeypatch.setattr(mod, "_azure_cred", None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "azure.identity",
        type("m", (), {"DefaultAzureCredential": _Cred}),
    )

    assert mod._azure_credential() is mod._azure_credential()
    assert len(created) == 1
