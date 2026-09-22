from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import asyncio

import client_ai_automation
from client_ai_automation import (
    ModelSignals,
    _assert_client_beliefs,
    _automatic_stage,
    _request_model_signals,
    _score,
    _signals_response_format,
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


def test_signal_extraction_runs_through_the_llm_gateway_not_azure(monkeypatch):
    """The extractor was a direct Azure Foundry SDK call. It must now go through
    the shared gateway (Foundry -> Fireworks -> local), and it must demand the
    JSON schema so a prose-only provider is skipped rather than mis-parsed."""
    seen: dict = {}

    async def fake_complete(prompt, *, task, system, response_format, max_tokens, timeout):
        seen.update(
            prompt=prompt, task=task, response_format=response_format, timeout=timeout
        )
        return '{"summary":"x","explicit_intent":"buyer","timeline_days":30,' \
               '"actionable_response":true,"budget_max":450000,"target_zips":["78701"],' \
               '"next_action":null,"evidence_refs":["note:1"]}'

    fake_gateway = type("g", (), {"complete": staticmethod(fake_complete)})
    monkeypatch.setitem(__import__("sys").modules, "llm_gateway", fake_gateway)

    facts = [{"id": "note:1", "text": "Wants to buy in 78701 under 450k in a month."}]
    signals, model_id = asyncio.run(_request_model_signals(facts, timeout=10))

    assert model_id == "llm-gateway"
    assert signals.budget_max == 450000
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["strict"] is True


def test_signal_extraction_rejects_evidence_outside_the_supplied_facts(monkeypatch):
    async def fake_complete(prompt, **kw):
        return '{"summary":"x","explicit_intent":"unknown","timeline_days":null,' \
               '"actionable_response":false,"budget_max":null,"target_zips":[],' \
               '"next_action":null,"evidence_refs":["note:99"]}'

    monkeypatch.setitem(
        __import__("sys").modules, "llm_gateway",
        type("g", (), {"complete": staticmethod(fake_complete)}),
    )
    with pytest.raises(ValueError, match="evidence outside"):
        asyncio.run(_request_model_signals([{"id": "note:1", "text": "hi"}], timeout=10))


def test_response_format_carries_the_signals_schema():
    rf = _signals_response_format()
    assert rf["json_schema"]["name"] == "client_crm_signals"
    assert "budget_max" in rf["json_schema"]["schema"]["properties"]


def test_beliefs_are_asserted_only_from_evidence_cited_signals(monkeypatch):
    import belief_store

    asserted: list[dict] = []

    async def fake_assert(ctx, **kw):
        asserted.append(kw)
        return {}

    fake_bs = type("bs", (), {
        "assert_belief": staticmethod(fake_assert),
        "BeliefSource": belief_store.BeliefSource,
    })
    monkeypatch.setitem(__import__("sys").modules, "belief_store", fake_bs)

    signals = ModelSignals.model_validate({
        "summary": "Buyer, 450k, 78701, 30 days",
        "explicit_intent": "buyer", "budget_max": 450000, "timeline_days": 30,
        "target_zips": ["78701", "78702"], "evidence_refs": ["note:1"],
    })
    asyncio.run(_assert_client_beliefs(object(), "c-1", signals))

    preds = {a["predicate"]: a for a in asserted}
    assert preds["max_budget"]["value"] == 450000
    assert preds["timeline"]["value"] == 30
    assert {a["value"] for a in asserted if a["predicate"] == "prefers_area"} == {"78701", "78702"}
    assert all(a["source"].kind == "model" for a in asserted)  # CHECK-enum safe
    assert all(0.0 < a["confidence"] < 1.0 for a in asserted)


def test_migration_backfills_through_force_rls_with_internal_only_jobs():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "set_config('app.current_role', 'platform_admin', true)" in sql
    assert "'crm:client_reconcile'" in sql
    assert "'internal_edit'" in sql
    assert "REVOKE DELETE, TRUNCATE" in sql


def test_property_candidates_bounded_degrades_instead_of_failing(monkeypatch):
    """A public_property_records lookup that blows its statement_timeout must
    return [] rather than let a bare TimeoutError() take down the whole
    reconcile — that TimeoutError() used to dead-letter the client entirely."""
    from contextlib import asynccontextmanager

    class _TimeoutConn:
        def __init__(self):
            self.executed = []

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, sql, *args):
            self.executed.append(sql)

        async def fetch(self, *a, **kw):
            raise TimeoutError()

    conn = _TimeoutConn()
    result = asyncio.run(
        client_ai_automation._property_candidates_bounded(conn, "Long Enough Name", [], 0)
    )

    assert result == []
    assert any("statement_timeout" in q for q in conn.executed)


def test_property_candidates_bounded_returns_real_matches(monkeypatch):
    from contextlib import asynccontextmanager

    row = {
        "id": "aaaaaaaa-0000-4000-8000-000000000000", "parcel_id": "P1",
        "address": "1 Main St", "city": "Dover", "state": "DE", "zip_code": "19901",
        "owner_name": "Jane Doe", "source_name": "county", "record_refreshed_at": NOW,
    }

    class _Conn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, sql, *args):
            pass

        async def fetch(self, *a, **kw):
            return [row]

    result = asyncio.run(
        client_ai_automation._property_candidates_bounded(_Conn(), "Jane Doe", [], 0)
    )
    assert len(result) == 1
    assert result[0]["owner_name"] == "Jane Doe"
