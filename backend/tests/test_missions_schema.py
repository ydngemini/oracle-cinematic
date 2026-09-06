"""The safety design of Missions lives in constraints, not in code.

A mission is the first thing in this product that *pursues* rather than
reports, so the guarantees have to survive a future author who has not read
the design. Everything asserted here was also exercised against the live
database when 0100 was applied; these tests are what stop it regressing.
"""

from __future__ import annotations

import pathlib

import pytest

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "0100_missions.sql"
)


@pytest.fixture(scope="module")
def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_the_standing_autonomy_ceiling_is_not_touched(sql):
    """0095 pins calls/texts/emails to at most 'assist' AT THE DATABASE.

    A mission carries its own consented grant instead of raising that dial. If
    this file ever alters autonomy_preferences, the ceiling that protects every
    other path in the product has been quietly moved.
    """
    assert "autonomy_preferences" not in sql, (
        "missions must not touch the standing autonomy dial"
    )
    assert "autonomy_outbound_ceiling" not in sql
    assert "autonomy_consequential_ceiling" not in sql


def test_a_grant_cannot_exist_without_the_consent_sentence(sql):
    """Not a boolean: "they ticked a box" is not a record of what the box said."""
    assert "missions_grant_requires_consent" in sql
    clause = sql.split("CONSTRAINT missions_grant_requires_consent CHECK (")[1].split(");")[0]
    assert "auto_channels = '{}'::text[]" in clause
    assert "consent_at IS NOT NULL" in clause
    assert "consent_text IS NOT NULL" in clause
    # An empty string is not consent.
    assert "length(btrim(consent_text)) > 0" in clause


def test_a_grant_cannot_exceed_what_the_mission_may_use(sql):
    assert "auto_channels  text[]" in sql
    assert "CHECK (auto_channels <@ allowed_channels)" in sql
    assert "allowed_channels <@ ARRAY['email','sms','voice','task']::text[]" in sql


def test_live_requires_a_simulation(sql):
    """Nobody points this at their real database and presses go unseen."""
    assert "missions_live_requires_simulation" in sql
    clause = sql.split("CONSTRAINT missions_live_requires_simulation CHECK (")[1].split(");")[0]
    assert "mode = 'shadow' OR simulated_at IS NOT NULL" in clause


def test_a_withheld_action_must_say_why(sql):
    """"would_have_done" with no reason is indistinguishable from a bug, and
    this is the column the UI shows when a mission reports it did nothing."""
    assert "mission_actions_withheld_has_a_reason" in sql
    clause = sql.split("CONSTRAINT mission_actions_withheld_has_a_reason CHECK (")[1].split(");")[0]
    assert "'would_have_done'" in clause and "'blocked'" in clause
    assert "blocked_reason IS NOT NULL" in clause


def test_an_excluded_candidate_must_say_why(sql):
    assert "mission_candidates_exclusion_has_a_reason" in sql


def test_shadow_is_the_same_work_with_the_last_step_withheld(sql):
    """`would_have_done` is a STATE of the same row, not a separate table and
    not a skipped code path. That is what makes the count of actions the count
    of intentions whether or not anything was sent."""
    states = sql.split("state         text NOT NULL DEFAULT 'planned' CHECK (state IN (")[1].split("))")[0]
    for state in ("planned", "would_have_done", "staged", "approved",
                  "executed", "blocked", "skipped", "failed"):
        assert f"'{state}'" in states, state


def test_a_mission_cannot_point_at_another_tenants_command(sql):
    """The composite FK is the enforcement; a plain uuid column would let a
    mission attach itself to any command row in the database."""
    assert "mission_actions_command_fk" in sql
    fk = sql.split("CONSTRAINT mission_actions_command_fk")[1].split(",\n\n")[0]
    assert "FOREIGN KEY (tenant_id, command_id)" in fk
    assert "REFERENCES command_executions (tenant_id, id)" in fk


def test_history_cannot_be_deleted(sql):
    """A planned action is a thing the system intended. Deleting one would make
    a mission's own history disagree with its receipts."""
    assert "REVOKE DELETE ON mission_actions FROM oracle_app;" in sql
    assert "REVOKE UPDATE, DELETE ON mission_events FROM oracle_app;" in sql


@pytest.mark.parametrize(
    "table",
    ["missions", "mission_candidates", "mission_actions",
     "mission_events", "tenant_action_budgets"],
)
def test_every_table_is_rls_forced_and_granted(sql, table):
    """FORCE, or the table owner bypasses its own policy."""
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in sql
    assert f"ALTER TABLE {table} FORCE  ROW LEVEL SECURITY;" in sql
    policy = sql.split(f"CREATE POLICY {table}")[1].split(";")[0]
    assert "app_is_platform_admin() OR tenant_id = app_current_tenant()" in policy
    assert "WITH CHECK" in policy, f"{table} can be written across tenants"
    assert f"ON {table} TO oracle_app;" in sql


def test_the_executors_hot_predicate_is_literal_matchable(sql):
    """A partial index the planner cannot match is no index — the lesson from
    the ledger work. The executor asks for exactly `state = 'planned'`."""
    assert "WHERE state = 'planned'" in sql
    assert "idx_mission_actions_planned" in sql
    assert "WHERE status IN ('shadow', 'active')" in sql


def test_the_objective_is_kept_in_the_agents_own_words(sql):
    """The planner is shown this rather than a normalised summary, and a person
    reading the mission later needs what was asked, not what we inferred."""
    assert "objective_text text NOT NULL CHECK (length(btrim(objective_text)) > 0)" in sql
