"""Autonomy modes expand internal work without weakening approval invariants."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_profile import AISettingsInput


MIGRATION = (
    Path(__file__).parents[1] / "db" / "migrations" / "0057_agent_autonomy_mode.sql"
)


def test_policy_autopilot_is_the_default_and_full_autonomy_is_explicit():
    assert AISettingsInput().autonomy_mode == "policy_autopilot"
    assert AISettingsInput(autonomy_mode="full_autonomy").autonomy_mode == "full_autonomy"
    with pytest.raises(ValidationError):
        AISettingsInput(autonomy_mode="unbounded")


def test_database_keeps_external_action_gates_non_bypassable():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "DEFAULT 'policy_autopilot'" in sql
    assert "('policy_autopilot','full_autonomy')" in sql
    assert "CHECK (outreach_requires_approval)" in sql
    assert "CHECK (calls_require_approval)" in sql
    assert "CHECK (legal_requires_approval)" in sql
    assert "cannot bypass consent, DNC, Fair Housing, spend, call, or legal approvals" in sql
