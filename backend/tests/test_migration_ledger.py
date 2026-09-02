"""The migration ledger, and the two ways it can lie.

`run_migrations.py` has always kept a `schema_migrations` ledger, but dev never
used it: `dev-start.sh` piped every .sql through psql with `> /dev/null 2>&1`,
so a migration that genuinely FAILED printed "(already applied / no-op)" and no
ledger row was ever written. Pointing dev at the real runner needs a reconcile —
a database with the schema but no ledger would otherwise re-apply all 76 files.

Reconcile decides by probing for the objects a migration declares, which makes
the parser load-bearing. Both bugs below were real, both were caught by the
"refuse on partial" rule rather than by producing a wrong verdict, and both are
pinned here.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("run_migrations", BACKEND / "run_migrations.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_a_column_is_attributed_to_its_own_alter_table():
    """A sliding window over the file paired ADD COLUMN with whichever
    ALTER TABLE was within N characters. On the real migrations that put
    leads.seller_client_id under `clients` and inbound_voice_calls.forwarded_at
    under `telephony_routes` — six false "partial" verdicts in one run."""
    sql = """
    ALTER TABLE clients ADD COLUMN IF NOT EXISTS company text;
    ALTER TABLE leads ADD COLUMN IF NOT EXISTS seller_client_id uuid;
    """
    columns = dict.fromkeys(runner._declared_objects(sql)["columns"])
    assert ("leads", "seller_client_id") in columns
    assert ("clients", "seller_client_id") not in columns
    assert ("clients", "company") in columns


def test_environment_conditional_ddl_is_not_probed():
    """0013 adds fema_flood_zones.geom and its GiST index only when PostGIS is
    installed. On a database without it, absence is exactly what the migration
    intended — probing would report drift."""
    sql = """
    CREATE TABLE IF NOT EXISTS fema_flood_zones (id uuid PRIMARY KEY);
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
            ALTER TABLE fema_flood_zones ADD COLUMN geom geometry;
            CREATE INDEX idx_ffz_geom ON fema_flood_zones USING GIST (geom);
        END IF;
    END $$;
    """
    declared = runner._declared_objects(sql)
    assert "fema_flood_zones" in declared["tables"]
    assert "idx_ffz_geom" not in declared["indexes"]
    assert ("fema_flood_zones", "geom") not in declared["columns"]


def test_an_idempotency_guarded_block_is_still_probed():
    """0018 wraps ADD CONSTRAINT leads_tenant_parcel_key in BEGIN/EXCEPTION —
    the object is meant to exist unconditionally, so it is probeable.

    It has to be. That guard catches `duplicate_object` while Postgres raises
    `duplicate_table` for a constraint index-name clash, so 0018 is not actually
    re-runnable; treating it as unverifiable made the runner try to apply an
    already-applied migration and fail with "relation already exists".
    """
    sql = """
    DO $$ BEGIN
        BEGIN
            ALTER TABLE leads ADD CONSTRAINT leads_tenant_parcel_key UNIQUE (tenant_id, parcel_id);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
    END $$;
    """
    assert "leads_tenant_parcel_key" in runner._declared_objects(sql)["constraints"]


def test_a_constraint_backed_index_is_discoverable():
    """A UNIQUE constraint creates an index, but under ADD CONSTRAINT rather
    than CREATE INDEX — invisible to an index-only probe."""
    declared = runner._declared_objects(
        "ALTER TABLE t ADD CONSTRAINT t_key UNIQUE (a, b);"
    )
    assert declared["constraints"] == ["t_key"]


def test_a_seed_only_migration_declares_nothing_and_is_not_guessed():
    """0025 seeds 51 states and creates no object. Reconcile must leave it
    unrecorded for the normal apply, not assume either way."""
    declared = runner._declared_objects(
        "INSERT INTO state_market_stats (state_code) VALUES ('DE') "
        "ON CONFLICT DO NOTHING;"
    )
    assert not any(declared.values())


def test_plaintext_is_refused_for_anything_but_a_local_host():
    """Migrating a managed instance over plaintext would put the master
    credential on the wire."""
    import os

    os.environ["ORACLE_DB_SSL"] = "disable"
    try:
        assert runner._ssl_context("db") is False
        with pytest.raises(RuntimeError, match="only honoured for a local database"):
            runner._ssl_context("neoh-prod.postgres.database.azure.com")
    finally:
        del os.environ["ORACLE_DB_SSL"]


def test_every_migration_on_disk_parses_without_error():
    """The probe runs against real files; a regex that throws on one of them
    would take the whole reconcile down."""
    # Named rather than counted. The allowance used to be "at most 8 files
    # declare nothing", which is a number that gets bumped whenever it fails —
    # including by a migration that should have declared something and forgot.
    # Listing them makes each exemption a decision with a reason attached, and
    # makes an unexpected one fail loudly instead of eating the slack.
    unprobeable_by_design = {
        "0003_hardening.sql": "GRANT/REVOKE only — privileges are not objects",
        "0014_audit_attribution.sql": "backfills attribution on existing rows",
        "0025_state_reference_seed.sql": "INSERTs reference data",
        "0026_seed_platform_tenant.sql": "INSERTs the platform tenant",
        "0037_application_sequence_privileges.sql": "GRANTs on sequences",
        "0042_seed_platform_operator_profile.sql": "INSERTs the operator profile",
        "0069_agency_law_unresearched.sql": "NULLs columns that were asserted without research",
        "0078_zip_state_conflicts.sql": "INSERTs conflict reference data",
        "0083_force_rls_on_subscriptions.sql": "ALTERs a table flag; FORCE creates no object",
        "0085_grant_execute_public_record_date_or_null.sql":
            "GRANT only — same shape as 0003; a privilege is not an object",
        "0088_grant_execute_app_current_agent.sql":
            "GRANT only — same shape as 0003; a privilege is not an object",
    }

    files = sorted((BACKEND / "db" / "migrations").glob("*.sql"))
    assert len(files) > 70

    silent = set()
    for path in files:
        declared = runner._declared_objects(path.read_text())
        assert isinstance(declared["columns"], list)
        if not any(declared.values()):
            silent.add(path.name)

    unexpected = silent - set(unprobeable_by_design)
    assert unexpected == set(), (
        "these migrations declare no probeable object, so --reconcile cannot "
        "tell whether they applied. Add DDL the probe can see, or list them "
        f"above with a reason: {sorted(unexpected)}"
    )

    # And the reverse: an entry that starts declaring something should lose its
    # exemption rather than sit there granting slack forever.
    stale = set(unprobeable_by_design) - silent
    assert stale == set(), f"no longer unprobeable, drop from the list: {sorted(stale)}"


def test_no_partial_index_predicates_the_planner_cannot_match():
    """A partial index is usable only if the query IMPLIES its predicate.

    0089 narrowed the owner-name index with
        WHERE length(regexp_replace(lower(COALESCE(owner_name,'')),...)) >= 5
    on the sound observation that _property_candidates never queries shorter
    names. But the query only says `regexp_replace(...) = $1`, and proving that
    implies `length(<same expression>) >= 5` means folding the bound constant
    through length() — which predicate_implied_by does not do. The planner
    silently skipped the index and fell back to a per-row regexp over 8.6M rows:
    EXPLAIN ANALYZE did not return in 100s, against 0.93ms with the full index.
    0090 reverted it.

    The distinction that matters: a predicate over a COLUMN (`address IS NOT
    NULL`, as idx_public_property_address uses) is provable from a strict
    operator on that column. A predicate over a DERIVED EXPRESSION proves
    nothing about that expression's value, so it only ever costs you the index.

    This asserts the narrow, checkable form of that rule: no partial index may
    predicate on length() of an expression.
    """
    files = sorted((BACKEND / "db" / "migrations").glob("*.sql"))
    corpus = "\n".join(f.read_text() for f in files)

    offenders = []
    for path in files:
        for stmt in path.read_text().split(";"):
            if "CREATE INDEX" not in stmt or " WHERE " not in stmt:
                continue
            if "length(" not in stmt.split(" WHERE ", 1)[1].lower():
                continue
            name = re.search(r"CREATE INDEX(?:\s+IF NOT EXISTS)?\s+(\w+)", stmt)
            # A later migration dropping it means the mistake was already
            # corrected; 0089 is left on disk because it is in the ledger.
            if name and f"DROP INDEX IF EXISTS {name.group(1)}" in corpus:
                continue
            offenders.append(path.name)
    assert offenders == [], (
        "these migrations create a partial index whose predicate the planner "
        f"cannot prove from an equality on the same expression: {sorted(set(offenders))}. "
        "See 0090 for why that silently disables the index."
    )


# ── Concurrent index pre-build (--prebuild-indexes) ─────────────────────────
#
# CREATE INDEX holds a ShareLock for the whole build. On an empty database that
# is instant; applied to a populated production table it is minutes, during
# which every write dies on the pool's 30s command_timeout. CREATE INDEX
# CONCURRENTLY avoids the lock but cannot run in a transaction, and the runner
# wraps each migration in one so the migration and its ledger row commit
# together.
#
# The pre-pass resolves that without weakening either: it builds the indexes
# concurrently BEFORE the transactional apply reaches them, and because every
# CREATE INDEX in this repo is written IF NOT EXISTS, the migration's own
# statement then does no work. No migration file is edited, so no checksum moves.
#
# Measured on the dev database with a live writer holding `leads`:
#   pre-build ran 112s concurrently and never blocked the writer
#   the apply that followed took 505ms instead of a multi-minute held lock

def test_prebuild_rewrites_create_index_to_concurrent_and_idempotent():
    stmts = runner._concurrent_index_statements(
        "CREATE INDEX idx_a ON t (col);"
    )
    assert len(stmts) == 1
    name, sql = stmts[0]
    assert name == "idx_a"
    assert "CONCURRENTLY" in sql
    # Without IF NOT EXISTS the pre-pass would not be re-runnable: a second run
    # would fail on the index the first one built.
    assert "IF NOT EXISTS" in sql


def test_prebuild_strips_comments_before_splitting_statements():
    """Migration comments are prose, and prose contains semicolons.

    0086's own header has one. Splitting on ';' first cut the file mid-sentence
    and hid the CREATE INDEX behind a fragment that no longer started with
    CREATE, so one of its two indexes was silently never pre-built.
    """
    sql = (
        "-- There is no work left; the job burns I/O to rediscover that.\n"
        "CREATE INDEX idx_after_a_semicolon_in_a_comment ON t (col);\n"
    )
    names = [n for n, _ in runner._concurrent_index_statements(sql)]
    assert names == ["idx_after_a_semicolon_in_a_comment"]


def test_prebuild_ignores_everything_that_is_not_a_create_index():
    """Tables, grants and policies stay with the transactional apply."""
    sql = """
    CREATE TABLE t (id uuid PRIMARY KEY);
    ALTER TABLE t ENABLE ROW LEVEL SECURITY;
    GRANT SELECT ON t TO oracle_app;
    DROP INDEX IF EXISTS idx_old;
    INSERT INTO t VALUES (gen_random_uuid());
    """
    assert runner._concurrent_index_statements(sql) == []


def test_prebuild_handles_the_real_index_migrations():
    """Against the files themselves, not a synthetic sample."""
    migrations = BACKEND / "db" / "migrations"

    found = runner._concurrent_index_statements(
        (migrations / "0086_normalization_and_owner_lookup_indexes.sql").read_text()
    )
    assert sorted(n for n, _ in found) == [
        "idx_leads_pending_payload_normalization",
        "idx_public_property_owner_normalized",
    ], "both of 0086's indexes must be pre-buildable"
    for _, sql in found:
        assert "CONCURRENTLY" in sql and "IF NOT EXISTS" in sql

    # A GRANT-only migration offers nothing to pre-build and must not confuse it.
    assert runner._concurrent_index_statements(
        (migrations / "0088_grant_execute_app_current_agent.sql").read_text()
    ) == []


def test_prebuild_preserves_a_partial_index_predicate():
    """Dropping the WHERE clause would build a different, much larger index."""
    sql = (
        "CREATE INDEX IF NOT EXISTS idx_p ON leads (updated_at, id)\n"
        " WHERE underwriting->>'source' LIKE 'firehose:%';"
    )
    _, rewritten = runner._concurrent_index_statements(sql)[0]
    assert "WHERE" in rewritten and "firehose:%" in rewritten
    assert rewritten.upper().count("CONCURRENTLY") == 1
