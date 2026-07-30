from __future__ import annotations

import asyncio
import json

import pytest

import scraper_supervisor
from market_research_agent import _default_specs


def _valid_payload(specs):
    return {
        "dataset_order": [spec.key for spec in reversed(specs)],
        "max_concurrency": 3,
        "attempts_per_source": 2,
        "retry_backoff_seconds": 4,
        "reason": "Prioritize federal indexes, then publisher research.",
    }


def test_model_plan_can_order_only_the_complete_allowlisted_catalog(monkeypatch):
    specs = _default_specs()
    monkeypatch.setenv("ORACLE_MARKET_AI_SUPERVISOR_MODEL", "gpt-oss-120b")

    plan = scraper_supervisor._parse_model_plan(
        json.dumps(_valid_payload(specs)),
        specs,
    )

    assert plan.dataset_order == tuple(spec.key for spec in reversed(specs))
    assert plan.max_concurrency == 3
    assert plan.attempts_per_source == 2
    assert plan.retry_backoff_seconds == 4
    assert plan.planner == "azure-foundry:gpt-oss-120b"


def test_structured_output_schema_allows_only_known_plan_fields_and_datasets():
    specs = _default_specs()
    schema = scraper_supervisor._response_text_config(specs)["format"]["schema"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "dataset_order",
        "max_concurrency",
        "attempts_per_source",
        "retry_backoff_seconds",
        "reason",
    }
    assert schema["properties"]["dataset_order"]["items"]["enum"] == [
        spec.key for spec in specs
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"command": "python evil.py"}),
        lambda payload: payload.update({"url": "https://example.com/listings"}),
        lambda payload: payload.update({"dataset_order": ["zillow_research_zhvi"]}),
        lambda payload: payload.update({"max_concurrency": 99}),
        lambda payload: payload.update({"attempts_per_source": 20}),
    ],
)
def test_model_plan_rejects_authority_expansion(mutation):
    specs = _default_specs()
    payload = _valid_payload(specs)
    mutation(payload)

    with pytest.raises(ValueError):
        scraper_supervisor._parse_model_plan(json.dumps(payload), specs)


def test_invalid_foundry_response_falls_back_without_disabling_sources(monkeypatch):
    specs = _default_specs()
    monkeypatch.setenv("ORACLE_MARKET_AI_SUPERVISOR_ENABLED", "1")

    def invalid_response(_specs):
        return '{"dataset_order":["unknown"],"shell":"curl consumer-site"}'

    monkeypatch.setattr(scraper_supervisor, "_request_model_plan", invalid_response)
    plan = asyncio.run(scraper_supervisor.build_scraper_plan(specs))

    assert plan.planner == "deterministic-fallback"
    assert plan.dataset_order == tuple(spec.key for spec in specs)
    assert 1 <= plan.max_concurrency <= 4
    assert plan.attempts_per_source == 2


def test_disabled_supervisor_never_calls_foundry(monkeypatch):
    specs = _default_specs()
    monkeypatch.setenv("ORACLE_MARKET_AI_SUPERVISOR_ENABLED", "0")

    def unexpected_call(_specs):
        raise AssertionError("Foundry should not be called when disabled")

    monkeypatch.setattr(scraper_supervisor, "_request_model_plan", unexpected_call)
    plan = asyncio.run(scraper_supervisor.build_scraper_plan(specs))

    assert plan.planner == "deterministic"
    assert plan.dataset_order == tuple(spec.key for spec in specs)
