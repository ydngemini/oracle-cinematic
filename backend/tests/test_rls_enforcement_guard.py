"""The app must not run as a role row-level security does not apply to.

Tenant isolation on most read paths is RLS and nothing else — the queries carry
no tenant predicate and rely on the policy. PostgreSQL exempts superusers and
BYPASSRLS roles from every policy, so connecting as one does not weaken
isolation, it removes it: silently, with no error and no log line.

Local dev connected as `postgres` until 2026-08-28. A freshly registered broker
could list another tenant's clients, and the entire test suite stayed green,
because production connects as `oracle_app_login` and behaves correctly. The one
environment where it was broken was the one nobody could observe it in.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from db import connection


class _Pool:
    def __init__(self, role, exempt):
        self._row = {"role": role, "exempt": exempt}

    def acquire(self):
        row = self._row

        @asynccontextmanager
        async def _ctx():
            class _Conn:
                async def fetchrow(self, _query):
                    return row

            yield _Conn()

        return _ctx()


def test_a_superuser_refuses_to_boot_in_production(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "prod")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(connection._assert_rls_is_enforced(_Pool("postgres", True)))

    message = str(excinfo.value)
    assert "Refusing to start" in message
    assert "postgres" in message
    # Name the fix, not just the fault.
    assert "oracle_app_login" in message


def test_dev_is_loud_rather_than_fatal(monkeypatch, caplog):
    """A local box must still start — but nobody should miss why it is wrong."""
    monkeypatch.setenv("ORACLE_ENV", "dev")

    with caplog.at_level("ERROR"):
        asyncio.run(connection._assert_rls_is_enforced(_Pool("postgres", True)))

    assert "TENANT ISOLATION IS OFF" in caplog.text


def test_a_normal_role_passes_silently(monkeypatch, caplog):
    monkeypatch.setenv("ORACLE_ENV", "prod")

    with caplog.at_level("ERROR"):
        asyncio.run(connection._assert_rls_is_enforced(_Pool("oracle_app_login", False)))

    assert caplog.text == ""


def test_the_local_default_is_not_a_superuser():
    """The password-auth path used to fall back to `postgres`."""
    import inspect

    source = inspect.getsource(connection.init_pool)
    assert 'os.getenv("ORACLE_DB_USER") or "oracle_app_login"' in source
    assert 'or "postgres"' not in source
