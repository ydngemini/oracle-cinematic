import asyncio

import pytest

import run_migrations


class _LedgerInsertError(RuntimeError):
    pass


class _DuplicateObjectError(RuntimeError):
    sqlstate = "42P07"


class _FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        assert self.conn.lock_held
        assert not self.conn.in_transaction
        self.conn.in_transaction = True
        self.conn.pending_sql = []
        self.conn.pending_ledger = []
        self.conn.events.append(("begin",))
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        assert self.conn.lock_held
        if exc_type is None:
            self.conn.committed_sql.extend(self.conn.pending_sql)
            self.conn.applied.update(self.conn.pending_ledger)
            self.conn.events.append(("commit",))
        else:
            self.conn.events.append(("rollback",))
        self.conn.pending_sql = []
        self.conn.pending_ledger = []
        self.conn.in_transaction = False
        return False


class _FakeConnection:
    def __init__(self, *, migration_error=None, fail_ledger=False):
        self.migration_error = migration_error
        self.fail_ledger = fail_ledger
        self.events = []
        self.applied = set()
        self.committed_sql = []
        self.pending_sql = []
        self.pending_ledger = []
        self.in_transaction = False
        self.lock_held = False

    def transaction(self):
        return _FakeTransaction(self)

    async def execute(self, sql, *args):
        if sql == run_migrations._LOCK_SQL:
            assert not self.lock_held
            self.lock_held = True
            self.events.append(("lock", args[0]))
            return "SELECT 1"

        assert self.lock_held
        if sql == run_migrations._CREATE_LEDGER_SQL:
            assert not self.in_transaction
            self.events.append(("create_ledger",))
        elif sql == run_migrations._RECORD_MIGRATION_SQL:
            assert self.in_transaction
            self.events.append(("ledger", args[0]))
            if self.fail_ledger:
                raise _LedgerInsertError("ledger insert failed")
            self.pending_ledger.append(args[0])
        elif sql.startswith("ALTER ROLE oracle_app_login PASSWORD"):
            assert not self.in_transaction
            self.events.append(("configure_role",))
        else:
            assert self.in_transaction
            self.events.append(("migration", sql))
            if self.migration_error is not None:
                raise self.migration_error
            self.pending_sql.append(sql)
        return "OK"

    async def fetch(self, sql, *args):
        assert self.lock_held
        assert not self.in_transaction
        assert sql == "SELECT filename FROM schema_migrations"
        self.events.append(("fetch_applied",))
        return [{"filename": filename} for filename in sorted(self.applied)]

    async def fetchval(self, sql, *args):
        assert sql == run_migrations._UNLOCK_SQL
        assert args == (run_migrations._MIGRATION_LOCK_KEY,)
        assert self.lock_held
        assert not self.in_transaction
        self.events.append(("unlock", args[0]))
        self.lock_held = False
        return True


def _write_migration(tmp_path, sql):
    migration = tmp_path / "0001_example.sql"
    migration.write_text(sql, encoding="utf-8")
    return str(migration)


def _event_names(conn):
    return [event[0] for event in conn.events]


def test_advisory_lock_is_held_until_migration_commit(tmp_path, monkeypatch):
    monkeypatch.delenv("ORACLE_DB_APP_PASSWORD", raising=False)
    migration = _write_migration(
        tmp_path,
        "-- psql-compatible migration\nBEGIN;\nCREATE TABLE example (id int);\nCOMMIT;\n",
    )
    conn = _FakeConnection()

    result = asyncio.run(run_migrations._run_migrations(conn, [migration]))

    assert result == 0
    assert _event_names(conn) == [
        "lock",
        "create_ledger",
        "fetch_applied",
        "begin",
        "migration",
        "ledger",
        "commit",
        "unlock",
    ]
    assert conn.events[0] == ("lock", run_migrations._MIGRATION_LOCK_KEY)
    assert conn.events[-1] == ("unlock", run_migrations._MIGRATION_LOCK_KEY)
    assert conn.applied == {"0001_example.sql"}
    assert conn.committed_sql == [
        "-- psql-compatible migration\nCREATE TABLE example (id int);\n"
    ]


def test_ledger_failure_rolls_back_migration_sql(tmp_path, monkeypatch):
    monkeypatch.delenv("ORACLE_DB_APP_PASSWORD", raising=False)
    migration = _write_migration(tmp_path, "CREATE TABLE example (id int);\n")
    conn = _FakeConnection(fail_ledger=True)

    with pytest.raises(_LedgerInsertError, match="ledger insert failed"):
        asyncio.run(run_migrations._run_migrations(conn, [migration]))

    assert _event_names(conn) == [
        "lock",
        "create_ledger",
        "fetch_applied",
        "begin",
        "migration",
        "ledger",
        "rollback",
        "unlock",
    ]
    assert conn.committed_sql == []
    assert conn.applied == set()


def test_duplicate_in_legacy_file_is_not_backfilled_into_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ORACLE_DB_APP_PASSWORD", raising=False)
    migration = _write_migration(
        tmp_path,
        "CREATE TABLE tenants (id uuid);\nCREATE TABLE later_object (id uuid);\n",
    )
    conn = _FakeConnection(
        migration_error=_DuplicateObjectError('relation "tenants" already exists')
    )

    with pytest.raises(_DuplicateObjectError, match="already exists"):
        asyncio.run(run_migrations._run_migrations(conn, [migration]))

    assert _event_names(conn) == [
        "lock",
        "create_ledger",
        "fetch_applied",
        "begin",
        "migration",
        "rollback",
        "unlock",
    ]
    assert "ledger" not in _event_names(conn)
    assert conn.applied == set()


def test_direct_admin_credentials_remain_provider_neutral(monkeypatch):
    monkeypatch.setenv("ORACLE_DB_ADMIN_USER", "azure_admin")
    monkeypatch.setenv("ORACLE_DB_ADMIN_PASSWORD", "key-vault-password")
    monkeypatch.delenv("DB_MASTER_SECRET_ARN", raising=False)

    assert run_migrations._admin_credentials() == (
        "azure_admin",
        "key-vault-password",
    )
