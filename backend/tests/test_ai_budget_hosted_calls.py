"""Every hosted model call billed to a brokerage outside a Neoh chat turn is
held before it is sent and metered after, under the brokerage's AI token
budget (prompt 18, T18.07, AC18.06; the blocker L18-08 left).

L18-08 set a spend scope in one place, the chat job. Everything else that asks
a hosted model for a brokerage — a phone-call reply, a mission's objective
parse, plan and drafts, client reconciliation, the command parse, comms draft,
CMA, tour, video script, walkthrough extraction, disposition marketing and the
ops copilot — spent with no hold and no meter. With no scope set every gateway
entry point calls straight through, and the synchronous seam
(invoke_bedrock_model) fell back to an unmetered direct path with six retries.

These run without a database. tests/test_ai_budget_live.py proves a non-chat
unit's hold and usage against real PostgreSQL under FORCE RLS.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import re
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import ai_budget
import ai_chat_agent
import llm_gateway
from llm_gateway import SpendRefused, SpendUnavailable
from ml_forge import bedrock_client
from tenancy import Role, TenantContext
from tests.test_ai_budget import _FakeLiteLLM, _FakeResponse, _Scope, _exhausted, _in_scope, _until

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "33333333-3333-3333-3333-333333333333"
AGENT = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT, role=Role.AGENT)
OWNER = TenantContext(agent_id="owner@tenant.test", tenant_id=TENANT, role=Role.BROKER_OWNER)
CLIENT_ID = "44444444-4444-4444-4444-444444444444"
CALLER = "+13025550100"
BACKEND = Path(__file__).resolve().parents[1]


class _Budget:
    """ai_budget's reserve/settle/release, in memory: a real SpendScope runs
    against it. `refuse` names the state the next reservation is refused with."""

    def __init__(self):
        self.refuse = None
        self.keys: list[tuple[str, str, int]] = []
        self.settled: list = []
        self.released: list = []


@pytest.fixture
def budget(monkeypatch):
    ledger = _Budget()

    async def reserve(ctx, call_key, estimate):
        ledger.keys.append((ctx.tenant_id, call_key, estimate))
        if ledger.refuse:
            raise ai_budget.refusal(ledger.refuse, ctx)
        return ai_budget.Hold(call_key=call_key, tokens=estimate, state="ok", held=True)

    async def settle(_ctx, hold, response):
        ledger.settled.append((hold.call_key, response))

    async def release(_ctx, hold):
        ledger.released.append(hold.call_key)

    monkeypatch.setattr(ai_budget, "reserve", reserve)
    monkeypatch.setattr(ai_budget, "settle", settle)
    monkeypatch.setattr(ai_budget, "release_hold", release)
    return ledger


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("ORACLE_FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setenv("ORACLE_AI_BEDROCK_FALLBACK", "1")
    for name in ("AZURE_AI_API_KEY", "ORACLE_FOUNDRY_API_KEY", "ORACLE_FOUNDRY_PROJECT_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_gateway, "counter", llm_gateway._Counter())

    def _install(*replies):
        fake = _FakeLiteLLM(*replies)
        monkeypatch.setattr(llm_gateway, "_litellm", lambda: fake)
        return fake

    return _install


@pytest.fixture
def no_direct_path(monkeypatch):
    """Records any use of the unmetered direct Fireworks/Bedrock path."""
    touched: list[str] = []

    def _record(name):
        def _fn(*_a, **_k):
            touched.append(name)
            raise AssertionError(f"the unmetered direct path was used ({name})")
        return _fn

    monkeypatch.setattr(bedrock_client, "_invoke_fireworks", _record("fireworks"))
    monkeypatch.setattr(bedrock_client, "_get_client", _record("bedrock"))
    monkeypatch.setattr(urllib.request, "urlopen", _record("urlopen"))
    return touched


def _scope_of(seen):
    scope = seen.get("scope")
    assert isinstance(scope, ai_budget.SpendScope), "no spend scope was set for this unit of work"
    return scope


# ── the seam: one scope per unit of work, refusals mapped once ────────────────

def test_a_unit_of_work_gets_its_own_scope_and_a_fresh_key():
    async def run():
        seen = []
        for _ in range(2):
            async with ai_budget.spending(AGENT, "cma") as scope:
                assert llm_gateway.spend_scope.get() is scope
                seen.append(scope)
            assert llm_gateway.spend_scope.get() is None
        with pytest.raises(ValueError):
            async with ai_budget.spending(AGENT, "cma"):
                raise ValueError("the unit failed")
        assert llm_gateway.spend_scope.get() is None, "a failed unit must not leak its scope"
        return seen

    first, second = asyncio.run(run())
    assert isinstance(first, ai_budget.SpendScope) and first.ctx is AGENT
    assert re.fullmatch(r"cma:[0-9a-f]{32}", first.turn_key)
    # A reused key would drop the second call's hold and its usage silently
    # (ON CONFLICT DO NOTHING on both the reservation and the usage row).
    assert first.turn_key != second.turn_key


@pytest.mark.parametrize("state,status_code", [
    ("exhausted", 402), ("insufficient", 402), ("paused", 402),
    ("platform_halted", 503), ("unknown", 503),
])
def test_a_route_refusal_is_402_for_the_brokerage_and_503_for_the_platform(state, status_code):
    refused = ai_budget.refusal(state, AGENT)
    exc = ai_budget.http_refusal(refused)
    assert isinstance(exc, HTTPException) and exc.status_code == status_code
    assert exc.detail == {"code": ai_budget.CODES[state], "message": refused.message, "state": state}
    assert (exc.headers or {}).get("Retry-After") == ("30" if state == "unknown" else None)


def test_a_job_dead_letters_a_refusal_under_its_code_and_retries_an_unreadable_budget():
    refused = ai_budget.refusal("exhausted", AGENT)
    assert refused.job_terminal is True and refused.job_error_code == "AI_BUDGET_EXHAUSTED"
    paused = ai_budget.refusal("paused", AGENT)
    assert paused.job_terminal is True and paused.job_error_code == "AI_SPEND_PAUSED"
    assert ai_budget.refusal("unknown", AGENT).job_terminal is False


# ── the synchronous seam fails closed under a scope ───────────────────────────

def test_the_synchronous_seam_refuses_under_a_scope_before_any_provider(monkeypatch, no_direct_path):
    """asyncio.to_thread copies the scope into the thread. Under it the old
    seam would hold from a foreign event loop and, on any failure, fall to the
    unmetered direct path with six retries."""
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: True)
    gateway_calls = []
    monkeypatch.setattr(bedrock_client, "_via_gateway", lambda *a, **k: gateway_calls.append(a))

    async def run():
        token = llm_gateway.spend_scope.set(_Scope())
        try:
            return await asyncio.to_thread(
                bedrock_client.invoke_bedrock_model, bedrock_client.PRIMARY_MODEL, "prompt", 100)
        finally:
            llm_gateway.spend_scope.reset(token)

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert gateway_calls == [] and no_direct_path == []


def test_complete_sync_refuses_under_a_scope(monkeypatch):
    called = []

    async def complete(*_a, **_k):
        called.append(1)
        return "answer"

    monkeypatch.setattr(llm_gateway, "complete", complete)

    async def run():
        token = llm_gateway.spend_scope.set(_Scope())
        try:
            return await asyncio.to_thread(llm_gateway.complete_sync, "prompt")
        finally:
            llm_gateway.spend_scope.reset(token)

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert called == []


def test_invoke_async_without_a_scope_is_the_old_synchronous_seam(monkeypatch):
    calls = []
    monkeypatch.setattr(bedrock_client, "invoke_bedrock_model",
                        lambda model, prompt, max_tokens=2048: calls.append((model, prompt, max_tokens)) or "ok")
    assert asyncio.run(bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "prompt", 300)) == "ok"
    assert asyncio.run(bedrock_client.invoke_async(
        bedrock_client.SECONDARY_MODEL, "user", 200, system="rules")) == "ok"
    assert calls == [(bedrock_client.PRIMARY_MODEL, "prompt", 300),
                     (bedrock_client.SECONDARY_MODEL, "rules\n\nuser", 200)]


def test_invoke_async_under_a_scope_holds_and_settles_through_the_gateway(monkeypatch, gateway, no_direct_path):
    answer = _FakeResponse("drafted")
    fake = gateway(answer)
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: True)
    scope = _Scope()
    text = _in_scope(scope, bedrock_client.invoke_async(
        bedrock_client.SECONDARY_MODEL, "write it", 600, system="rules"))
    assert text == "drafted"
    assert [lane for lane, _ in scope.before_calls] == ["fireworks"]
    assert scope.settled == [("hold-1", answer)]
    assert fake.calls[0]["messages"][0] == {"role": "system", "content": "rules"}
    assert fake.calls[0]["model"].endswith("glm-5p2-fast"), "SECONDARY maps onto the fast ladder"
    assert no_direct_path == []


def test_a_refused_invoke_async_reaches_no_provider_and_no_direct_path(monkeypatch, gateway, no_direct_path):
    fake = gateway()
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: True)
    with pytest.raises(SpendRefused):
        _in_scope(_Scope(refuse=_exhausted()), bedrock_client.invoke_async(
            bedrock_client.PRIMARY_MODEL, "prompt", 100))
    assert fake.calls == [] and no_direct_path == []


def test_a_gateway_that_cannot_answer_under_a_scope_is_not_followed_by_the_unmetered_path(
        monkeypatch, gateway, no_direct_path):
    gateway(*[RuntimeError("down")] * 4)
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: True)
    scope = _Scope()
    assert _in_scope(scope, bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "p", 100)) is None
    assert no_direct_path == []
    assert len(scope.released) == len(scope.before_calls) >= 1


class _HTTPResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_the_direct_fireworks_path_under_a_scope_is_one_held_attempt_metered_by_its_usage(monkeypatch):
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: False)
    monkeypatch.setattr(bedrock_client, "FIREWORKS_ENABLED", True)
    sent = []
    body = {"choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5}}

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return _HTTPResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    scope = _Scope()
    assert _in_scope(scope, bedrock_client.invoke_async(
        bedrock_client.PRIMARY_MODEL, "prompt", 100, system="rules")) == "hello"
    assert [lane for lane, _ in scope.before_calls] == ["fireworks"]
    assert llm_gateway.token_usage_of(scope.settled[0][1]) == (11, 5)
    assert sent[0]["messages"][0] == {"role": "system", "content": "rules"}


def test_a_throttled_direct_call_under_a_scope_is_not_retried_and_drops_its_hold(monkeypatch):
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: False)
    monkeypatch.setattr(bedrock_client, "FIREWORKS_ENABLED", True)
    monkeypatch.setattr(bedrock_client.time, "sleep", lambda _s: (_ for _ in ()).throw(
        AssertionError("a held call must not sleep and retry")))
    attempts = []

    def urlopen(request, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    scope = _Scope()
    assert _in_scope(scope, bedrock_client.invoke_async(bedrock_client.PRIMARY_MODEL, "p", 100)) is None
    assert attempts == [1]
    assert scope.released == ["hold-1"] and scope.settled == []


def test_the_direct_bedrock_path_under_a_scope_uses_converse_and_is_metered(monkeypatch):
    class _Bedrock:
        def __init__(self):
            self.calls = []

        def converse(self, **kwargs):
            self.calls.append(kwargs)
            return {"output": {"message": {"content": [{"text": "from bedrock"}]}},
                    "usage": {"inputTokens": 7, "outputTokens": 3}}

        def invoke_model(self, **_kwargs):
            raise AssertionError("invoke_model reports no usage the meter reads")

    client = _Bedrock()
    monkeypatch.setattr(bedrock_client, "_gateway_enabled", lambda: False)
    monkeypatch.setattr(bedrock_client, "FIREWORKS_ENABLED", False)
    monkeypatch.setattr(bedrock_client, "_get_client", lambda: client)
    scope = _Scope()
    assert _in_scope(scope, bedrock_client.invoke_async(
        bedrock_client.PRIMARY_MODEL, "prompt", 100, system="rules")) == "from bedrock"
    assert [lane for lane, _ in scope.before_calls] == ["bedrock"]
    assert llm_gateway.token_usage_of(scope.settled[0][1]) == (7, 3)
    assert client.calls[0]["system"] == [{"text": "rules"}]
    assert client.calls[0]["inferenceConfig"]["maxTokens"] == 100


# ── an answered call is metered whatever its caller does next ────────────────

class _SlowSettle(_Scope):
    """A scope whose settle takes a moment, as a database transaction does."""

    def __init__(self):
        super().__init__()
        self.settling = None

    async def after(self, hold, response):
        self.settling.set()
        await asyncio.sleep(0.05)
        self.settled.append((hold, response))


@pytest.mark.parametrize("entry", ["complete", "tool_call", "spend_guarded"])
def test_an_answered_call_is_settled_when_its_callers_deadline_cancels_the_settle(gateway, entry):
    """The provider has billed a call that answered. A caller's own deadline
    (a phone turn's 8 s, the comms draft's 20 s, the request parse's 3 s) that
    fired while the settle ran cancelled it mid-transaction: the usage row
    rolled back and the hold was left to lapse, so billed tokens were never
    counted. The settle finishes whatever happens to the caller."""
    answer = _FakeResponse("answered")
    gateway(answer)

    async def answered():
        return answer

    call = {
        "complete": lambda: llm_gateway.complete("draft this", timeout=8.0),
        "tool_call": lambda: llm_gateway.tool_call([{"role": "user", "content": "x"}], [], timeout=8.0),
        "spend_guarded": lambda: llm_gateway.spend_guarded("fireworks", 10, answered),
    }[entry]
    scope = _SlowSettle()

    async def run():
        scope.settling = asyncio.Event()
        token = llm_gateway.spend_scope.set(scope)
        try:
            task = asyncio.ensure_future(call())
        finally:
            llm_gateway.spend_scope.reset(token)
        await scope.settling.wait()
        task.cancel()                  # the caller's deadline fires mid-settle
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: scope.settled, seconds=2)

    asyncio.run(run())
    assert scope.settled == [("hold-1", answer)] and scope.released == []


# ── phone calls: held for the call's brokerage; a refusal is said, not stalled ─

def _gateway_voice(monkeypatch, complete):
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers",
                        lambda: [llm_gateway.Provider(name="fireworks", model="fireworks_ai/m")])
    monkeypatch.setattr(llm_gateway, "complete", complete)


def test_a_voice_reply_is_spent_in_its_own_scope_for_the_calls_brokerage(monkeypatch):
    seen = {}

    async def complete(_prompt, **_kwargs):
        seen["scope"] = llm_gateway.spend_scope.get()
        return "Happy to help."

    _gateway_voice(monkeypatch, complete)
    reply = asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))
    assert reply == "Happy to help."
    scope = _scope_of(seen)
    assert scope.ctx.tenant_id == TENANT
    assert scope.turn_key.startswith("voice:")
    # The key is the usage idempotency key and is stored: never a phone number.
    assert "5550100" not in scope.turn_key


def test_a_refused_voice_reply_is_raised_not_hidden_behind_the_stall_line(monkeypatch):
    async def complete(_prompt, **_kwargs):
        raise _exhausted()

    _gateway_voice(monkeypatch, complete)
    with pytest.raises(SpendRefused):
        asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))


def test_a_voice_reply_with_no_brokerage_is_refused_before_any_model(monkeypatch):
    called = []

    async def complete(_prompt, **_kwargs):
        called.append(1)
        return "answer"

    _gateway_voice(monkeypatch, complete)
    with pytest.raises(SpendRefused):
        asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=""))
    assert called == []


async def _drained(coro):
    """`coro`'s result, once every settle it left running in the background
    (a phone call's) has finished."""
    result = await coro
    while llm_gateway._METERING:
        await asyncio.gather(*list(llm_gateway._METERING), return_exceptions=True)
        await asyncio.sleep(0)
    return result


def test_a_voice_reply_whose_hold_outlasts_the_live_call_budget_is_refused_on_time(
        monkeypatch, gateway, budget):
    """The hold is a database transaction (a pool acquire of up to 10 s, then
    statements of up to 30 s each), and the gateway's deadline did not count
    it: under pool pressure a caller waited past the point Twilio abandons the
    turn. The whole reply, hold included, is bounded by the live-call budget,
    and a budget that cannot be read in it is an unreadable budget — refused
    (the webhook says so and ends the call), never passed, nothing sent."""
    import time

    fake = gateway()
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers",
                        lambda: [llm_gateway.Provider(name="fireworks", model="fireworks_ai/m")])
    monkeypatch.setattr(ai_chat_agent, "VOICE_REPLY_BUDGET_SECONDS", 0.2)

    async def stuck(ctx, call_key, estimate):
        budget.keys.append((ctx.tenant_id, call_key, estimate))
        await asyncio.sleep(5)

    monkeypatch.setattr(ai_budget, "reserve", stuck)
    started = time.monotonic()
    with pytest.raises(SpendUnavailable) as refused:
        asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))
    assert time.monotonic() - started < 2, "the hold ran past the live-call budget"
    assert refused.value.code == "AI_BUDGET_UNAVAILABLE"
    assert fake.calls == [] and len(budget.keys) == 1


@pytest.mark.parametrize("path", ["gateway", "direct"])
def test_a_voice_reply_is_said_without_waiting_for_its_settle(monkeypatch, gateway, budget, path):
    """After the answer, the settle is a second transaction no deadline
    counted: under pool pressure an answer the model gave at 7.9 s reached
    the caller at ~18 s. A live call's settle runs in the background — the
    reply is said at once — and still records the usage."""
    if path == "gateway":
        gateway(_FakeResponse("At four."))
        monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers",
                            lambda: [llm_gateway.Provider(name="fireworks", model="fireworks_ai/m")])
    else:
        _direct_voice(monkeypatch, fireworks=True)

    async def slow_settle(_ctx, hold, response):
        await asyncio.sleep(0.3)
        budget.settled.append((hold.call_key, response))

    monkeypatch.setattr(ai_budget, "settle", slow_settle)

    async def run():
        reply = await ai_chat_agent._generate_voice_reply(CALLER, "what time is it?", tenant_id=TENANT)
        at_reply = list(budget.settled)
        await _drained(asyncio.sleep(0))
        return reply, at_reply

    reply, at_reply = asyncio.run(run())
    assert reply == ("At four." if path == "gateway" else "fireworks answer")
    assert at_reply == [], "the caller waited for the settle's transaction"
    assert len(budget.settled) == 1 and budget.settled[0][0].startswith("voice:")
    assert budget.released == []


def _direct_voice(monkeypatch, *, fireworks):
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda: [])
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", fireworks)
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_API_KEY", "fw-test")
    sent = []

    async def local_chat(_payload, **_kwargs):
        sent.append("fireworks")
        return {"choices": [{"message": {"content": "fireworks answer"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4}}

    def foundry(_items, _context_type, *_rest):
        sent.append("foundry")
        return SimpleNamespace(output_text="foundry answer",
                               usage={"input_tokens": 6, "output_tokens": 2})

    monkeypatch.setattr(ai_chat_agent, "_local_chat", local_chat)
    monkeypatch.setattr(ai_chat_agent, "_foundry_response", foundry)
    return sent


@pytest.mark.parametrize("fireworks,lane,answer", [
    (True, "fireworks", "fireworks answer"), (False, "foundry", "foundry answer"),
])
def test_the_direct_voice_ladder_holds_each_hosted_tier_before_it_is_sent(
        monkeypatch, budget, fireworks, lane, answer):
    sent = _direct_voice(monkeypatch, fireworks=fireworks)
    budget.refuse = "exhausted"
    with pytest.raises(SpendRefused):
        asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))
    assert sent == [], "a refused tier reached its provider, or the refusal fell through to the next"

    budget.refuse = None
    assert asyncio.run(_drained(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))) \
        == answer
    tenant, key, _estimate = budget.keys[-1]
    assert tenant == TENANT and key.startswith("voice:") and key.endswith(f":{lane}:0")
    assert budget.settled and budget.settled[-1][0] == key


def test_a_refused_fireworks_tier_is_never_a_reason_to_try_foundry(monkeypatch, budget):
    """Each tier's hold is its own estimate: a refusal of the first ("too
    little left for this call") must end the ladder, not hand the turn to a
    cheaper-looking second tier."""
    sent = _direct_voice(monkeypatch, fireworks=True)

    async def reserve(ctx, call_key, estimate):
        budget.keys.append((ctx.tenant_id, call_key, estimate))
        if ":fireworks:" in call_key:
            raise ai_budget.refusal("insufficient", ctx)
        return ai_budget.Hold(call_key=call_key, tokens=estimate, state="ok", held=True)

    monkeypatch.setattr(ai_budget, "reserve", reserve)
    with pytest.raises(SpendRefused) as refused:
        asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))
    assert refused.value.code == "AI_BUDGET_INSUFFICIENT"
    assert sent == [] and [key.rsplit(":", 2)[1] for _, key, _ in budget.keys] == ["fireworks"]


class _FormRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def _twilio_speech(monkeypatch, *, state, reply):
    import commands_api
    import twilio_call_handler
    import voice_tts

    monkeypatch.setattr(commands_api, "_validate_twilio_webhook_signature", lambda *_a, **_k: None)

    async def load(_sid, **_kwargs):
        if isinstance(state, Exception):
            raise state
        return state

    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", load)
    asked, spoken = [], []

    async def generate(_caller_id, _speech, *, tenant_id):
        asked.append(tenant_id)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def play_url(text, **_kwargs):
        spoken.append(text)
        return None

    monkeypatch.setattr(ai_chat_agent, "_generate_voice_reply", generate)
    monkeypatch.setattr(voice_tts, "play_url", play_url)
    response = asyncio.run(commands_api.twilio_speech(_FormRequest({
        "SpeechResult": "what time is the showing", "CallSid": "CA" + "0" * 32, "To": "+13025550199",
    })))
    return response.body.decode(), asked, spoken


def test_a_twilio_turn_is_answered_for_the_brokerage_the_call_belongs_to(monkeypatch):
    twiml, asked, _ = _twilio_speech(monkeypatch, state={"tenant_id": TENANT}, reply="At four.")
    assert asked == [TENANT]
    assert "At four." in twiml and "<Hangup/>" not in twiml


@pytest.mark.parametrize("state,reply", [
    (None, "never asked"),                          # no call state: spend cannot be attributed
    ({"direction": "outbound"}, "never asked"),     # state without a brokerage
    (ConnectionError("redis down"), "never asked"),  # unreadable state is not "no brokerage"
    ({"tenant_id": TENANT}, _exhausted()),          # the brokerage's budget refused
])
def test_a_twilio_turn_that_cannot_be_answered_says_so_and_ends_without_new_spend(monkeypatch, state, reply):
    twiml, asked, spoken = _twilio_speech(monkeypatch, state=state, reply=reply)
    assert ai_chat_agent.VOICE_REFUSED_LINE in twiml and "<Hangup/>" in twiml
    assert ai_chat_agent._VOICE_STALL_LINE not in twiml, "a refusal must not sound like 'please wait'"
    assert spoken == [], "no text-to-speech is bought for the refusal line"
    if not isinstance(reply, Exception):
        assert asked == []


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _custom_call(monkeypatch, *, tenant, reply):
    import commands_api

    async def secret(*_a, **_k):
        return "ok"

    async def call_tenant(call_id):
        assert call_id == "call-1"
        return tenant

    monkeypatch.setattr(commands_api, "_require_webhook_secret", secret)
    monkeypatch.setattr(commands_api, "_call_session_tenant", call_tenant)
    asked = []

    async def generate(caller_id, _speech, *, tenant_id):
        asked.append((caller_id, tenant_id))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ai_chat_agent, "_generate_voice_reply", generate)
    out = asyncio.run(commands_api.custom_call_webhook(_JsonRequest({
        "event": "speech", "call_id": "call-1", "from_number": CALLER, "speech": "hi",
    })))
    return out, asked


def test_a_custom_call_turn_is_answered_for_the_brokerage_its_session_belongs_to(monkeypatch):
    out, asked = _custom_call(monkeypatch, tenant=TENANT, reply="Hello there.")
    assert asked == [(CALLER, TENANT)]
    assert out["text"] == "Hello there." and not out.get("end_call")


@pytest.mark.parametrize("tenant,reply,code", [
    (None, "never asked", "CALL_NOT_ATTRIBUTED"),
    (TENANT, _exhausted(), "AI_BUDGET_EXHAUSTED"),
])
def test_a_custom_call_turn_that_cannot_be_answered_says_so_and_ends(monkeypatch, tenant, reply, code):
    out, asked = _custom_call(monkeypatch, tenant=tenant, reply=reply)
    assert out["text"] == ai_chat_agent.VOICE_REFUSED_LINE
    assert out["end_call"] is True and out["code"] == code
    if tenant is None:
        assert asked == []


def test_an_acs_turn_refused_by_the_budget_says_so_and_ends_the_call(monkeypatch):
    import acs_call_handler

    state = {"tenant_id": TENANT, "caller": CALLER, "turns": 0, "stage": "listening"}

    @asynccontextmanager
    async def lock(_cid):
        yield

    saved, played, asked = [], [], []

    async def load(_cid):
        return dict(state)

    async def save(_cid, value):
        saved.append(dict(value))

    async def play(_cid, text, *, operation_context, state=None):
        played.append((text, operation_context))

    async def generate(caller_id, _speech, *, tenant_id):
        asked.append((caller_id, tenant_id))
        raise _exhausted()

    monkeypatch.setattr(acs_call_handler, "_call_lock", lock)
    monkeypatch.setattr(acs_call_handler, "_load_call_state", load)
    monkeypatch.setattr(acs_call_handler, "_save_call_state", save)
    monkeypatch.setattr(acs_call_handler, "_play_text", play)
    monkeypatch.setattr(ai_chat_agent, "_generate_voice_reply", generate)
    asyncio.run(acs_call_handler.handle_speech_recognized("cid-1", "hello"))
    assert asked == [(CALLER, TENANT)]
    assert played == [(ai_chat_agent.VOICE_REFUSED_LINE, "farewell")]
    assert saved[-1]["stage"] == "ending"


# ── realtime phone calls: admitted before any bridge opens ────────────────────
#
# The media sockets open a hosted realtime model (Qwen Omni, or an ElevenLabs
# agent on Plivo) billed to the call's brokerage for the whole conversation.
# A broker owner's pause, the platform switch and the deploy-time halt stopped
# nothing there: a call for a paused brokerage still reached the model.

_OPEN_CONTROLS = {"cap": None, "warn_pct": 80, "paused": False, "halted": False}
TWILIO_SID = "CA" + "1" * 32
PLIVO_UUID = "8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class _MediaSocket:
    def __init__(self, *, headers=None, query=None, frames=()):
        self.headers = headers or {}
        self.query_params = query or {}
        self.frames = list(frames)
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, *, code):
        self.closed_code = code

    async def receive_text(self):
        return self.frames.pop(0)


@pytest.fixture
def controls(monkeypatch):
    """The brokerage's controls as admission reads them (ai_budget._read):
    `controls["row"]` is the row, an exception (unreadable) or "stall"."""
    box = {"row": dict(_OPEN_CONTROLS), "reads": []}

    async def read(ctx, *, totals=True):
        box["reads"].append(ctx.tenant_id)
        row = box["row"]
        if isinstance(row, BaseException):
            raise row
        if row == "stall":
            await asyncio.sleep(5)
        return row, None

    monkeypatch.setattr(ai_budget, "_read", read)
    monkeypatch.delenv("ORACLE_AI_INFERENCE_HALT", raising=False)
    return box


def _bridge_recorder(opened):
    class _Bridge:
        def __init__(self, *_args, **_kwargs):
            opened.append("constructed")

        async def run(self):
            opened.append("run")

    return _Bridge


def _acs_media(monkeypatch, state, opened):
    import acs_call_handler
    import commands_api
    import qwen_omni_realtime

    socket = _MediaSocket(headers={"authorization": "Bearer signed",
                                   "x-ms-call-connection-id": "acs-call-1"})
    said, stopped = [], []

    async def authorize(_cid):
        return True

    async def load(_cid, **_kwargs):
        if isinstance(state, Exception):
            raise state
        return None if state is None else dict(state)

    @asynccontextmanager
    async def lock(_cid):
        yield

    async def play(_cid, text, *, operation_context, state=None):
        said.append((text, operation_context))

    async def save(_cid, _value):
        return None

    class _Connection:
        def stop_media_streaming(self, **kwargs):
            stopped.append(kwargs)

    monkeypatch.setattr(qwen_omni_realtime, "verify_acs_websocket_jwt", lambda _a: True)
    monkeypatch.setattr(acs_call_handler, "authorize_qwen_media_call", authorize)
    monkeypatch.setattr(acs_call_handler, "_load_call_state", load)
    monkeypatch.setattr(acs_call_handler, "_call_lock", lock)
    monkeypatch.setattr(acs_call_handler, "_play_text", play)
    monkeypatch.setattr(acs_call_handler, "_save_call_state", save)
    monkeypatch.setattr(acs_call_handler, "_get_client",
                        lambda _s: SimpleNamespace(get_call_connection=lambda _c: _Connection()))
    monkeypatch.setattr(qwen_omni_realtime, "QwenOmniRealtimeBridge", _bridge_recorder(opened))
    return commands_api.acs_qwen_media(socket), socket, lambda: said


def _provider_transfers(monkeypatch, provider_class):
    moved = []

    async def transfer_call(self, provider_call_id, *, redirect_url, credentials=None):
        moved.append((provider_call_id, redirect_url))

    monkeypatch.setattr(provider_class, "transfer_call", transfer_call)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.neoh.test")
    return moved


def _twilio_media(monkeypatch, state, opened):
    import commands_api
    import qwen_omni_realtime
    import twilio_call_handler
    import voice_provider

    start = {"event": "start", "start": {"callSid": TWILIO_SID, "accountSid": "AC" + "2" * 32,
                                         "streamSid": "MZ" + "3" * 32,
                                         "customParameters": {"bridge_token": "bound"}}}
    socket = _MediaSocket(headers={"x-twilio-signature": "signed"}, frames=[json.dumps(start)])

    async def authorize(*_args):
        return True

    async def load(_sid, **_kwargs):
        if isinstance(state, Exception):
            raise state
        return None if state is None else dict(state)

    async def mark(_sid):
        return None

    monkeypatch.setattr(twilio_call_handler, "verify_twilio_websocket_signature", lambda _s: True)
    monkeypatch.setattr(twilio_call_handler, "authorize_twilio_media", authorize)
    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", load)
    monkeypatch.setattr(twilio_call_handler, "mark_twilio_streaming", mark)
    monkeypatch.setattr(qwen_omni_realtime, "TwilioQwenRealtimeBridge", _bridge_recorder(opened))
    moved = _provider_transfers(monkeypatch, voice_provider.TwilioVoiceProvider)
    return commands_api.twilio_qwen_media(socket), socket, lambda: moved


def _plivo_media(monkeypatch, state, opened, *, engine="qwen"):
    import commands_api
    import elevenlabs_realtime
    import plivo_call_handler
    import qwen_omni_realtime
    import voice_provider

    start = {"event": "start", "start": {"callId": PLIVO_UUID, "streamId": "stream-1"}}
    socket = _MediaSocket(query={"bridge_token": "bound"}, frames=[json.dumps(start)])

    async def authorize(*_args):
        return True

    async def load(_uuid, **_kwargs):
        if isinstance(state, Exception):
            raise state
        return None if state is None else dict(state)

    async def mark(_uuid):
        return None

    monkeypatch.setattr(plivo_call_handler, "authorize_plivo_media", authorize)
    monkeypatch.setattr(plivo_call_handler, "load_plivo_call_state", load)
    monkeypatch.setattr(plivo_call_handler, "mark_plivo_streaming", mark)
    monkeypatch.setattr(elevenlabs_realtime, "realtime_provider", lambda: engine)
    monkeypatch.setattr(qwen_omni_realtime, "PlivoQwenRealtimeBridge", _bridge_recorder(opened))
    monkeypatch.setattr(elevenlabs_realtime, "PlivoElevenLabsBridge", _bridge_recorder(opened))
    moved = _provider_transfers(monkeypatch, voice_provider.PlivoVoiceProvider)
    return commands_api.plivo_qwen_media(socket), socket, lambda: moved


_MEDIA_ROUTES = {
    "acs": _acs_media,
    "twilio": _twilio_media,
    "plivo": _plivo_media,
    "plivo-elevenlabs": lambda mp, state, opened: _plivo_media(mp, state, opened, engine="elevenlabs"),
}


def _refusal_was_said(route, said):
    """The caller hears VOICE_REFUSED_LINE and the call ends: ACS plays it as
    the farewell (whose PlayCompleted hangs up); Twilio and Plivo move the
    call to the refusal document, which their own speech says before
    <Hangup/> — no model and no text-to-speech is bought for it."""
    import commands_api

    if route == "acs":
        assert said == [(ai_chat_agent.VOICE_REFUSED_LINE, "farewell")]
        return
    provider = route.split("-")[0]
    call_id = TWILIO_SID if provider == "twilio" else PLIVO_UUID
    assert said == [(call_id, f"https://api.neoh.test/api/commands/webhooks/{provider}/refused")]
    markup = asyncio.run(commands_api.voice_refused_markup(provider)).body.decode()
    assert ai_chat_agent.VOICE_REFUSED_LINE.replace("'", "&#x27;") in markup \
        or ai_chat_agent.VOICE_REFUSED_LINE in markup
    assert "<Hangup/>" in markup


@pytest.mark.parametrize("route", sorted(_MEDIA_ROUTES))
@pytest.mark.parametrize("stop,code", [
    ("paused", "AI_SPEND_PAUSED"),
    ("platform_switch", "AI_SPEND_HALTED"),
    ("deploy_halt", "AI_SPEND_HALTED"),
    ("exhausted", "AI_BUDGET_EXHAUSTED"),
    ("unreadable", "AI_BUDGET_UNAVAILABLE"),
    ("no_brokerage", "AI_BUDGET_UNAVAILABLE"),
    ("state_unreadable", "AI_BUDGET_UNAVAILABLE"),
])
def test_a_realtime_call_the_brokerage_cannot_spend_on_never_opens_its_model(
        monkeypatch, controls, caplog, route, stop, code):
    state = {"tenant_id": TENANT, "caller": CALLER, "callee": CALLER, "stage": "greeting"}
    if stop == "paused":
        controls["row"] = {**_OPEN_CONTROLS, "paused": True}
    elif stop == "platform_switch":
        controls["row"] = {**_OPEN_CONTROLS, "halted": True}
    elif stop == "deploy_halt":
        monkeypatch.setenv("ORACLE_AI_INFERENCE_HALT", "1")
    elif stop == "exhausted":
        controls["row"] = {**_OPEN_CONTROLS, "cap": 1000}

        async def read(_ctx, *, totals=True):
            return controls["row"], {"prompt": 900, "completion": 100, "reserved": 0}

        monkeypatch.setattr(ai_budget, "_read", read)
    elif stop == "unreadable":
        controls["row"] = ConnectionError("database down")
    elif stop == "no_brokerage":
        state = {"caller": CALLER, "stage": "greeting"}
    elif stop == "state_unreadable":
        state = ConnectionError("redis down")
    opened: list = []
    caplog.set_level("INFO")
    run, socket, said = _MEDIA_ROUTES[route](monkeypatch, state, opened)
    asyncio.run(run)
    assert opened == [], "a hosted realtime model was opened for a call its brokerage cannot spend on"
    if route == "acs" and stop == "state_unreadable":
        # An ACS call is driven with the credentials its state carries: with
        # none readable nothing can be played, only the socket closed.
        assert said() == []
    else:
        _refusal_was_said(route, said())
    assert socket.closed_code == 1000
    assert f"code={code}" in caplog.text
    assert "5550100" not in caplog.text, "a phone number reached the log"


@pytest.mark.parametrize("route", sorted(_MEDIA_ROUTES))
def test_an_admitted_realtime_call_opens_its_bridge_for_the_calls_brokerage(monkeypatch, controls, route):
    opened: list = []
    run, socket, said = _MEDIA_ROUTES[route](monkeypatch, {"tenant_id": TENANT, "stage": "greeting"}, opened)
    asyncio.run(run)
    assert opened == ["constructed", "run"]
    assert controls["reads"] == [TENANT]
    assert said() == [] and socket.closed_code is None


def test_a_realtime_call_whose_budget_cannot_be_read_in_time_is_refused_not_passed(monkeypatch, controls):
    """The caller is on the line: a read that stalls (a pool acquire, a slow
    statement) is an unreadable budget once its time is up — never a pass."""
    import time

    import commands_api

    monkeypatch.setattr(commands_api, "_REALTIME_ADMISSION_SECONDS", 0.1)
    controls["row"] = "stall"
    opened: list = []
    run, socket, said = _twilio_media(monkeypatch, {"tenant_id": TENANT}, opened)
    started = time.monotonic()
    asyncio.run(run)
    assert time.monotonic() - started < 2
    assert opened == [] and socket.closed_code == 1000
    _refusal_was_said("twilio", said())


def test_a_refused_realtime_call_that_cannot_be_moved_still_ends_without_its_model(monkeypatch, controls):
    """The refusal document needs the public base URL and the provider's
    API; when either fails the bridge is still not opened and the socket
    closes, so the stream markup's own goodbye line ends the call."""
    import voice_provider

    controls["row"] = {**_OPEN_CONTROLS, "paused": True}
    opened: list = []
    run, socket, _said = _plivo_media(monkeypatch, {"tenant_id": TENANT}, opened)

    async def transfer_fails(self, *_a, **_k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(voice_provider.PlivoVoiceProvider, "transfer_call", transfer_fails)
    asyncio.run(run)
    assert opened == [] and socket.closed_code == 1000


def test_the_refusal_document_is_fixed_and_only_for_the_media_providers():
    import commands_api

    with pytest.raises(HTTPException) as missing:
        asyncio.run(commands_api.voice_refused_markup("acs"))
    assert missing.value.status_code == 404
    twiml = asyncio.run(commands_api.voice_refused_markup("twilio")).body.decode()
    plivo = asyncio.run(commands_api.voice_refused_markup("plivo")).body.decode()
    assert "<Say" in twiml and "<Speak>" in plivo and "<Hangup/>" in twiml and "<Hangup/>" in plivo


# ── missions: refused once, journalled once, never "no model could write it" ──

def test_the_planner_asks_once_and_never_retries_a_refusal():
    from missions import planner

    class _Gateway:
        calls = 0

        async def complete(self, *_a, **_k):
            self.calls += 1
            raise _exhausted()

    gw = _Gateway()
    with pytest.raises(SpendRefused):
        asyncio.run(planner._ask(gw, "plan this"))
    assert gw.calls == 1


def test_a_refused_draft_is_a_refusal_not_an_unavailable_draft(monkeypatch):
    from missions import drafter

    calls = []

    async def complete(*_a, **_k):
        calls.append(1)
        raise _exhausted()

    monkeypatch.setattr(llm_gateway, "complete", complete)
    with pytest.raises(SpendRefused):
        asyncio.run(drafter.draft_message(AGENT, channel="sms", recipient_name="Ava",
                                          objective="re-engage", intent="asked about pricing"))
    assert calls == [1]


MISSION = {"id": "55555555-5555-5555-5555-555555555555", "status": "active",
           "objective_text": "re-engage past buyers", "launched_at": None}


class _MissionConn:
    def __init__(self, last_event=None):
        self.last_event = last_event

    async def fetchrow(self, sql, *_args):
        if "FROM mission_events" in sql:
            return self.last_event
        if "FROM missions" in sql:
            return dict(MISSION)
        raise AssertionError(f"unexpected query: {sql[:60]}")


def _tick(monkeypatch, *, pending, due=(), plan=None, stage=None, last_event=None):
    import db.connection
    from missions import executor, planner, policy

    monkeypatch.setattr(executor, "enabled", lambda: True)
    conn = _MissionConn(last_event)

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(db.connection, "tenant_tx", tx)
    journal, states, seen = [], [], {}

    async def _journal(_ctx, _mission_id, kind, detail):
        journal.append((kind, detail))

    async def count(*_a):
        return pending

    async def due_actions(*_a):
        return [dict(a) for a in due]

    async def candidates(*_a):
        return [{"id": "c-1", "subject_id": "s-1"}]

    async def propose(*_a):
        seen["scope"] = llm_gateway.spend_scope.get()
        if isinstance(plan, Exception):
            raise plan
        return [], [], "nothing to do"

    async def context(*_a):
        return {"name": "Ava", "email": "ava@example.test", "phone": None, "state_code": None}

    async def evaluate(*_a, **_k):
        return policy.Verdict(may_stage=True, may_release=False, reason="ok")

    staged = []

    async def _stage(_ctx, _mission, action, _verdict):
        staged.append(action["id"])
        seen["scope"] = llm_gateway.spend_scope.get()
        if isinstance(stage, Exception):
            raise stage
        return {"id": "cmd-1"}, ""

    async def set_state(_ctx, action_id, state, **_kw):
        states.append((action_id, state))

    monkeypatch.setattr(executor, "_journal", _journal)
    monkeypatch.setattr(executor, "_count_planned", count)
    monkeypatch.setattr(executor, "_due_actions", due_actions)
    monkeypatch.setattr(executor, "_refresh_candidates", candidates)
    monkeypatch.setattr(planner, "propose_plan", propose)
    monkeypatch.setattr(executor, "_candidate_context", context)
    monkeypatch.setattr(policy, "evaluate_action", evaluate)
    monkeypatch.setattr(executor, "_stage", _stage)
    monkeypatch.setattr(executor, "_set_state", set_state)
    result = asyncio.run(executor.tick(AGENT, MISSION["id"]))
    return result, journal, states, staged, seen


def test_a_refused_plan_is_journalled_with_its_code_in_the_missions_own_scope(monkeypatch):
    refused = _exhausted()
    result, journal, _states, _staged, seen = _tick(monkeypatch, pending=0, plan=refused)
    assert journal == [("plan_failed", {"code": "AI_BUDGET_EXHAUSTED", "error": refused.message})]
    assert result["planned"] == 0
    scope = _scope_of(seen)
    assert scope.turn_key.startswith("mission:") and scope.ctx.tenant_id == TENANT


def test_a_refused_draft_leaves_the_action_planned_defers_it_and_stops_the_tick(monkeypatch):
    refused = _exhausted()
    due = [{"id": "a-1", "channel": "email", "step_index": 0},
           {"id": "a-2", "channel": "email", "step_index": 1}]
    result, journal, states, staged, seen = _tick(monkeypatch, pending=2, due=due, stage=refused)
    assert states == [], "a refused action is not blocked: it keeps its place for the next tick"
    assert staged == ["a-1"], "the tick stops at the first refusal"
    assert journal == [("action_deferred", {"action_id": "a-1", "code": "AI_BUDGET_EXHAUSTED",
                                            "reason": refused.message})]
    assert result["stopped"] == "AI_BUDGET_EXHAUSTED"
    assert _scope_of(seen).turn_key.startswith("mission:")


def test_a_ten_minute_tick_does_not_repeat_the_same_refusal_in_the_journal(monkeypatch):
    last = {"kind": "plan_failed", "code": "AI_BUDGET_EXHAUSTED"}
    _result, journal, *_ = _tick(monkeypatch, pending=0, plan=_exhausted(), last_event=last)
    assert journal == []
    other = {"kind": "plan_failed", "code": "AI_SPEND_PAUSED"}
    _result, journal, *_ = _tick(monkeypatch, pending=0, plan=_exhausted(), last_event=other)
    assert [kind for kind, _ in journal] == ["plan_failed"]


def test_a_mission_objective_is_parsed_in_its_own_scope_and_a_refusal_is_recorded(monkeypatch):
    import missions_api
    import semantic_turn

    seen = {}

    class _Gateway:
        async def complete(self, *_a, **_k):
            seen["scope"] = llm_gateway.spend_scope.get()
            raise _exhausted()

    monkeypatch.setattr(semantic_turn, "GATEWAY", _Gateway())
    monkeypatch.delenv("ORACLE_SEMANTIC_FRAME_ENABLED", raising=False)
    monkeypatch.setattr(missions_api, "require_feature", lambda *_a: None)

    async def snapshot(_ctx):
        return {}

    inserted = {}

    class _Conn:
        async def fetchrow(self, _sql, *args):
            inserted["constraints"] = json.loads(args[-1])
            return {"id": MISSION["id"]}

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    async def journal(*_a):
        return None

    monkeypatch.setattr(missions_api, "_autonomy_snapshot", snapshot)
    monkeypatch.setattr(missions_api, "tenant_tx", tx)
    monkeypatch.setattr(missions_api, "_journal", journal)
    monkeypatch.setattr(missions_api, "_mission_json", lambda row: dict(row))
    body = missions_api.MissionCreate(objective_kind=missions_api.OBJECTIVES[0],
                                      objective_text="re-engage past buyers but not before Monday")
    out = asyncio.run(missions_api.create_mission(body, AGENT))
    assert out["mission"]["id"] == MISSION["id"], "a parser refusal never blocks the mission's gates"
    frame = inserted["constraints"]["semantic_frame"]
    assert frame["status"] == "unavailable" and frame["reason"] == "AI_BUDGET_EXHAUSTED"
    assert _scope_of(seen).turn_key.startswith("mission_objective:")


# ── client reconciliation: recorded facts only, said so, retried later ────────

FACTS = [{"id": "preference:budget", "text": "Explicit maximum budget is 450000. Wants to buy."}]


def test_a_refused_signal_extraction_is_not_retried_as_a_plain_pass(monkeypatch):
    import client_ai_automation

    calls = []

    async def complete(_prompt, **kwargs):
        calls.append(kwargs.get("response_format") is not None)
        raise _exhausted()

    monkeypatch.setattr(llm_gateway, "complete", complete)
    with pytest.raises(SpendRefused):
        asyncio.run(client_ai_automation._request_model_signals(FACTS, timeout=8))
    assert calls == [True]


def test_a_budget_refusal_degrades_extraction_to_recorded_facts_with_its_code(monkeypatch):
    import client_ai_automation

    async def request(_facts, *, timeout):
        raise _exhausted()

    monkeypatch.setattr(client_ai_automation, "_request_model_signals", request)
    monkeypatch.setenv("ORACLE_CLIENT_AI_MODEL_ENABLED", "1")
    _signals, model_id, code = asyncio.run(client_ai_automation._extract_signals(FACTS))
    assert (model_id, code) == ("deterministic-budget", "AI_BUDGET_EXHAUSTED")


def test_an_unreadable_budget_fails_the_extraction_so_the_job_retries(monkeypatch):
    import client_ai_automation

    async def request(_facts, *, timeout):
        raise SpendUnavailable("unknown", "AI_BUDGET_UNAVAILABLE", "Try again in a moment.")

    monkeypatch.setattr(client_ai_automation, "_request_model_signals", request)
    monkeypatch.setenv("ORACLE_CLIENT_AI_MODEL_ENABLED", "1")
    with pytest.raises(SpendUnavailable):
        asyncio.run(client_ai_automation._extract_signals(FACTS))


def _reconcile_refused(monkeypatch, conn, seen):
    import client_ai_automation

    async def request(_facts, *, timeout):
        seen["scope"] = llm_gateway.spend_scope.get()
        raise _exhausted()

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    monkeypatch.setattr(client_ai_automation, "_request_model_signals", request)
    monkeypatch.setattr(client_ai_automation, "tenant_tx", fake_tx)
    monkeypatch.setenv("ORACLE_CLIENT_AI_MODEL_ENABLED", "1")
    ctx = TenantContext(agent_id="client-ai", tenant_id=conn.TENANT_ID, role=Role.AGENT)
    return asyncio.run(client_ai_automation.reconcile_client(ctx, conn.CLIENT_ID))


def test_a_reconcile_the_budget_stopped_says_so_and_is_retried_not_reserved(monkeypatch):
    from tests.test_property_lookup_status import _ReconcileConn

    conn = _ReconcileConn([])
    conn.client["preferences"] = {"budget_max": 450000}
    seen = {}
    automation = _reconcile_refused(monkeypatch, conn, seen)["automation"]
    scope = _scope_of(seen)
    assert scope.turn_key.startswith("client_ai:") and scope.ctx.tenant_id == conn.TENANT_ID
    assert automation["status"] == "degraded" and automation["last_error_code"] == "AI_BUDGET_EXHAUSTED"
    summary = conn.activities[-1][3]
    assert "completed" not in summary and "AI budget" in summary

    # Same inputs, inside 24 h, not forced: the AI step that did not run is
    # not "unchanged" — it is retried, not re-served until tomorrow.
    seen.clear()
    _reconcile_refused(monkeypatch, conn, seen)
    assert conn.shortcuts == 0 and "scope" in seen


def test_an_unreadable_budget_marks_the_client_failed_with_its_code_and_is_retried(monkeypatch):
    import client_ai_automation

    async def reconcile(ctx, _client_id, force=False):
        raise ai_budget.refusal("unknown", ctx)

    executed = []

    class _Conn:
        async def execute(self, _sql, *args):
            executed.append(args)

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield _Conn()

    class _Reporter:
        async def progress(self, *_a):
            return None

    monkeypatch.setattr(client_ai_automation, "reconcile_client", reconcile)
    monkeypatch.setattr(client_ai_automation, "tenant_tx", fake_tx)
    with pytest.raises(SpendUnavailable) as raised:
        asyncio.run(client_ai_automation.handle_client_reconcile_job(
            {"tenant_id": TENANT, "client_id": CLIENT_ID}, _Reporter()))
    assert raised.value.job_terminal is False
    assert any("AI_BUDGET_UNAVAILABLE" in args for args in executed)


# ── routes: a clear refusal, never a silent template or a second model ────────

def test_a_refused_command_parse_is_a_402_and_nothing_is_staged(monkeypatch):
    import commands_api

    seen = {}

    class _Mind:
        async def generate_command_draft(self, **_kwargs):
            seen["scope"] = llm_gateway.spend_scope.get()
            raise _exhausted()

    async def resolve(*_a):
        return None, None, None

    async def identity(_ctx):
        return {"name": "Ava"}

    async def explode(*_a, **_k):
        raise AssertionError("a refused parse staged a command")

    monkeypatch.setattr(commands_api, "require_feature", lambda *_a: None)
    monkeypatch.setattr(commands_api, "_resolve_command_context", resolve)
    monkeypatch.setattr(commands_api, "load_agent_identity", identity)
    monkeypatch.setattr(commands_api, "_mind_service", _Mind())
    monkeypatch.setattr(commands_api, "stage_command", explode)
    body = commands_api.CommandParseRequest(raw_text="email Ava about the showing tomorrow")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(commands_api.parse_personal_command(body, AGENT))
    assert raised.value.status_code == 402
    assert raised.value.detail["code"] == "AI_BUDGET_EXHAUSTED"
    assert _scope_of(seen).turn_key.startswith("parse:")


def _mind(monkeypatch, provider):
    from agent_mind import mind_service

    service = mind_service.MindService()
    service._session = SimpleNamespace(closed=False)
    service._healthy = False

    async def no_health():
        service._healthy = False

    monkeypatch.setattr(service, "_check_health", no_health)
    monkeypatch.setattr(service, "_bedrock", lambda: provider)
    return service


class _Provider:
    SECONDARY_MODEL = bedrock_client.SECONDARY_MODEL

    def __init__(self, reply):
        self.reply = reply
        self.asked: list = []

    async def invoke_async(self, model_id, prompt, max_tokens=2048, *, system=""):
        self.asked.append(("invoke_async", model_id, max_tokens, bool(system)))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def stream_converse(self, model_id, system_text, user_text, **_kwargs):
        self.asked.append(("stream_converse", model_id))
        yield self.reply


def _draft(service):
    return service.generate_command_draft(raw_text="email Ava", intent="email", profile={"name": "Ava"})


def test_the_command_drafts_hosted_step_is_held_and_metered_not_streamed(monkeypatch):
    from agent_mind import mind_service

    provider = _Provider('{"draft_payload": {"body": "Hi Ava"}, "confidence": 0.9}')
    service = _mind(monkeypatch, provider)
    out = _in_scope(_Scope(), _draft(service))
    assert out["draft_payload"] == {"body": "Hi Ava"}
    assert provider.asked == [("invoke_async", provider.SECONDARY_MODEL, mind_service.COMMAND_MAX_TOKENS, True)]

    provider.reply = _exhausted()
    with pytest.raises(SpendRefused):
        _in_scope(_Scope(), _draft(service))

    # Without a scope nothing changes.
    provider.reply = '{"draft_payload": {"body": "x"}}'
    provider.asked.clear()
    assert asyncio.run(_draft(service))["draft_payload"] == {"body": "x"}
    assert provider.asked == [("stream_converse", provider.SECONDARY_MODEL)]


def _comms(monkeypatch, invoke):
    import client_enterprise

    monkeypatch.setenv("CRM_DRAFT_LLM", "1")
    client = {"id": CLIENT_ID, "full_name": "Ava Buyer", "email": "ava@example.test",
              "client_type": "buyer", "stage": "lead", "company": None}

    class _Conn:
        async def fetchrow(self, sql, *_args):
            return client if "FROM clients" in sql else {"display_name": "Sam", "brokerage": "B",
                                                         "email_signature": "Sam"}

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(client_enterprise, "tenant_tx", tx)
    monkeypatch.setattr(bedrock_client, "invoke_async", invoke)
    body = client_enterprise.DraftRequest(client_id=CLIENT_ID, intent="check in")
    return asyncio.run(client_enterprise.comms_draft(body, AGENT))


def test_a_refused_comms_draft_is_a_402_not_a_silent_template(monkeypatch):
    seen = {}

    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        seen["scope"] = llm_gateway.spend_scope.get()
        raise _exhausted()

    with pytest.raises(HTTPException) as raised:
        _comms(monkeypatch, invoke)
    assert raised.value.status_code == 402 and raised.value.detail["code"] == "AI_BUDGET_EXHAUSTED"
    assert _scope_of(seen).turn_key.startswith("comms_draft:")


def test_an_admitted_comms_draft_is_drafted_through_the_held_seam(monkeypatch):
    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        assert isinstance(llm_gateway.spend_scope.get(), ai_budget.SpendScope)
        return "Subject: Checking in\n\nHi Ava, how is the search going?"

    out = _comms(monkeypatch, invoke)
    assert out == {"subject": "Checking in", "body": "Hi Ava, how is the search going?"}


def _cma(monkeypatch, replies):
    import cma_generator

    asked, seen = [], {}

    async def invoke(model_id, _prompt, _max_tokens=2048, *, system=""):
        asked.append(model_id)
        seen["scope"] = llm_gateway.spend_scope.get()
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(cma_generator, "invoke_async", invoke)
    body = cma_generator.CMARequest(property_address="12 Elm St, Dover", zoning_code="R-1",
                                    square_footage=1800, arv_estimate=350000)
    return asyncio.run(cma_generator.generate_listing_presentation(body, AGENT)), asked, seen


def test_a_refused_cma_is_a_402_and_the_secondary_model_is_never_asked(monkeypatch):
    with pytest.raises(HTTPException) as raised:
        _cma(monkeypatch, [_exhausted()])
    assert raised.value.status_code == 402


def test_a_cma_runs_in_its_own_scope_and_escalates_only_on_no_answer(monkeypatch):
    out, asked, seen = _cma(monkeypatch, [None, "## Retail value"])
    assert out["cma_markdown"] == "## Retail value"
    import cma_generator
    assert asked == [cma_generator.PRIMARY_MODEL, cma_generator.SECONDARY_MODEL]
    assert _scope_of(seen).turn_key.startswith("cma:")
    with pytest.raises(HTTPException) as raised:
        _cma(monkeypatch, [None, None])
    assert raised.value.status_code == 503


def test_a_refused_tour_generation_answers_402_with_the_budget_code(monkeypatch):
    import server

    seen = {}

    async def generate(_body):
        seen["scope"] = llm_gateway.spend_scope.get()
        raise _exhausted()

    class _Req:
        async def json(self):
            return {"address": "12 Elm St"}

    monkeypatch.setattr(server, "generate_tour", generate)
    response = asyncio.run(server.api_generate_tour.__wrapped__(_Req(), ctx=AGENT))
    assert response.status_code == 402
    assert json.loads(response.body)["code"] == "AI_BUDGET_EXHAUSTED"
    assert _scope_of(seen).turn_key.startswith("tour:")


def test_generate_tour_asks_through_the_held_seam_and_stops_at_a_refusal(monkeypatch):
    import tour_generator

    asked = []

    async def invoke(model_id, _prompt, _max_tokens=2048, *, system=""):
        asked.append(model_id)
        raise _exhausted()

    monkeypatch.setattr(tour_generator, "invoke_async", invoke)
    with pytest.raises(SpendRefused):
        asyncio.run(tour_generator.generate_tour({"address": "12 Elm St", "sqft": 1500}))
    assert asked == [tour_generator.PRIMARY_MODEL]


class _ScriptClient:
    def __init__(self):
        self.options: list = []
        self.calls: list = []
        self.responses = self

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text="A bright three-bed home.",
                               usage={"input_tokens": 40, "output_tokens": 30})


def test_a_refused_video_script_is_a_402_and_no_model_is_asked(monkeypatch, budget):
    import video_studio
    import video_studio_api

    client = _ScriptClient()
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setattr(video_studio, "_foundry_responses_client", lambda: client)
    budget.refuse = "exhausted"
    body = video_studio_api.VideoJobScriptRequest(property={"address": "12 Elm St"})
    with pytest.raises(HTTPException) as raised:
        asyncio.run(video_studio_api.draft_script_for_property(body, AGENT))
    assert raised.value.status_code == 402
    assert client.calls == []
    assert budget.keys[0][1].startswith("video_script:") and budget.keys[0][1].endswith(":foundry:0")


def test_an_admitted_video_script_is_bounded_held_and_metered(monkeypatch, budget):
    import video_studio

    client = _ScriptClient()
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setattr(video_studio, "_foundry_responses_client", lambda: client)

    async def run():
        async with ai_budget.spending(AGENT, "video_script"):
            return await video_studio.draft_script_async({"address": "12 Elm St"})

    assert asyncio.run(run()) == "A bright three-bed home."
    assert client.options == [{"timeout": 90.0, "max_retries": 0}]
    assert llm_gateway.token_usage_of(budget.settled[0][1]) == (40, 30)


def test_with_no_script_model_configured_nothing_is_held_and_the_template_stands(monkeypatch, budget):
    """No model would be asked, so a spent budget must not refuse a template."""
    import video_studio

    monkeypatch.delenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    budget.refuse = "exhausted"

    async def run():
        async with ai_budget.spending(AGENT, "video_script"):
            return await video_studio.draft_script_async({"address": "12 Elm St"})

    assert "12 Elm St" in asyncio.run(run())
    assert budget.keys == []


def test_the_video_worker_fails_a_job_whose_script_was_refused(monkeypatch, budget):
    import video_studio

    client = _ScriptClient()
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setattr(video_studio, "_foundry_responses_client", lambda: client)
    budget.refuse = "exhausted"
    statuses = []

    async def set_status(_ctx, _job_id, status, **_fields):
        statuses.append(status)

    monkeypatch.setattr(video_studio, "_set_status", set_status)
    row = {"id": "66666666-6666-6666-6666-666666666666", "kind": "text", "size": "720x1280",
           "source": {"property": {"address": "12 Elm St"}}, "script": "", "tenant_id": TENANT}
    with pytest.raises(SpendRefused):
        asyncio.run(video_studio._process_job(row, "worker-1"))
    assert statuses == ["scripting"], "a refused script is not replaced by a template and rendered"
    assert client.calls == []


def test_the_video_worker_queues_again_a_job_whose_budget_could_not_be_read(monkeypatch, budget):
    """'Unknown' is not a refusal: the worker used to fail the job for good.
    It is queued again (nothing was sent, no quota reserved) and the worker
    backs off before its next claim."""
    import video_studio

    client = _ScriptClient()
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setattr(video_studio, "_foundry_responses_client", lambda: client)
    budget.refuse = "unknown"
    statuses = []

    async def set_status(_ctx, _job_id, status, **fields):
        statuses.append((status, fields))

    row = {"id": "66666666-6666-6666-6666-666666666666", "kind": "text", "size": "720x1280",
           "source": {"property": {"address": "12 Elm St"}}, "script": "", "tenant_id": TENANT}
    claims = []

    async def claim(_worker_id):
        claims.append(1)
        if len(claims) == 1:
            return dict(row)
        raise asyncio.CancelledError()     # stops the loop after one job

    monkeypatch.setattr(video_studio, "_set_status", set_status)
    monkeypatch.setattr(video_studio, "_claim_next_job", claim)
    monkeypatch.setattr(video_studio, "BUDGET_RETRY_SECONDS", 0, raising=False)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(video_studio._worker_loop("vs-test"))
    assert [status for status, _ in statuses] == ["scripting", "queued"]
    # The agent sees why it waits, not a bare 'queued' (cleared when it proceeds).
    assert statuses[1][1]["error"].startswith("AI_BUDGET_UNAVAILABLE")
    assert client.calls == [] and len(claims) == 2


def test_a_video_job_that_waited_on_its_budget_clears_the_code_once_its_script_is_drafted(
        monkeypatch, budget):
    import video_studio

    client = _ScriptClient()
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setattr(video_studio, "_foundry_responses_client", lambda: client)
    statuses = []

    async def set_status(_ctx, _job_id, status, **fields):
        statuses.append((status, fields))

    class _QuotaReached(Exception):
        pass

    def quota_tx(_ctx):
        raise _QuotaReached()

    monkeypatch.setattr(video_studio, "_set_status", set_status)
    monkeypatch.setattr(video_studio, "tenant_tx", quota_tx)
    row = {"id": "66666666-6666-6666-6666-666666666666", "kind": "text", "size": "720x1280",
           "source": {"property": {"address": "12 Elm St"}}, "script": "", "tenant_id": TENANT,
           "error": "AI_BUDGET_UNAVAILABLE: waiting"}
    with pytest.raises(_QuotaReached):      # stopped at the quota gate, after the script
        asyncio.run(video_studio._process_job(row, "worker-1"))
    assert statuses[-1] == ("scripting", {"script": "A bright three-bed home.", "error": None})


def test_a_walkthrough_whose_extraction_was_refused_ends_with_the_code(monkeypatch, tmp_path):
    import voice_intel

    seen, broadcasts, finished = {}, [], []

    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        seen["scope"] = llm_gateway.spend_scope.get()
        raise _exhausted()

    class _Conn:
        async def execute(self, *_a):
            return None

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    async def broadcast(_tenant, frame, **_kwargs):
        broadcasts.append(frame)
        return 1

    async def finish(job_id, status_value, error=None):
        finished.append((job_id, status_value, error))

    monkeypatch.setattr(voice_intel, "invoke_async", invoke)
    monkeypatch.setattr(voice_intel, "_transcribe", lambda _p: "Walked the house, roof needs work.")
    monkeypatch.setattr(voice_intel, "tenant_tx", tx)
    monkeypatch.setattr(voice_intel.sealed_text, "split_json", lambda payload, _f: (payload, "sealed"))
    monkeypatch.setattr(voice_intel.sealed_text, "key_if_needed", lambda *_a: None)
    monkeypatch.setattr(voice_intel.ws_hub, "broadcast_record", broadcast)
    monkeypatch.setattr(voice_intel, "_finish", finish)
    staged = tmp_path / "walk.wav"
    staged.write_bytes(b"audio")
    job = voice_intel.VoiceJob(ctx=AGENT, lead_id="77777777-7777-7777-7777-777777777777",
                               staged_path=staged, original_filename="walk.wav", job_id="job-1")
    asyncio.run(voice_intel._run_job(job))
    assert _scope_of(seen).turn_key.startswith("walkthrough:")
    assert broadcasts[-1]["extraction_code"] == "AI_BUDGET_EXHAUSTED"
    job_id, status_value, error = finished[-1]
    assert status_value == "failed" and error.startswith("AI_BUDGET_EXHAUSTED")


def _walkthrough(monkeypatch, tmp_path, *, attempts):
    """One claimed walkthrough whose AI budget cannot be read, on its
    `attempts`-th claim: what was executed, broadcast and finished."""
    import voice_intel

    executed, broadcasts, finished, slept = [], [], [], []

    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        raise ai_budget.refusal("unknown", AGENT)

    class _Conn:
        async def execute(self, sql, *args):
            executed.append((" ".join(sql.split()), args))

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    async def broadcast(_tenant, frame, **_kwargs):
        broadcasts.append(frame)
        return 1

    async def finish(job_id, status_value, error=None):
        finished.append((job_id, status_value, error))

    real_sleep = asyncio.sleep

    async def sleep(seconds, *args):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(voice_intel, "invoke_async", invoke)
    monkeypatch.setattr(voice_intel, "_transcribe", lambda _p: "Walked the house, roof needs work.")
    monkeypatch.setattr(voice_intel, "tenant_tx", tx)
    monkeypatch.setattr(voice_intel.sealed_text, "split_json", lambda payload, _f: (payload, "sealed"))
    monkeypatch.setattr(voice_intel.sealed_text, "key_if_needed", lambda *_a: None)
    monkeypatch.setattr(voice_intel.ws_hub, "broadcast_record", broadcast)
    monkeypatch.setattr(voice_intel, "_finish", finish)
    monkeypatch.setattr(voice_intel.asyncio, "sleep", sleep)
    staged = tmp_path / "walk.wav"
    staged.write_bytes(b"audio")
    job = voice_intel.VoiceJob(ctx=AGENT, lead_id="77777777-7777-7777-7777-777777777777",
                               staged_path=staged, original_filename="walk.wav",
                               job_id="99999999-9999-9999-9999-999999999999", attempts=attempts)
    asyncio.run(voice_intel._run_job(job))
    return job, executed, broadcasts, finished, slept


def test_a_walkthrough_whose_budget_could_not_be_read_is_queued_again_with_its_audio(monkeypatch, tmp_path):
    """'Unknown' is not a refusal: the job is queued again with its audio,
    and nothing is logged or broadcast until the extraction can run. The
    back-off is the job's (a not-before its claim honours), not this
    worker's: the worker sleeps for nothing and is free for other jobs, and
    a second worker cannot re-claim the job at once and burn its attempts."""
    import voice_intel

    job, executed, broadcasts, finished, slept = _walkthrough(monkeypatch, tmp_path, attempts=1)
    assert finished == [], "an unreadable budget is not a final answer"
    assert broadcasts == [] and not any("interaction_logs" in sql for sql, _ in executed)
    (sql, args), = executed
    assert sql.startswith("UPDATE voice_walkthrough_jobs") and "status = 'queued'" in sql
    assert "failed" not in sql and "audio" not in sql, "requeued with its audio, never failed here"
    assert args[0] == job.job_id
    assert any(isinstance(a, str) and a.startswith("AI_BUDGET_UNAVAILABLE") for a in args)
    assert voice_intel.BUDGET_RETRY_SECONDS not in slept, "the worker itself backed off"


def test_the_claim_waits_out_a_deferred_walkthroughs_back_off():
    import inspect

    import voice_intel

    claim = " ".join(inspect.getsource(voice_intel._claim_next).split())
    # A queued row that carries a reason was deferred: not claimable until
    # BUDGET_RETRY_SECONDS after it was queued again, by any worker.
    assert "error IS NULL OR updated_at <= now() - make_interval(secs => $1)" in claim


def test_a_walkthrough_whose_budget_stays_unreadable_logs_its_transcript_on_the_last_attempt(
        monkeypatch, tmp_path):
    """It used to end 'failed' with its audio released and its transcript
    never logged — the agent's own words dropped. On the last attempt the
    transcript is logged without extraction under the budget's code, as a
    refusal does, and the job ends with that code."""
    import voice_intel

    _job, executed, broadcasts, finished, _slept = _walkthrough(
        monkeypatch, tmp_path, attempts=voice_intel.MAX_ATTEMPTS)
    assert any("INSERT INTO interaction_logs" in sql for sql, _ in executed), "the transcript was dropped"
    logged = next(args for sql, args in executed if "INSERT INTO interaction_logs" in sql)
    payload = json.loads(logged[2])
    assert payload["extraction_code"] == "AI_BUDGET_UNAVAILABLE" and payload["extraction"] is None
    assert broadcasts[-1]["extraction_code"] == "AI_BUDGET_UNAVAILABLE"
    (_id, status_value, error), = finished
    assert status_value == "failed" and error.startswith("AI_BUDGET_UNAVAILABLE")


def test_disposition_marketing_is_spent_for_the_leads_brokerage_and_a_refusal_is_named(monkeypatch):
    import disposition_enforcer

    asked, broadcasts = [], []

    async def invoke(model_id, _prompt, _max_tokens=2048, *, system=""):
        asked.append((model_id, llm_gateway.spend_scope.get()))
        raise _exhausted()

    lead = {"id": "88888888-8888-8888-8888-888888888888", "tenant_id": OTHER_TENANT,
            "days_remaining": 5, "parcel_id": "P-1", "underwriting": "{}",
            "payload": json.dumps({"address": "12 Elm St"}), "seller_client_id": None}

    class _Conn:
        async def fetch(self, sql, *_args):
            return [] if "expire_stale_contracts" in sql else [lead]

        async def execute(self, *_a):
            raise AssertionError("no assets were generated, so none are stored")

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    async def broadcast(_tenant, frame, **_kwargs):
        broadcasts.append(frame)
        return 1

    monkeypatch.setattr(disposition_enforcer, "invoke_async", invoke)
    monkeypatch.setattr(disposition_enforcer, "tenant_tx", tx)
    monkeypatch.setattr(disposition_enforcer.ws_hub, "broadcast_record", broadcast)
    asyncio.run(disposition_enforcer.sweep_once())
    assert len(asked) == 1, "a refusal is never followed by the secondary model"
    scope = asked[0][1]
    assert isinstance(scope, ai_budget.SpendScope)
    assert scope.ctx.tenant_id == OTHER_TENANT and scope.turn_key.startswith("disposition:")
    assert broadcasts[-1]["assets_ready"] is False
    assert broadcasts[-1]["assets_code"] == "AI_BUDGET_EXHAUSTED"


def test_a_refused_ops_copilot_question_is_a_402(monkeypatch):
    import aws_observability

    seen = {}

    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        seen["scope"] = llm_gateway.spend_scope.get()
        raise _exhausted()

    monkeypatch.setattr(aws_observability, "_check_copilot_rate_limit", lambda _ctx: None)
    monkeypatch.setattr(bedrock_client, "invoke_async", invoke)
    body = aws_observability.CopilotRequest(message="why is CPU high?", context={})
    with pytest.raises(HTTPException) as raised:
        asyncio.run(aws_observability.api_copilot_query(body, OWNER))
    assert raised.value.status_code == 402
    assert _scope_of(seen).turn_key.startswith("ops_copilot:")


# ── a caller's deadline that fires inside a hold is an unreadable budget ──────
#
# The hold is a database transaction (a pool acquire of up to 10 s, then its
# statements). A caller's own deadline that fired while it was being read
# looked like the model failing: the comms draft served its template with a
# 200, the request parse recorded a timeout, client reconciliation fell back
# to rules, the direct phone ladder said "one moment" — though no model was
# asked and the budget was never read.

@pytest.fixture
def stalled(monkeypatch, budget):
    """A reservation that never answers in the caller's time."""
    async def stuck(ctx, call_key, estimate):
        budget.keys.append((ctx.tenant_id, call_key, estimate))
        await asyncio.sleep(5)

    monkeypatch.setattr(ai_budget, "reserve", stuck)
    return budget


async def _held_call(sent, *, answer_after=0.0):
    """One hosted call under the current scope: held, then sent."""
    async def provider():
        sent.append(1)
        await asyncio.sleep(answer_after)
        return "answer"

    return await llm_gateway.spend_guarded("fireworks", 50, provider)


def test_a_deadline_that_fires_inside_a_hold_is_an_unreadable_budget(stalled):
    sent = []

    async def run():
        async with ai_budget.spending(AGENT, "cma"):
            return await ai_budget.wait_for(_held_call(sent), 0.1)

    with pytest.raises(SpendUnavailable) as refused:
        asyncio.run(run())
    assert refused.value.code == "AI_BUDGET_UNAVAILABLE" and sent == []
    assert len(stalled.keys) == 1


def test_a_deadline_that_fires_after_the_hold_is_still_the_callers_own_timeout(budget):
    """A provider slow to answer is a timeout, as before — not the budget."""
    sent = []

    async def run():
        async with ai_budget.spending(AGENT, "cma"):
            return await ai_budget.wait_for(_held_call(sent, answer_after=5), 0.1)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
    assert sent == [1] and len(budget.released) == 1


def test_a_comms_draft_whose_hold_outlasts_its_deadline_is_a_503_not_the_template(monkeypatch, stalled):
    import client_enterprise

    monkeypatch.setattr(client_enterprise, "_DRAFT_LLM_SECONDS", 0.1)
    sent = []

    async def invoke(_model_id, _prompt, _max_tokens=2048, *, system=""):
        return await _held_call(sent)

    with pytest.raises(HTTPException) as raised:
        _comms(monkeypatch, invoke)
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "AI_BUDGET_UNAVAILABLE"
    assert raised.value.headers == {"Retry-After": "30"}
    assert sent == []


@pytest.mark.parametrize("answer_after,expected", [
    (None, ("unavailable", "AI_BUDGET_UNAVAILABLE")),   # cut off inside the hold
    (5, ("timed_out", "")),                            # the model was slow: as before
])
def test_a_request_parse_cut_off_inside_its_hold_records_the_budget_code(
        monkeypatch, budget, answer_after, expected):
    from datetime import datetime, timezone

    import semantic_turn

    if answer_after is None:
        async def stuck(ctx, call_key, estimate):
            await asyncio.sleep(5)

        monkeypatch.setattr(ai_budget, "reserve", stuck)
    monkeypatch.setattr(semantic_turn, "FRAME_BUDGET_S", 0.1)
    sent = []

    async def extract(*_args, **_kwargs):
        await _held_call(sent, answer_after=answer_after or 0)

    monkeypatch.setattr(semantic_turn.sf, "extract_frame", extract)

    async def run():
        async with ai_budget.spending(AGENT, "mission_objective"):
            return await semantic_turn._parse(AGENT, "re-engage past buyers", known=(),
                                              now=datetime.now(timezone.utc), lookup=False)

    parsed = asyncio.run(run())
    assert (parsed.status, parsed.reason) == expected
    assert sent == ([] if answer_after is None else [1])


def test_client_reconciliation_cut_off_inside_its_hold_is_retried_not_rules(monkeypatch, stalled):
    import client_ai_automation

    monkeypatch.setattr(client_ai_automation, "_model_timeouts", lambda: (0.05, 0.1))
    monkeypatch.setenv("ORACLE_CLIENT_AI_MODEL_ENABLED", "1")
    sent = []

    async def request(_facts, *, timeout):
        await _held_call(sent)

    monkeypatch.setattr(client_ai_automation, "_request_model_signals", request)

    async def run():
        async with ai_budget.spending(AGENT, "client_ai"):
            return await client_ai_automation._extract_signals(FACTS)

    with pytest.raises(SpendUnavailable) as refused:
        asyncio.run(run())
    assert refused.value.code == "AI_BUDGET_UNAVAILABLE" and sent == []


@pytest.mark.parametrize("fireworks", [True, False])
def test_the_direct_voice_ladder_refuses_a_hold_it_cannot_read_in_the_live_call_budget(
        monkeypatch, stalled, fireworks):
    """The gateway path already raised SpendUnavailable (the webhook's refusal
    line); the direct ladder said the stall line, though no model was asked."""
    sent = _direct_voice(monkeypatch, fireworks=fireworks)
    monkeypatch.setattr(ai_chat_agent, "VOICE_REPLY_BUDGET_SECONDS", 0.2)
    with pytest.raises(SpendUnavailable) as refused:
        asyncio.run(ai_chat_agent._generate_voice_reply(CALLER, "hello?", tenant_id=TENANT))
    assert refused.value.code == "AI_BUDGET_UNAVAILABLE"
    assert sent == [] and len(stalled.keys) == 1


# ── what the owner is told is counted ─────────────────────────────────────────

def test_the_budget_no_longer_says_these_calls_are_not_counted():
    not_counted = " | ".join(ai_budget.NOT_COUNTED).lower()
    for covered in ("phone-call voice replies", "mission drafting", "client automation",
                    "cma", "comms-draft", "command-parse"):
        assert covered not in not_counted, covered
    for gap in ("local-model calls", "reported no usage", "cut off"):
        assert gap in not_counted, gap
    counts = ai_budget.COUNTS.lower()
    for covered in ("phone", "mission", "client", "cma", "draft", "command", "tour", "video",
                    "walkthrough", "disposition"):
        assert covered in counts, covered


def test_the_budget_says_live_phone_conversations_are_model_tokens_it_does_not_count_yet():
    """The Qwen Omni realtime phone bridge is a hosted model its provider bills
    in tokens. The owner was told real-time voice is excluded because it is
    'not model tokens' — false, so the reason live-call spend is missing from
    'used' was wrong. It is a gap, stated as one."""
    live = [item for item in ai_budget.NOT_COUNTED if "real-time" in item.lower()]
    assert live, "live phone conversations on a real-time voice model are not named"
    for item in live:
        assert "not model tokens" not in item.lower()
        assert "tokens" in item.lower() and "not yet held or counted" in item.lower()
        # Admitted, though not metered: a stopped brokerage's call never reaches it.
        assert "not connected to it while your ai is paused" in item.lower()


# ── the inventory: every hosted call site is classified ───────────────────────

#: Each place in backend/ that reaches a hosted model, and why it is (or is
#: not) under a brokerage's budget. A new call site fails this test until it
#: is classified here and, if a brokerage pays for it, put under a scope.
HOSTED_CALL_SITES = {
    # The seams themselves: they hold under the caller's scope.
    "llm_gateway.py::complete": "seam (held per provider under the scope)",
    "llm_gateway.py::stream": "seam (held; nothing under a scope streams)",
    "llm_gateway.py::tool_call": "seam (held under the scope)",
    "ml_forge/bedrock_client.py::_via_gateway": "seam (refuses under a scope)",
    "ml_forge/bedrock_client.py::invoke_bedrock_model": "seam (refuses under a scope)",
    "ml_forge/bedrock_client.py::_invoke_fireworks": "seam: unscoped direct path",
    "ml_forge/bedrock_client.py::_stream_fireworks": "seam: unscoped direct stream",
    "ml_forge/bedrock_client.py::stream_converse": "seam: unscoped stream",
    "ml_forge/bedrock_client.py::invoke_async": "seam (held under the scope)",
    "ml_forge/bedrock_client.py::_fireworks_once": "seam (one held attempt)",
    "ml_forge/bedrock_client.py::_bedrock_converse_once": "seam (one held attempt)",
    # Neoh chat turns (L18-08).
    "ai_chat_agent.py::_foundry_response": "chat / voice scope (spend_guarded_thread)",
    "ai_chat_agent.py::_foundry_generate": "chat scope",
    "ai_chat_agent.py::_converse": "chat scope (spend_guarded_thread)",
    "ai_chat_agent.py::_round": "chat scope (spend_guarded / gateway)",
    # Phone calls (this slice): scope 'voice'.
    "ai_chat_agent.py::_generate_voice_reply": "voice scope",
    "ai_chat_agent.py::_voice_reply_direct": "voice scope",
    # Missions: scope 'mission' (tick) and 'mission_objective' (create).
    "missions/planner.py::_ask": "mission scope",
    "missions/drafter.py::draft_message": "mission scope",
    "semantic_frame.py::extract_frame": "chat / mission_objective scope",
    "semantic_turn.py::_parse": "chat / mission_objective scope",
    # Client automation: scope 'client_ai'.
    "client_ai_automation.py::_request_model_signals": "client_ai scope",
    # Routes and workers billed to a brokerage.
    "agent_mind/mind_service.py::generate_command_draft": "parse scope (unscoped: stream as before)",
    # Nested `_run` of generate_command_draft (unscoped stream, as before) and of
    # _generate_monologue_bedrock (platform: ambient persona monologue).
    "agent_mind/mind_service.py::_run": "parse: unscoped stream only / platform monologue",
    "client_enterprise.py::_llm_draft": "comms_draft scope",
    "cma_generator.py::generate_listing_presentation": "cma scope",
    "tour_generator.py::generate_tour": "tour scope",
    "video_studio.py::_script_response": "video_script scope (spend_guarded_thread)",
    "video_studio.py::draft_script": "video_script: refuses under a scope",
    "video_studio.py::draft_script_async": "video_script scope",
    "video_studio.py::_process_job": "video_script scope",
    "video_studio_api.py::_scripted": "video_script scope",
    "voice_intel.py::_extract": "walkthrough scope",
    "disposition_enforcer.py::_generate_marketing": "disposition scope",
    "aws_observability.py::api_copilot_query": "ops_copilot scope",
    # Tenant-billed, admitted but NOT metered (a stated limit,
    # ai_budget.NOT_COUNTED): a brokerage's live phone conversation streamed
    # through a real-time model session. The media route admits the call
    # before the bridge opens (commands_api._realtime_refusal); there is no
    # per-call hold and no usage read.
    "qwen_omni_realtime.py::run":
        "tenant-billed, admitted, not metered (stated limit): Qwen Omni realtime phone "
        "conversation, billed by its provider in tokens; response.done usage is not read",
    "elevenlabs_realtime.py::__init__":
        "tenant-billed, admitted, not metered (stated limit): ElevenLabs agent phone "
        "conversation (Plivo)",
    # Platform-level: no brokerage pays for these (out of scope, stated).
    "agent_mind/mind_service.py::_stream_monologue_bedrock":
        "platform: ambient persona monologue, no tenant and no caller in backend/",
    "scraper_supervisor.py::_request_model_plan": "platform: public-dataset scraper planning, no tenant",
    "ml_forge/synthetic_lawyer.py::generate_record": "platform: training-data synthesis, no tenant",
    "semantic_eval.py::run_case": "platform: offline parser evaluation (scripts/, --approve-spend), no tenant",
}

_HOSTED_NAMES = {"invoke_bedrock_model", "invoke_async", "stream_converse", "complete_sync",
                 "acompletion", "converse", "converse_stream", "invoke_model", "tool_round",
                 "_foundry_response", "_script_response", "extract_frame", "_fireworks_once",
                 "_bedrock_converse_once", "draft_script", "draft_script_async"}
_GATEWAY_NAMES = {"llm_gateway", "gateway", "GATEWAY"}


def _hosted_sites() -> set[str]:
    found: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "/tests/" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                name = None
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    name = child.id if child.id in _HOSTED_NAMES else None
                elif isinstance(child, ast.Attribute):
                    if child.attr in _HOSTED_NAMES:
                        name = child.attr
                    elif (child.attr in {"complete", "stream"} and isinstance(child.value, ast.Name)
                          and child.value.id in _GATEWAY_NAMES):
                        name = child.attr
                    elif child.attr == "create" and isinstance(child.value, ast.Attribute) \
                            and child.value.attr == "responses":
                        name = "responses.create"
                    elif child.attr == "urlopen" and rel == "ml_forge/bedrock_client.py":
                        name = "urlopen"
                    elif (child.attr == "connect" and isinstance(child.value, ast.Name)
                          and child.value.id == "websockets"):
                        # A real-time model session (a live phone conversation).
                        name = "websockets.connect"
                if name in _HOSTED_NAMES or name in {"complete", "stream", "responses.create", "urlopen",
                                                     "websockets.connect"}:
                    if owner != "<module>":
                        found.add(f"{rel}::{owner}")
                visit(child, owner)

        visit(tree, "<module>")
    return found


def test_every_hosted_model_call_site_is_classified():
    found = _hosted_sites()
    unclassified = sorted(found - set(HOSTED_CALL_SITES))
    assert not unclassified, f"hosted model calls with no budget classification: {unclassified}"
    stale = sorted(set(HOSTED_CALL_SITES) - found)
    assert not stale, f"classified call sites that no longer exist: {stale}"
