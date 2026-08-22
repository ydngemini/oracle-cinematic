"""The gateway, and the one thing it must never do.

Two hand-rolled provider ladders existed — `ml_forge/bedrock_client` chose
between Bedrock and Fireworks and left its seven callers to escalate
PRIMARY → SECONDARY themselves, while `ai_chat_agent` had a second set of rules
for Foundry/Bedrock/Fireworks/local. Collapsing them is only safe if the
collapsed version keeps one distinction the old code got right by accident:

**a text completion may be retried; a tool round may not.** By the time a tool
response comes back the handlers have already written to the database, so
re-sending the same conversation to a second provider re-runs those writes.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import llm_gateway
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="agent@tenant.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeLiteLLM:
    """Records every call and replies from a scripted list."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.suppress_debug_info = False
        self.drop_params = False

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else _FakeResponse("ok")
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setenv("ORACLE_AI_BEDROCK_FALLBACK", "1")
    # The ladder these tests assert on is fireworks -> bedrock -> local. A real
    # .env now carries Foundry credentials, and inheriting them inserts a fourth
    # provider mid-ladder — so the fixture states the environment rather than
    # accepting whatever the developer happens to have configured.
    for name in ("AZURE_AI_API_KEY", "ORACLE_FOUNDRY_API_KEY",
                 "ORACLE_FOUNDRY_PROJECT_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_gateway, "counter", llm_gateway._Counter())

    def _install(*replies):
        fake = _FakeLiteLLM(*replies)
        monkeypatch.setattr(llm_gateway, "_litellm", lambda: fake)
        return fake

    return _install


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

def test_a_tool_round_is_never_retried_against_a_second_provider(gateway):
    """The whole reason the gateway exists rather than a bare litellm call.

    _generate returns receipts instead of retrying once CRM writes have applied;
    a gateway-level fallback would undo that and double-apply them.
    """
    fake = gateway(RuntimeError("provider exploded"))

    with pytest.raises(llm_gateway.LLMUnavailable, match="tool round"):
        asyncio.run(llm_gateway.tool_call(
            [{"role": "user", "content": "update the client"}],
            [{"type": "function", "function": {"name": "update_client"}}],
        ))

    assert len(fake.calls) == 1, "a tool round was sent to more than one provider"
    assert "fallbacks" not in fake.calls[0]


def test_a_text_completion_does_fall_through(gateway):
    """A duplicate paragraph is the worst case here, so falling through is safe
    — and it is what replaces the callers' hand-rolled PRIMARY -> SECONDARY."""
    fake = gateway(RuntimeError("first provider down"), _FakeResponse("second answered"))

    answer = asyncio.run(llm_gateway.complete("summarise this"))

    assert answer == "second answered"
    assert len(fake.calls) == 2
    assert fake.calls[0]["model"].startswith("fireworks_ai/")
    assert fake.calls[1]["model"].startswith("bedrock/")


def test_empty_content_counts_as_a_failure_not_an_answer(gateway):
    """A reasoning model that spends its whole budget before emitting content
    returns "" with finish_reason=length. Passing that up hands the caller
    silence that looks like a considered reply."""
    fake = gateway(_FakeResponse(""), _FakeResponse("a real answer"))

    assert asyncio.run(llm_gateway.complete("x")) == "a real answer"
    assert len(fake.calls) == 2


def test_every_provider_failing_raises_rather_than_returning_empty(gateway):
    gateway(RuntimeError("a"), RuntimeError("b"), RuntimeError("c"))

    with pytest.raises(llm_gateway.LLMUnavailable, match="Every configured provider"):
        asyncio.run(llm_gateway.complete("x"))


def test_no_configured_provider_says_which_variables_would_fix_it(monkeypatch):
    monkeypatch.delenv("ORACLE_FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_AI_BEDROCK_FALLBACK", raising=False)
    monkeypatch.delenv("ORACLE_AI_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("AZURE_AI_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_FOUNDRY_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.setenv("ORACLE_AI_LOCAL_FALLBACK", "0")

    with pytest.raises(llm_gateway.LLMUnavailable, match="ORACLE_FIREWORKS_API_KEY"):
        asyncio.run(llm_gateway.complete("x"))


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

def test_the_timeout_bounds_the_whole_call_not_each_attempt(gateway):
    """Voice replies carry an 8-second wall clock. A per-attempt timeout would
    let two fallbacks spend 16 seconds and turn a late answer into no answer."""
    fake = gateway(RuntimeError("slow"), _FakeResponse("ok"))

    asyncio.run(llm_gateway.complete("x", timeout=30.0))

    first, second = fake.calls
    assert first["timeout"] <= 30.0
    assert second["timeout"] < first["timeout"], (
        "the second attempt was given a fresh budget instead of the remainder"
    )


def test_an_exhausted_deadline_stops_the_ladder(gateway, monkeypatch):
    fake = gateway(RuntimeError("first"), _FakeResponse("never reached"))
    # Two readings inside the budget (set the deadline, enter the first
    # attempt), then a clock well past it. Stateful rather than an iterator so
    # an extra reading cannot turn this into a StopIteration.
    ticks = {"n": 0}

    def _clock() -> float:
        ticks["n"] += 1
        return 0.0 if ticks["n"] <= 2 else 100.0

    # A fake module bound to the gateway's own `time` name. Patching
    # llm_gateway.time.monotonic would patch the real time module, and the
    # asyncio event loop reads the clock too — it drained the fake before the
    # gateway ever saw it.
    monkeypatch.setattr(llm_gateway, "time", types.SimpleNamespace(monotonic=_clock))

    with pytest.raises(llm_gateway.LLMUnavailable):
        asyncio.run(llm_gateway.complete("x", timeout=10.0))

    assert len(fake.calls) == 1, "the ladder kept going past its deadline"


# ---------------------------------------------------------------------------
# Accounting and the sync seam
# ---------------------------------------------------------------------------

def test_calls_are_counted_per_provider_so_the_ambient_budget_is_measurable(gateway):
    """At one call per socket every six seconds the ambient monologue was
    ~500/minute per replica, and the only way to know was to read the code."""
    gateway(RuntimeError("down"), _FakeResponse("ok"))
    asyncio.run(llm_gateway.complete("x"))

    snapshot = llm_gateway.counter.snapshot()
    assert snapshot["failures"]["analysis:fireworks"] == 1
    assert snapshot["calls"]["analysis:bedrock"] == 1
    assert snapshot["total_calls"] == 1
    assert snapshot["calls_per_minute"] > 0


def test_the_sync_wrapper_refuses_to_run_inside_an_event_loop(gateway):
    """asyncio.run() inside a running loop raises; the seven callers reach this
    through asyncio.to_thread, where there is no loop. Saying so beats
    deadlocking if that ever changes."""
    async def _inside():
        with pytest.raises(RuntimeError, match="running event loop"):
            llm_gateway.complete_sync("x")

    asyncio.run(_inside())


def test_the_sync_wrapper_returns_none_rather_than_raising(gateway, monkeypatch):
    """invoke_bedrock_model's contract, which its seven callers branch on."""
    gateway(RuntimeError("a"), RuntimeError("b"), RuntimeError("c"))
    assert llm_gateway.complete_sync("x") is None


def test_a_missing_litellm_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm", None)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _fail(name, *args, **kwargs):
        if name == "litellm":
            raise ImportError("no litellm")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fail)
    with pytest.raises(llm_gateway.LLMUnavailable, match="not installed"):
        llm_gateway._litellm()


def test_a_small_token_budget_is_floored_for_reasoning_models(gateway):
    """Measured against the local Qwen3 server: asked for 32 tokens it spent
    all of them on reasoning_content and returned "" with finish_reason=length.
    The empty-content check catches that, but the fix is to let the model
    finish — bedrock_client floors Fireworks calls at the same number."""
    fake = gateway(_FakeResponse("ok"))
    asyncio.run(llm_gateway.complete("x", max_tokens=32))

    assert fake.calls[0]["max_tokens"] == llm_gateway.MIN_TOKENS
    assert llm_gateway.MIN_TOKENS >= 2048


def test_a_generous_token_budget_is_left_alone(gateway):
    fake = gateway(_FakeResponse("ok"))
    asyncio.run(llm_gateway.complete("x", max_tokens=8000))
    assert fake.calls[0]["max_tokens"] == 8000


def test_the_runtime_load_endpoint_reads_a_real_number_now(monkeypatch):
    """/api/admin/runtime-load reported None for ambient LLM calls "until a
    gateway exposes a counter" — reporting 0 would have claimed a measurement
    nobody took. The gateway is that counter."""
    import admin_ops

    monkeypatch.setattr(llm_gateway, "counter", llm_gateway._Counter())
    assert admin_ops._ambient_llm_calls_last_minute() == 0

    llm_gateway.counter.record("analysis", "fireworks", ok=True)
    llm_gateway.counter.record("fast", "local", ok=False)
    assert admin_ops._ambient_llm_calls_last_minute() == 2


def test_the_window_is_rolling_not_cumulative(monkeypatch):
    """A process up for a day would otherwise report a number dominated by
    yesterday, which answers a different question than the endpoint asks."""
    counter = llm_gateway._Counter()
    counter.record("analysis", "local", ok=True)
    counter._recent = [counter._recent[0] - 120.0]  # one call, two minutes ago

    assert counter.calls_in_last(60.0) == 0
    assert counter.calls_in_last(300.0) == 1


def test_the_bedrock_seam_skips_the_gateway_when_litellm_is_absent(monkeypatch):
    """litellm is imported lazily inside the gateway, so importing the gateway
    proves nothing about reachability. Without checking the spec, a deployment
    that had not installed it yet took the gateway path on every call, failed,
    logged, and then did the direct call anyway."""
    import importlib.util

    import ml_forge.bedrock_client as bedrock

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name, *a, **k: None if name == "litellm" else real_find_spec(name, *a, **k),
    )
    assert bedrock._gateway_enabled() is False


def test_the_bedrock_seam_uses_the_gateway_when_it_can(monkeypatch):
    import ml_forge.bedrock_client as bedrock

    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    assert bedrock._gateway_enabled() is True


# ---------------------------------------------------------------------------
# Azure AI Foundry
# ---------------------------------------------------------------------------

def test_foundry_uses_the_resource_host_not_the_project_endpoint(monkeypatch):
    """ORACLE_FOUNDRY_PROJECT_ENDPOINT points at the *project*
    (…/api/projects/<name>), but inference lives on the resource host beneath
    it. Passing the project path as api_base builds a URL Azure does not serve."""
    monkeypatch.setenv("AZURE_AI_API_KEY", "az-test")
    monkeypatch.setenv(
        "ORACLE_FOUNDRY_PROJECT_ENDPOINT",
        "https://neoh238.services.ai.azure.com/api/projects/proj-default",
    )
    provider = llm_gateway._foundry("Kimi-K2.6")

    assert provider is not None
    assert provider.api_base == "https://neoh238.services.ai.azure.com"
    assert "/api/projects" not in provider.api_base
    assert provider.model == "azure/Kimi-K2.6"
    assert provider.kwargs()["api_version"]


def test_foundry_is_absent_without_both_a_key_and_an_endpoint(monkeypatch):
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/api/projects/p")
    monkeypatch.delenv("AZURE_AI_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_FOUNDRY_API_KEY", raising=False)
    assert llm_gateway._foundry("Kimi-K2.6") is None

    monkeypatch.setenv("AZURE_AI_API_KEY", "az-test")
    monkeypatch.delenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    assert llm_gateway._foundry("Kimi-K2.6") is None


def test_foundry_sits_behind_fireworks_in_the_ladder(monkeypatch):
    """Reachable but with nothing deployed, Foundry 404s. Putting it first would
    spend the whole deadline failing over on every call — the same lesson
    _generate_voice_reply recorded as "Fireworks first"."""
    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setenv("AZURE_AI_API_KEY", "az-test")
    monkeypatch.setenv(
        "ORACLE_FOUNDRY_PROJECT_ENDPOINT",
        "https://neoh238.services.ai.azure.com/api/projects/proj-default",
    )
    names = [p.name for p in llm_gateway.providers_for("analysis")]
    assert names.index("fireworks") < names.index("foundry")


# ---------------------------------------------------------------------------
# _generate through the gateway (P1 remainder)
# ---------------------------------------------------------------------------

def test_chat_falls_through_providers_only_while_nothing_is_written(monkeypatch):
    """The ladder is walked in _generate, not in the gateway, because only
    _generate can see `actions`. Once a CRM write has committed, replaying the
    conversation against a second provider applies it twice — so a
    fall-through is legal only before the first write."""
    import ai_chat_agent as agent

    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setenv("AZURE_AI_API_KEY", "az-test")
    monkeypatch.setenv(
        "ORACLE_FOUNDRY_PROJECT_ENDPOINT",
        "https://neoh238.services.ai.azure.com/api/projects/proj-default",
    )
    providers = agent._gateway_chat_providers()
    assert [p.name for p in providers] == ["fireworks", "foundry"], providers
    # local is excluded — it is already the final fallback in _generate, and
    # running it twice through different plumbing would double the work.
    assert "local" not in {p.name for p in providers}


def test_the_gateway_chat_path_is_skipped_without_litellm(monkeypatch):
    """A deployment that has not installed litellm must behave exactly as it
    did before the gateway existed."""
    import importlib.util

    import ai_chat_agent as agent

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name, *a, **k: None if name == "litellm" else real(name, *a, **k),
    )
    assert agent._gateway_chat_providers() == []


def test_the_gateway_chat_path_is_skipped_when_disabled(monkeypatch):
    import ai_chat_agent as agent

    monkeypatch.setenv("ORACLE_LLM_GATEWAY", "0")
    assert agent._gateway_chat_providers() == []


def test_tool_round_returns_the_shape_the_chat_loop_already_speaks(gateway):
    """ai_chat_agent's loop reads choices[0].message.tool_calls. Returning a
    litellm type would make the loop depend on the library it was decoupled
    from."""
    class _Dumpable:
        def model_dump(self):
            return {"choices": [{"message": {"content": "hi", "tool_calls": []}}]}

    gateway(_Dumpable())
    data = asyncio.run(llm_gateway.tool_round([{"role": "user", "content": "x"}], []))

    assert isinstance(data, dict)
    assert data["choices"][0]["message"]["content"] == "hi"


def test_tool_round_still_refuses_to_try_a_second_provider(gateway):
    fake = gateway(RuntimeError("first provider down"))

    with pytest.raises(llm_gateway.LLMUnavailable):
        asyncio.run(llm_gateway.tool_round([{"role": "user", "content": "x"}], []))

    assert len(fake.calls) == 1, "a tool round reached a second provider"


def _fake_provider(name):
    return llm_gateway.Provider(name=name, model=f"{name}/m")


def test_generate_stops_at_the_first_provider_that_wrote_anything(monkeypatch):
    """The property the whole gateway design exists to protect.

    If a provider applies a CRM write and then fails, _generate must return the
    receipts — not try the next provider, which would replay the conversation
    and apply the write a second time.
    """
    import ai_chat_agent as agent

    attempts = []

    async def _loop(ctx, bundle, system_prompt, assistant_id, applied=None, **kwargs):
        attempts.append(kwargs["gateway_provider"].name)
        applied.append({"ok": True, "action_id": "act-1"})   # a write committed
        raise RuntimeError("provider died mid-loop")

    monkeypatch.setattr(agent, "_gateway_chat_providers",
                        lambda: [_fake_provider("fireworks"), _fake_provider("foundry")])
    monkeypatch.setattr(agent, "_local_fallback", _loop)

    async def _prompt(self, agent_id, base):
        return base
    monkeypatch.setattr(agent.SessionManager, "inject_jit_prompt", _prompt)

    text, actions, model = asyncio.run(
        agent._generate(CTX, {"record": None, "messages": [], "attachments": [],
                              "assistant": {}}, "asst-1"))

    assert attempts == ["fireworks"], "a second provider ran after a write applied"
    assert actions and actions[0]["action_id"] == "act-1"
    assert "was applied" in text
    assert model.startswith("fireworks:")


def test_generate_does_fall_through_when_nothing_was_written(monkeypatch):
    """The counterpart: before any write, trying the next provider is free."""
    import ai_chat_agent as agent

    attempts = []

    async def _loop(ctx, bundle, system_prompt, assistant_id, applied=None, **kwargs):
        name = kwargs["gateway_provider"].name
        attempts.append(name)
        if name == "fireworks":
            raise RuntimeError("down, nothing written")
        return "answered", []

    monkeypatch.setattr(agent, "_gateway_chat_providers",
                        lambda: [_fake_provider("fireworks"), _fake_provider("foundry")])
    monkeypatch.setattr(agent, "_local_fallback", _loop)

    async def _prompt(self, agent_id, base):
        return base
    monkeypatch.setattr(agent.SessionManager, "inject_jit_prompt", _prompt)

    text, actions, model = asyncio.run(
        agent._generate(CTX, {"record": None, "messages": [], "attachments": [],
                              "assistant": {}}, "asst-1"))

    assert attempts == ["fireworks", "foundry"]
    assert text == "answered" and actions == []
    assert model.startswith("foundry:")


def test_the_fast_ladder_leads_with_the_lower_tail_not_the_lower_median(monkeypatch):
    """Voice has an 8-second wall clock, so the worst case decides the order.

    Measured over repeated short prompts: foundry median 1,261ms / max 4,153;
    fireworks median 6,013ms / max 36,502. A 36-second tail guarantees a stall
    line, so foundry leads `fast`. `analysis` keeps fireworks first — there it
    is genuinely faster (695ms vs 1,124ms median) and no wall clock applies.
    """
    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setenv("AZURE_AI_API_KEY", "az-test")
    monkeypatch.setenv(
        "ORACLE_FOUNDRY_PROJECT_ENDPOINT",
        "https://neoh-eastus2.services.ai.azure.com/api/projects/proj-default",
    )
    fast = [p.name for p in llm_gateway.providers_for("fast")]
    analysis = [p.name for p in llm_gateway.providers_for("analysis")]

    assert fast.index("foundry") < fast.index("fireworks"), fast
    assert analysis.index("fireworks") < analysis.index("foundry"), analysis


def test_the_deadline_is_enforced_by_the_loop_not_the_provider_sdk(gateway):
    """Measured in production: a voice turn with an 8s budget returned after
    15.7s because litellm did not honour the timeout it was handed. A live
    phone call cannot depend on a provider SDK to hold its wall clock."""
    class _Slow:
        async def acompletion(self, **kwargs):
            await asyncio.sleep(5)          # ignores the timeout it was given
            return _FakeResponse("too late")

        suppress_debug_info = False
        drop_params = False

    slow = _Slow()
    import llm_gateway as g
    original = g._litellm
    g._litellm = lambda: slow
    try:
        started = asyncio.get_event_loop_policy().new_event_loop()
        try:
            import time as _t
            t0 = _t.monotonic()
            with pytest.raises(llm_gateway.LLMUnavailable):
                started.run_until_complete(
                    llm_gateway.complete("x", timeout=0.4))
            elapsed = _t.monotonic() - t0
        finally:
            started.close()
    finally:
        g._litellm = original

    assert elapsed < 3, f"the deadline was not enforced ({elapsed:.1f}s)"
