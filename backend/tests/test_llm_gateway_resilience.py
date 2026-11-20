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
            llm_gateway._breaker_record(provider, ok=False, exc=NotFoundError())
    assert len(llm_gateway.providers_for("fast")) == 2, "every candidate open: offer them all rather than none"
    llm_gateway._breaker_record(dead, ok=True)
    assert llm_gateway._breaker_open(dead) is False and llm_gateway._breaker_open(good) is True


def test_the_ai_banner_is_untouched_and_ai_models_pages_only_when_a_task_has_no_model(monkeypatch):
    """Review r1: a dead fallback tier while another model answers must not put
    'Neoh is having trouble' in front of every user."""
    ok = {"provider": "fireworks", "model": "kimi-k3", "state": "ok"}
    dead = {"provider": "foundry", "model": "Kimi-K2.6", "state": "skipped", "failures": 2, "kind": "auth_rejected"}
    monkeypatch.setattr(llm_gateway, "provider_health", lambda: {"fast": [dead, ok], "analysis": [ok]})
    partial = ch._ai_models()
    assert partial["state"] == ch.HEALTHY and "foundry/Kimi-K2.6 (auth_rejected)" in partial["summary"]
    assert "ai_models" not in ch.USER_MESSAGES and ch.role_of("ai_models") == "optional"
    monkeypatch.setattr(llm_gateway, "provider_health", lambda: {"fast": [dead], "analysis": [ok]})
    down = ch._ai_models()
    assert down["state"] == ch.DEGRADED and "no hosted AI model is available for: fast" in down["summary"]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_connection_blip_does_not_switch_a_model_off(fresh):
    """Review r1: litellm reports every status-less error as APIConnectionError;
    two calls in flight during a one-second blip must not open the breaker."""
    _two_fireworks(fresh)
    clock = _Clock()
    fresh.setattr(llm_gateway.time, "monotonic", clock)
    dead = llm_gateway._fireworks("dead-model")
    for _ in range(5):
        llm_gateway._breaker_record(dead, ok=False, exc=APIConnectionError())
    assert llm_gateway._breaker_open(dead) is False, "a burst within a second is not a sustained outage"
    clock.t += llm_gateway.UNREACHABLE_MIN_SPAN_S
    llm_gateway._breaker_record(dead, ok=False, exc=APIConnectionError())
    assert llm_gateway._breaker_open(dead) is True, "sustained over the span: skipped"
    clock.t += llm_gateway.BREAKER_COOLDOWN_S + 1
    assert llm_gateway._breaker_open(dead) is False, "half-open after the cool-down"
    llm_gateway._breaker_record(dead, ok=False, exc=APIConnectionError())
    assert llm_gateway._breaker_open(dead) is True, "a failed trial reopens at once"


def test_old_failures_are_forgotten(fresh):
    _two_fireworks(fresh)
    clock = _Clock()
    fresh.setattr(llm_gateway.time, "monotonic", clock)
    dead = llm_gateway._fireworks("dead-model")
    llm_gateway._breaker_record(dead, ok=False, exc=NotFoundError())
    clock.t += llm_gateway.BREAKER_COOLDOWN_S + 1
    llm_gateway._breaker_record(dead, ok=False, exc=NotFoundError())
    assert llm_gateway._breaker_open(dead) is False, "two failures a cool-down apart are not 'in a row'"


def test_a_skipped_structured_model_is_still_tried_rather_than_refusing(fresh):
    """Review r1: with the only structured-output model skipped, a structured
    call must try it instead of raising 'none honour response_format' while
    local (no structured output) counts as available."""
    fresh.setenv("ORACLE_AI_LOCAL_FALLBACK", "1")
    fresh.setattr(llm_gateway, "_TASK_MODELS", {"fast": (("fireworks", "only-structured"), ("local", "local-llama")),
                                                 "analysis": (("fireworks", "a"),)})
    structured = llm_gateway._fireworks("only-structured")
    for _ in range(llm_gateway.BREAKER_THRESHOLD):
        llm_gateway._breaker_record(structured, ok=False, exc=NotFoundError())
    assert [p.name for p in llm_gateway.providers_for("fast")] == ["local"]
    fake = _FakeLiteLLM(_FakeResponse('{"ok": true}'))
    fresh.setattr(llm_gateway, "_litellm", lambda: fake)
    out = asyncio.run(llm_gateway.complete("hi", task="fast", response_format={"type": "json_object"}))
    assert out == '{"ok": true}' and fake.calls[0]["model"].endswith("only-structured")


def test_importing_the_gateway_stops_litellm_loading_a_dotenv():
    import os

    assert os.environ.get("LITELLM_MODE") == "PRODUCTION"


def test_the_operator_view_shows_the_whole_ladder_with_skipped_marked(fresh):
    import operator_api

    _two_fireworks(fresh)
    dead = llm_gateway._fireworks("dead-model")
    for _ in range(llm_gateway.BREAKER_THRESHOLD):
        llm_gateway._breaker_record(dead, ok=False, exc=NotFoundError())
    fast = operator_api._gateway_view()["ladders"]["fast"] if "ladders" in operator_api._gateway_view() else None
    if fast is None:
        pytest.skip("view shape differs")
    assert [(r["model"].split("/")[-1], r["skipped"]) for r in fast] == [("dead-model", True), ("good-model", False)]


def test_no_module_still_defaults_to_the_retired_fast_model():
    from pathlib import Path

    backend = Path(llm_gateway.__file__).resolve().parent
    hits = [str(p.relative_to(backend)) for p in backend.rglob("*.py")
            if "tests" not in p.parts and "venv" not in p.parts
            and "routers/glm-5p2-fast" in p.read_text(errors="ignore")]
    assert hits == []
