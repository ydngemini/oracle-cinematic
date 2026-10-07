"""run_migrations.py --precheck-then-migrate — the PRE_DEPLOY job's guard.

Migrations moved from a CI runner into an App Platform PRE_DEPLOY job, because
the databases now accept connections from the app only (trusted sources). The
runner's bash precheck (scripts/migration-precheck.sh) cannot run in the
release image (no psql), so its checks live here, in Python, and run before
anything is touched.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_migrations as rm  # noqa: E402

RELEASE = {"0001_a.sql": "a" * 64, "0002_b.sql": "b" * 64, "0003_c.sql": "c" * 64}


def _check(stamp="neoh-environment=staging", expected="staging", ledger=None, release=None):
    if ledger is None:
        ledger = [("0001_a.sql", "a" * 64), ("0002_b.sql", "b" * 64)]
    return rm.evaluate_precheck(stamp=stamp, expected_env=expected, ledger=ledger,
                                release=release or RELEASE)


def test_a_healthy_database_passes_with_its_pending_count():
    failures, notes = _check()
    assert failures == []
    assert "1 pending" in notes[0] and "0003_c.sql" in notes[0]


def test_an_unstamped_or_other_environments_database_is_refused():
    assert "no environment stamp" in _check(stamp="")[0][0]
    assert "'production' database" in _check(stamp="neoh-environment=production")[0][0]


def test_no_ledger_is_refused():
    failures, _ = rm.evaluate_precheck(stamp="neoh-environment=staging", expected_env="staging",
                                       ledger=None, release=RELEASE)
    assert "no readable schema_migrations ledger" in failures[0]


def test_a_migration_this_release_lacks_is_refused():
    failures, _ = _check(ledger=[("0001_a.sql", "a" * 64), ("0099_ghost.sql", "f" * 64)])
    assert any("does not contain: 0099_ghost.sql" in f for f in failures)


def test_a_release_behind_the_database_is_refused():
    failures, _ = _check(release={"0001_a.sql": "a" * 64},
                         ledger=[("0001_a.sql", "a" * 64), ("0002_b.sql", "b" * 64)])
    assert any("BEHIND" in f for f in failures)


def test_an_edited_applied_migration_is_refused():
    failures, _ = _check(ledger=[("0001_a.sql", "0" * 64), ("0002_b.sql", "b" * 64)])
    assert any("EDITED" in f and "0001_a.sql" in f for f in failures)


def test_an_unchecksummed_legacy_row_is_not_called_edited():
    failures, _ = _check(ledger=[("0001_a.sql", None), ("0002_b.sql", "b" * 64)])
    assert failures == []


DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")


@pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
def test_precheck_against_a_real_migrated_database(monkeypatch):
    """Run in CI's real-Postgres job, on a database built from every migration."""
    import glob

    import asyncpg

    async def scenario():
        conn = await asyncpg.connect(DSN)
        try:
            files = sorted(glob.glob(os.path.join(rm.MIGRATIONS_DIR, "*.sql")))
            db = await conn.fetchval("SELECT current_database()")
            before = await conn.fetchval(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = $1", db)
            monkeypatch.setenv("ORACLE_EXPECTED_ENVIRONMENT", "staging")
            await conn.execute(f'COMMENT ON DATABASE "{db}" IS NULL')
            refused = await rm._precheck(conn, files)
            await conn.execute(f"""COMMENT ON DATABASE "{db}" IS 'neoh-environment=staging'""")
            accepted = await rm._precheck(conn, files)
            await conn.execute(f"""COMMENT ON DATABASE "{db}" IS 'neoh-environment=production'""")
            wrong = await rm._precheck(conn, files)
            restore = "NULL" if before is None else "'" + before.replace("'", "''") + "'"
            await conn.execute(f'COMMENT ON DATABASE "{db}" IS {restore}')
            return refused, accepted, wrong
        finally:
            await conn.close()

    refused, accepted, wrong = asyncio.run(scenario())
    assert (refused, accepted, wrong) == (3, 0, 3)
