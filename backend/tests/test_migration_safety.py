"""Whether the PREVIOUS release can run against the schema a release leaves.

That is the question that decides whether an application-only rollback is safe,
and it is not "did the migration work". A migration can apply perfectly and
still make rollback impossible.

The classifier is deliberately conservative — UNKNOWN is treated as unsafe
everywhere — but conservative is not the same as useless. Its first version
flagged 52 of this repository's 110 migrations as destructive, which is the
failure mode that matters most here: a warning that fires half the time gets
overridden by habit, and then it is not a warning at all. Three exemptions,
each verified against a real migration in this repo, brought that to 22.
"""

from __future__ import annotations

import pathlib

import pytest

from migration_safety import (
    ADDITIVE,
    DESTRUCTIVE,
    UNKNOWN,
    classify_files,
    classify_sql,
    rollback_is_safe,
)

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"


# ── The shapes that break a previous release ───────────────────────────────

@pytest.mark.parametrize("sql, because", [
    ("ALTER TABLE leads DROP COLUMN health;", "the old app still SELECTs it"),
    ("DROP TABLE mls_sync_status;", "the old app still reads it"),
    ("ALTER TABLE leads RENAME COLUMN a TO b;", "the old app uses the old name"),
    ("ALTER TABLE leads ALTER COLUMN score TYPE bigint;", "the old app writes the old type"),
    ("ALTER TABLE leads ALTER COLUMN state SET NOT NULL;", "the old app inserts without it"),
    ("ALTER TABLE leads ADD COLUMN tier text NOT NULL;", "the old app's INSERTs omit it"),
    ("DROP FUNCTION app_current_tenant();", "the old app calls it"),
    ("TRUNCATE leads;", "data is gone"),
    ("REVOKE SELECT ON leads FROM oracle_app;", "the old app loses a privilege it holds"),
])
def test_breaking_changes_are_destructive(sql, because):
    verdict = classify_sql("t.sql", sql)
    assert verdict.classification == DESTRUCTIVE, because
    assert not verdict.rollback_safe


@pytest.mark.parametrize("sql", [
    "CREATE TABLE t (id uuid PRIMARY KEY);",
    "ALTER TABLE leads ADD COLUMN tier text;",
    "ALTER TABLE leads ADD COLUMN tier text NOT NULL DEFAULT 'a';",
    "CREATE INDEX idx_leads_state ON leads (state);",
    "CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;",
    "GRANT SELECT ON leads TO oracle_app;",
    "COMMENT ON TABLE leads IS 'x';",
])
def test_additive_changes_leave_the_previous_release_working(sql):
    assert classify_sql("t.sql", sql).classification == ADDITIVE


def test_a_not_null_column_with_a_default_is_additive():
    """The distinction that matters: NOT NULL alone breaks the old app's
    INSERTs; NOT NULL DEFAULT does not, because the default fills it in."""
    assert classify_sql("a.sql", "ALTER TABLE t ADD COLUMN c text NOT NULL;").classification == DESTRUCTIVE
    assert classify_sql("b.sql", "ALTER TABLE t ADD COLUMN c text NOT NULL DEFAULT 'x';").classification == ADDITIVE


# ── The three exemptions, each from a real migration ───────────────────────

def test_drop_then_recreate_is_a_replace_not_a_removal():
    """0002 does exactly this. Postgres has no CREATE POLICY IF NOT EXISTS, so
    dropping and recreating is the idiom — and calling it destructive made the
    classifier wrong about half this repository."""
    verdict = classify_sql("0002.sql", """
        DROP POLICY IF EXISTS listings_read ON listings;
        CREATE POLICY listings_read ON listings FOR SELECT USING (true);
    """)
    assert verdict.classification == ADDITIVE


def test_a_policy_dropped_and_NOT_recreated_is_still_destructive():
    """The exemption must not swallow a real removal."""
    verdict = classify_sql("x.sql", "DROP POLICY IF EXISTS listings_read ON listings;")
    assert verdict.classification == DESTRUCTIVE


def test_revoking_from_public_is_hardening_not_removal():
    """0003's least-privilege pass. The app keeps working because it holds an
    explicit grant; PUBLIC's implicit one is what goes away."""
    verdict = classify_sql("0003.sql", """
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO oracle_app;
    """)
    assert verdict.classification == ADDITIVE


def test_revoking_on_a_table_created_in_the_same_migration_is_harmless():
    """0110 revokes on mls_feed_entitlements twelve lines after creating it.
    A previous release cannot depend on privileges for an object it never
    knew existed."""
    verdict = classify_sql("0110.sql", """
        CREATE TABLE mls_feed_entitlements (tenant_id uuid, mls_id text);
        REVOKE ALL ON mls_feed_entitlements FROM PUBLIC;
        REVOKE ALL ON mls_feed_entitlements FROM oracle_app;
        GRANT SELECT ON mls_feed_entitlements TO oracle_app;
    """)
    assert verdict.classification == ADDITIVE


def test_revoking_on_a_pre_existing_table_is_still_destructive():
    verdict = classify_sql("x.sql", "REVOKE ALL ON leads FROM oracle_app;")
    assert verdict.classification == DESTRUCTIVE


# ── Prose must not trip it ─────────────────────────────────────────────────

def test_a_comment_mentioning_a_drop_is_not_a_drop():
    """These migrations explain themselves at length, often describing what
    they deliberately do NOT do."""
    verdict = classify_sql("x.sql", """
        -- Deliberately does NOT do: ALTER TABLE leads DROP COLUMN payload;
        /* A previous attempt used DROP TABLE leads; that was wrong. */
        ALTER TABLE leads ADD COLUMN note text;
    """)
    assert verdict.classification == ADDITIVE


# ── Unrecognised is not safe ───────────────────────────────────────────────

def test_unrecognised_sql_is_not_treated_as_safe():
    """UNKNOWN must never read as ADDITIVE. The cost is asymmetric: a needless
    manual review costs minutes; a rollback onto an incompatible schema costs
    the database."""
    verdict = classify_sql("x.sql", "SET work_mem = '64MB';")
    assert verdict.classification == UNKNOWN
    assert not verdict.rollback_safe


def test_one_destructive_migration_blocks_the_whole_set():
    """Rolling back past a column drop is unsafe whatever the other twenty
    migrations did. A summary that averaged them would be worse than none."""
    verdicts = [
        classify_sql("a.sql", "CREATE TABLE t (id int);"),
        classify_sql("b.sql", "ALTER TABLE t DROP COLUMN id;"),
        classify_sql("c.sql", "CREATE INDEX i ON t (id);"),
    ]
    safe, blocking = rollback_is_safe(verdicts)
    assert safe is False
    assert len(blocking) == 1 and blocking[0].startswith("b.sql")

    safe, blocking = rollback_is_safe([verdicts[0], verdicts[2]])
    assert safe is True and blocking == []


# ── Against the real migrations ────────────────────────────────────────────

def test_the_repositorys_own_migrations_classify_sensibly():
    verdicts = classify_files(sorted(MIGRATIONS.glob("*.sql")))
    assert len(verdicts) > 100

    by_name = {v.filename: v for v in verdicts}

    # 0111 drops four columns. It is the reason this module exists.
    assert by_name["0111_mls_drop_unfed_columns.sql"].classification == DESTRUCTIVE
    # 0110 creates a table and revokes on it — additive despite the REVOKE.
    assert by_name["0110_mls_production_readiness.sql"].classification == ADDITIVE
    # 0002/0003 are the drop-recreate and revoke-from-PUBLIC idioms.
    assert by_name["0002_listing_grants.sql"].classification == ADDITIVE
    assert by_name["0003_hardening.sql"].classification == ADDITIVE

    destructive = [v for v in verdicts if v.classification == DESTRUCTIVE]
    assert len(destructive) < len(verdicts) / 3, (
        "more than a third of migrations flagged destructive — a warning that "
        "fires this often gets overridden by habit and stops being a warning. "
        f"Currently {len(destructive)}/{len(verdicts)}."
    )


def test_every_verdict_explains_itself():
    """An operator reading this mid-incident needs the consequence, not a
    restatement of the SQL."""
    for verdict in classify_files(sorted(MIGRATIONS.glob("*.sql"))):
        assert verdict.reasons, f"{verdict.filename} gave no reason"
        assert all(r.strip() for r in verdict.reasons)
