from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import sales_api
from sales_api import (
    PlanCreate,
    PlanDefinition,
    PlanStep,
    ProviderSetupInput,
    _provider_payload,
    _sign_preview,
    _verify_preview,
)
from tenancy import Role, TenantContext


def test_smart_plan_accepts_all_supported_channels_and_rejects_unknown_fields():
    definition = PlanDefinition(
        steps=[
            PlanStep(key="wait_1", type="wait", delay_minutes=15),
            PlanStep(key="task_1", type="task", title="Review contact", priority="high"),
            PlanStep(key="email_1", type="email", subject="Next steps", body="Hello"),
            PlanStep(key="sms_1", type="sms", body="Are you available tomorrow?"),
            PlanStep(key="call_1", type="approved_call", body="Introduce yourself and confirm timing."),
        ]
    )

    plan = PlanCreate(name="Buyer follow-up", definition=definition)
    assert [step.type for step in plan.definition.steps] == [
        "wait",
        "task",
        "email",
        "sms",
        "approved_call",
    ]
    with pytest.raises(ValidationError, match="Extra inputs"):
        PlanStep.model_validate({"key": "wait_2", "type": "wait", "destination": "+13055550142"})


def test_smart_plan_requires_channel_specific_draft_fields_and_unique_keys():
    with pytest.raises(ValidationError, match="subject and body"):
        PlanStep(key="email_1", type="email", body="No subject")
    with pytest.raises(ValidationError, match="1-1600"):
        PlanStep(key="sms_1", type="sms", body="x" * 1601)
    with pytest.raises(ValidationError, match="unique"):
        PlanDefinition(
            steps=[
                PlanStep(key="wait_1", type="wait"),
                PlanStep(key="wait_1", type="wait"),
            ]
        )


def test_preview_signature_is_tenant_bound_tamper_evident_and_expires(monkeypatch):
    monkeypatch.setenv("ORACLE_SMART_PLAN_SIGNING_KEY", "s" * 64)
    payload = {
        "version": 1,
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "plan_id": "22222222-2222-2222-2222-222222222222",
        "contact_ids": ["33333333-3333-3333-3333-333333333333"],
        "start_at": "2026-08-03T18:00:00+00:00",
        "fingerprint": "f" * 64,
        "issued_at": int(sales_api.datetime.now(sales_api.timezone.utc).timestamp()),
    }
    token = _sign_preview(payload)
    assert _verify_preview(token) == payload

    raw, signature = token.split(".", 1)
    replacement = "0" if signature[-1] != "0" else "1"
    with pytest.raises(HTTPException, match="invalid"):
        _verify_preview(f"{raw}.{signature[:-1]}{replacement}")

    monkeypatch.setattr(sales_api, "_PREVIEW_TTL_SECONDS", -1)
    with pytest.raises(HTTPException, match="expired"):
        _verify_preview(token)


def test_provider_setup_is_structured_and_twilio_browser_call_ready():
    sid = "AC" + "a" * 32
    api_key = "SK" + "b" * 32
    app_sid = "AP" + "c" * 32
    body = ProviderSetupInput(
        account_label="team",
        account_sid=sid,
        auth_token="auth-token-value",
        api_key=api_key,
        api_secret="api-secret-value",
        from_number="+13055550142",
        twiml_app_sid=app_sid,
        sms_sender="+13055550143",
        sms_sender_type="twilio_registered",
    )
    payload = _provider_payload("twilio", body)

    assert payload["account_sid"] == sid
    assert payload["twiml_app_sid"] == app_sid
    assert payload["sms_sender_type"] == "twilio_registered"
    assert "account_label" not in payload
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProviderSetupInput.model_validate({"account_label": "x", "raw_secret_blob": "no"})


def test_ses_provider_requires_complete_optional_tenant_key_pair():
    with pytest.raises(ValidationError, match="configured together"):
        ProviderSetupInput(
            account_label="team",
            from_email="broker@example.test",
            aws_access_key_id="A" * 20,
        )
    body = ProviderSetupInput(
        account_label="team",
        from_email="broker@example.test",
        region="us-east-2",
        aws_access_key_id="A" * 20,
        aws_secret_access_key="s" * 40,
        aws_session_token="t" * 32,
    )
    payload = _provider_payload("ses", body)
    assert payload["aws_access_key_id"] == "A" * 20
    assert payload["aws_session_token"] == "t" * 32


def test_sales_migration_forces_rls_and_keeps_revisions_immutable_and_destinations_out():
    migration = (
        Path(__file__).parents[1] / "db" / "migrations" / "0058_sales_ai_stack.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "smart_plans",
        "smart_plan_revisions",
        "smart_plan_enrollments",
        "smart_plan_step_runs",
        "agent_call_intents",
    ):
        assert f"'{table}'" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "BEFORE UPDATE OR DELETE ON smart_plan_revisions" in migration
    assert "REVOKE DELETE, TRUNCATE" in migration

    call_table = migration.split("CREATE TABLE IF NOT EXISTS agent_call_intents", 1)[1]
    call_table = call_table.split("CREATE INDEX", 1)[0]
    assert "contact_id" in call_table
    assert "destination" not in call_table
    assert "phone" not in call_table


def test_personal_and_team_plan_access_is_role_safe():
    tenant_id = "11111111-1111-1111-1111-111111111111"
    owner = TenantContext("owner", tenant_id, Role.AGENT)
    other = TenantContext("other", tenant_id, Role.AGENT)
    broker = TenantContext("broker", tenant_id, Role.BROKER_OWNER)
    personal = {"scope": "personal", "owner_agent_id": "owner"}
    team = {"scope": "team", "owner_agent_id": "broker"}

    assert sales_api._plan_readable_by_agent(personal, owner)
    assert sales_api._plan_mutable_by_agent(personal, owner)
    assert not sales_api._plan_readable_by_agent(personal, other)
    assert sales_api._plan_readable_by_agent(team, other)
    assert not sales_api._plan_mutable_by_agent(team, other)
    assert sales_api._plan_mutable_by_agent(team, broker)
