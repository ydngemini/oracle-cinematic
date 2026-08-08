from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from client_ai_automation import (
    ModelSignals,
    _automatic_stage,
    _score,
    automation_state_json,
    normalize_phone,
    normalize_preferences,
)


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
MIGRATION = Path(__file__).parents[1] / "db" / "migrations" / "0053_client_ai_steward.sql"


def test_example_client_normalizes_and_scores_only_recorded_facts():
    preferences = normalize_preferences({"budget": "$200K", "zip": "19963"})

    assert normalize_phone("13024078981") == "+13024078981"
    assert preferences["budget_max"] == 200_000
    assert preferences["target_zips"] == ["19963"]

    score, factors = _score(
        has_email=True,
        has_phone=True,
        preferences=preferences,
        last_inbound_at=None,
        actionable_response=False,
        properties=[],
        timeline_days=None,
        explicit_intent="unknown",
        now=NOW,
    )

    assert score == 20
    assert {factor["code"] for factor in factors} == {
        "valid_email", "valid_phone", "explicit_budget", "explicit_market",
    }


def test_stage_policy_never_invents_transaction_or_lost_status():
    base = {
        "score": 80,
        "last_inbound_at": NOW - timedelta(days=2),
        "has_property": True,
        "timeline_days": 30,
        "actionable_response": True,
        "now": NOW,
    }
    assert _automatic_stage(current_stage="lead", transaction_statuses=set(), **base) == "active"
    assert _automatic_stage(
        current_stage="lead", transaction_statuses={"under_contract"}, **base,
    ) == "under_contract"
    assert _automatic_stage(
        current_stage="lead", transaction_statuses={"closed"}, **base,
    ) == "closed"
    assert _automatic_stage(
        current_stage="lost", transaction_statuses=set(), **base,
    ) == "lost"


def test_stage_policy_keeps_unqualified_record_as_lead():
    assert _automatic_stage(
        current_stage="lead",
        score=20,
        last_inbound_at=None,
        has_property=False,
        timeline_days=None,
        actionable_response=False,
        transaction_statuses=set(),
        now=NOW,
    ) == "lead"


def test_model_signal_schema_rejects_unrecognized_fields_and_bad_zips():
    with pytest.raises(ValidationError):
        ModelSignals.model_validate({"target_zips": ["1996"], "assignee_id": "agent-1"})

    with pytest.raises(ValidationError):
        ModelSignals.model_validate({"explicit_intent": "seller", "evidence_refs": []})


def test_missing_state_is_explicitly_queued_not_fabricated():
    state = automation_state_json(None)

    assert state["status"] == "queued"
    assert state["score_mode"] == "auto"
    assert state["stage_mode"] == "auto"
    assert state["evidence"] == []
    assert state["property_candidates"] == []


def test_migration_backfills_through_force_rls_with_internal_only_jobs():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "set_config('app.current_role', 'platform_admin', true)" in sql
    assert "'crm:client_reconcile'" in sql
    assert "'internal_edit'" in sql
    assert "REVOKE DELETE, TRUNCATE" in sql
