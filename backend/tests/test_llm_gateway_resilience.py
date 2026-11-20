"""AI32-08: the gateway keeps working when a configured model is gone.

Measured 2026-11-20: Azure Foundry unreachable (subscription disabled), the
fast default glm-5p2-fast retired ("Model not found"), and a .env naming a
model the account no longer serves — so the semantic parser abstained on all
129 evaluation cases. A stale model name now falls back to the release
default, repeated definite failures are skipped instead of eating every
call's deadline, and the health snapshot names what is being skipped."""

from __future__ import annotations

import asyncio

import pytest

import component_health as ch
import llm_gateway
from tests.test_llm_gateway import _FakeLiteLLM, _FakeResponse


class NotFoundError(Exception):
    """Named like litellm's: the breaker classifies by exception name."""


class APIConnectionError(Exception):
    pass


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    for name in ("AZURE_AI_API_KEY", "ORACLE_FOUNDRY_API_KEY", "ORACLE_FOUNDRY_PROJECT_ENDPOINT",
                 "ORACLE_AI_BEDROCK_FALLBACK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORACLE_AI_LOCAL_FALLBACK", "0")
    monkeypatch.setattr(llm_gateway, "_breaker", {})
    monkeypatch.setattr(llm_gateway, "counter", llm_gateway._Counter())
    return monkeypatch


def test_a_configured_model_is_tried_first_then_the_release_default(monkeypatch):
    monkeypatch.setenv("ORACLE_FIREWORKS_MODEL", "accounts/fireworks/models/retired-model")
    assert llm_gateway._configured_then_default("fireworks", "ORACLE_FIREWORKS_MODEL", "default-model") == (
        ("fireworks", "accounts/fireworks/models/retired-model"), ("fireworks", "default-model"))
    monkeypatch.setenv("ORACLE_FIREWORKS_MODEL", "default-model")
    assert llm_gateway._configured_then_default("fireworks", "ORACLE_FIREWORKS_MODEL", "default-model") == (
        ("fireworks", "default-model"),)
    monkeypatch.delenv("ORACLE_FIREWORKS_MODEL")
    assert llm_gateway._configured_then_default("fireworks", "ORACLE_FIREWORKS_MODEL", "default-model") == (
        ("fireworks", "default-model"),)


def test_the_fast_default_is_the_measured_model_not_the_retired_router():
    assert llm_gateway.FIREWORKS_FAST_DEFAULT.endswith("deepseek-v4p1-flash-1017-ultrafast")
    fast_models = [m for _, m in llm_gateway._TASK_MODELS["fast"]]
    assert all("glm-5p2-fast" not in m for m in fast_models)


def test_a_stale_model_name_degrades_to_the_default_instead_of_failing(fresh):
    fresh.setenv("ORACLE_FIREWORKS_MODEL", "accounts/fireworks/models/retired-model")
    fresh.setattr(llm_gateway, "_TASK_MODELS", {"analysis": llm_gateway._configured_then_default(
        "fireworks", "ORACLE_FIREWORKS_MODEL", llm_gateway.FIREWORKS_ANALYSIS_DEFAULT)})
    fake = _FakeLiteLLM(NotFoundError("Model not found"), _FakeResponse("answer"))
    fresh.setattr(llm_gateway, "_litellm", lambda: fake)
    assert asyncio.run(llm_gateway.complete("hi", task="analysis")) == "answer"
    assert [c["model"].split("/")[-1] for c in fake.calls] == ["retired-model", "kimi-k3"]


def _two_fireworks(fresh):
    fresh.setattr(llm_gateway, "_TASK_MODELS", {"fast": (("fireworks", "dead-model"), ("fireworks", "good-model")),
                                                 "analysis": (("fireworks", "analysis-model"),)})


def test_repeated_definite_failures_skip_the_dead_candidate(fresh):
    _two_fireworks(fresh)
    fake = _FakeLiteLLM(NotFoundError("gone"), _FakeResponse("a"), NotFoundError("gone"), _FakeResponse("b"),
                        _FakeResponse("c"))
    fresh.setattr(llm_gateway, "_litellm", lambda: fake)
    for _ in range(3):
        asyncio.run(llm_gateway.complete("hi", task="fast"))
    tried = [c["model"].split("/")[-1] for c in fake.calls]
    assert tried == ["dead-model", "good-model", "dead-model", "good-model", "good-model"], \
        "after two definite failures the dead model is no longer tried first"
    assert [p.model.split("/")[-1] for p in llm_gateway.providers_for("fast")] == ["good-model"]
    health = {r["model"]: r for r in llm_gateway.provider_health()["fast"]}
    assert {r["model"] for r in llm_gateway.provider_health()["analysis"]} == {"analysis-model"}
    assert health["dead-model"]["state"] == "skipped" and health["dead-model"]["kind"] == "model_not_found"
    assert health["good-model"]["state"] == "ok"


def test_timeouts_and_unknown_errors_never_open_the_breaker(fresh):
    _two_fireworks(fresh)
    fake = _FakeLiteLLM(TimeoutError(), _FakeResponse("a"), RuntimeError("429"), _FakeResponse("b"))
    fresh.setattr(llm_gateway, "_litellm", lambda: fake)
    for _ in range(2):
        asyncio.run(llm_gateway.complete("hi", task="fast"))
    assert len(llm_gateway.providers_for("fast")) == 2 and llm_gateway._breaker == {}


def test_a_success_closes_the_breaker_and_all_open_still_offers_everything(fresh):
    _two_fireworks(fresh)
    dead, good = (llm_gateway._fireworks(m) for m in ("dead-model", "good-model"))
    for provider in (dead, good):
        for _ in range(llm_gateway.BREAKER_THRESHOLD):
            llm_gateway._breaker_record(provider, ok=False, exc=APIConnectionError())
    assert len(llm_gateway.providers_for("fast")) == 2, "every candidate open: offer them all rather than none"
    llm_gateway._breaker_record(dead, ok=True)
    assert llm_gateway._breaker_open(dead) is False and llm_gateway._breaker_open(good) is True


def test_the_ai_component_names_skipped_models(monkeypatch):
    monkeypatch.setattr(llm_gateway, "provider_health", lambda: {
        "fast": [{"provider": "foundry", "model": "Kimi-K2.6", "state": "skipped", "failures": 2, "kind": "unreachable"}],
        "analysis": [{"provider": "fireworks", "model": "kimi-k3", "state": "ok"}]})
    healthy = ch._c(ch.HEALTHY, "Neoh responding", total_15m=4)
    out = ch._with_model_health(healthy)
    assert out["state"] == ch.DEGRADED and "foundry/Kimi-K2.6 (unreachable)" in out["summary"]
    assert out["detail"]["total_15m"] == 4 and "models" in out["detail"]
    failing = ch._c(ch.UNAVAILABLE, "Neoh responses are failing")
    assert ch._with_model_health(failing)["state"] == ch.UNAVAILABLE, "never softens a worse state"
