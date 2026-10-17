"""A brokerage's hosted-model token budget stops a Neoh chat turn BEFORE the
spend, never after (prompt 18, T18.07, AC18.05, AC18.06, F18.05, F18.06;
prompt 07, T07.07).

Before 0170 a chat turn could make up to seven hosted model calls with no
per-brokerage ceiling, and the deployed gateway path did not meter them.
These run without a database: the gateway hook, the chat ladder, the job's
refusal, the socket's admission, the route gates and the migration's text.
tests/test_ai_budget_live.py proves reserve/settle, the lock, the warning, the
hard stop, both kill switches and the role guard against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai_budget
import ai_chat_agent
import llm_gateway
from llm_gateway import SpendRefused, SpendUnavailable
from memory_core.session_manager import SessionManager, _profile_to_dict
from tenancy import Role, TenantContext, require_context

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT, role=Role.AGENT)
OWNER = TenantContext(agent_id="owner@tenant.test", tenant_id=TENANT, role=Role.BROKER_OWNER)
ADMIN = TenantContext(agent_id="admin@platform.test", tenant_id=TENANT, role=Role.PLATFORM_ADMIN)
MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0170_ai_token_budget.sql"


def _exhausted() -> SpendRefused:
    return SpendRefused("exhausted", "AI_BUDGET_EXHAUSTED", "The monthly AI budget is used up.")


class _Scope:
    """Records what the gateway asks of a spend scope; refuses on demand."""

    def __init__(self, refuse: Exception | None = None):
        self.refuse = refuse
        self.before_calls: list[tuple[str, int]] = []
        self.settled: list = []
        self.released: list = []

    async def before(self, lane, estimate):
        self.before_calls.append((lane, estimate))
        if self.refuse is not None:
            raise self.refuse
        return f"hold-{len(self.before_calls)}"

    async def after(self, hold, response):
        self.settled.append((hold, response))

    async def release(self, hold):
        self.released.append(hold)


def _in_scope(scope, coro):
    async def run():
        token = llm_gateway.spend_scope.set(scope)
        try:
            return await coro
        finally:
            llm_gateway.spend_scope.reset(token)
    return asyncio.run(run())


class _FakeResponse:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class _FakeLiteLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

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
    for name in ("AZURE_AI_API_KEY", "ORACLE_FOUNDRY_API_KEY", "ORACLE_FOUNDRY_PROJECT_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_gateway, "counter", llm_gateway._Counter())

    def _install(*replies):
        fake = _FakeLiteLLM(*replies)
        monkeypatch.setattr(llm_gateway, "_litellm", lambda: fake)
        return fake

    return _install


# ── the gateway hook: refused before anything is sent ─────────────────────────

def test_a_refused_tool_round_never_reaches_a_provider(gateway):
    fake = gateway()
    scope = _Scope(refuse=_exhausted())
    with pytest.raises(SpendRefused) as refused:
        _in_scope(scope, llm_gateway.tool_call(
            [{"role": "user", "content": "update the client"}],
            [{"type": "function", "function": {"name": "update_client"}}]))
    assert refused.value.code == "AI_BUDGET_EXHAUSTED"
    assert fake.calls == [], "the budget must refuse BEFORE the spend"


def test_a_refused_completion_does_not_fall_through_to_the_next_provider(gateway):
    """SpendRefused subclasses LLMUnavailable, which complete() re-raises: a
    refusal is not one provider's outage, so no second provider is tried."""
    fake = gateway()
    scope = _Scope(refuse=_exhausted())
    with pytest.raises(SpendRefused):
        _in_scope(scope, llm_gateway.complete("summarise this"))
    assert fake.calls == []
    assert len(scope.before_calls) == 1


def test_an_admitted_call_is_settled_with_its_response_and_a_failed_one_released(gateway):
    answer = _FakeResponse("second answered")
    fake = gateway(RuntimeError("first provider down"), answer)
    scope = _Scope()
    assert _in_scope(scope, llm_gateway.complete("summarise this")) == "second answered"
    assert [lane for lane, _ in scope.before_calls] == ["fireworks", "bedrock"]
    assert scope.released == ["hold-1"]                 # the failed call holds nothing
    assert scope.settled == [("hold-2", answer)]        # usage comes from the response
    assert len(fake.calls) == 2
    # The estimate covers the completion ceiling the gateway actually sends.
    assert all(estimate >= llm_gateway.MIN_TOKENS for _, estimate in scope.before_calls)


def test_a_tool_round_is_settled_and_a_provider_error_releases_its_hold(gateway):
    gateway(RuntimeError("boom"))
    scope = _Scope()
    with pytest.raises(llm_gateway.LLMUnavailable):
        _in_scope(scope, llm_gateway.tool_call([{"role": "user", "content": "x"}], []))
    assert scope.released == ["hold-1"] and scope.settled == []


def test_the_local_model_is_not_hosted_spend(gateway, monkeypatch):
    monkeypatch.delenv("ORACLE_FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.setenv("ORACLE_AI_BEDROCK_FALLBACK", "0")
    monkeypatch.setenv("ORACLE_AI_CHAT_PROVIDER", "azure-foundry")
    fake = gateway(_FakeResponse("local answer"))
    scope = _Scope(refuse=_exhausted())
    assert _in_scope(scope, llm_gateway.complete("x")) == "local answer"
    assert scope.before_calls == [] and len(fake.calls) == 1


def test_without_a_scope_the_gateway_behaves_as_before(gateway):
    fake = gateway(_FakeResponse("plain"))
    assert llm_gateway.spend_scope.get() is None
    assert asyncio.run(llm_gateway.complete("x")) == "plain"
    assert len(fake.calls) == 1


def test_bedrock_usage_is_read_so_its_tier_meters_instead_of_recording_zero():
    response = {"usage": {"inputTokens": 900, "outputTokens": 120, "totalTokens": 1020}}
    assert llm_gateway.token_usage_of(response) == (900, 120)


def test_the_estimate_counts_the_prompt_and_the_completion_ceiling():
    messages = [{"role": "user", "content": "x" * 3000}]
    estimate = llm_gateway.estimate_tokens(messages, completion=4000)
    assert 4000 + 1000 <= estimate < 4000 + 1100


def test_a_binary_payload_is_held_at_the_attachment_allowance_not_its_encoded_size():
    """A provider prices an image by its pixels, not its bytes: a 3 MB photo
    held at len(json)/3 was over a million tokens and refused a turn as
    'used up' that would have cost a few thousand."""
    photo = bytes(3 * 1024 * 1024)
    plain = llm_gateway.estimate_tokens([{"text": "the kitchen"}], completion=0)
    raw = llm_gateway.estimate_tokens([{"text": "the kitchen"}, {"image": {"source": {"bytes": photo}}}],
                                      completion=0)
    url = "data:image/jpeg;base64," + "A" * 4_000_000
    encoded = llm_gateway.estimate_tokens([{"text": "the kitchen"}, {"image_url": url}], completion=0)
    for held in (raw, encoded):
        assert llm_gateway.ATTACHMENT_TOKENS <= held - plain <= llm_gateway.ATTACHMENT_TOKENS + 20


async def _never_cut(*_a, **_k):
    await asyncio.sleep(5)


@pytest.mark.parametrize("entry", ["complete", "tool_call", "spend_guarded"])
def test_a_call_its_caller_cuts_off_releases_its_hold(gateway, entry):
    """The request parser waits 3 s for an 8 s gateway call: the cut-off call
    used to keep its hold for ten minutes (phantom 'used' at admission) and was
    never metered. Released now, like a call that timed out in the gateway, and
    named in what the budget does not count."""
    fake = gateway()
    fake.acompletion = _never_cut
    call = {
        "complete": lambda: llm_gateway.complete("parse this", timeout=8.0),
        "tool_call": lambda: llm_gateway.tool_call([{"role": "user", "content": "x"}], [], timeout=8.0),
        "spend_guarded": lambda: llm_gateway.spend_guarded("foundry", 10, _never_cut),
    }[entry]
    scope = _Scope()
    with pytest.raises(asyncio.TimeoutError):
        _in_scope(scope, asyncio.wait_for(call(), timeout=0.05))
    assert len(scope.before_calls) == 1
    assert scope.released == ["hold-1"] and scope.settled == []
    assert any("cut off" in item for item in ai_budget.NOT_COUNTED)


# ── spend_guarded: the direct tiers' hold, settle and release ────────────────

def test_spend_guarded_refuses_before_the_call_settles_after_and_releases_on_failure():
    ran: list[str] = []

    async def call():
        ran.append("sent")
        return {"usage": {"inputTokens": 10, "outputTokens": 2}}

    async def failing():
        ran.append("sent")
        raise RuntimeError("provider down")

    refusing = _Scope(refuse=_exhausted())
    with pytest.raises(SpendRefused):
        _in_scope(refusing, llm_gateway.spend_guarded("foundry", 50, call))
    assert ran == [], "a refused call is never sent"

    admitted = _Scope()
    response = _in_scope(admitted, llm_gateway.spend_guarded("foundry", 50, call))
    assert admitted.before_calls == [("foundry", 50)]
    assert admitted.settled == [("hold-1", response)] and admitted.released == []

    failed = _Scope()
    with pytest.raises(RuntimeError):
        _in_scope(failed, llm_gateway.spend_guarded("bedrock", 50, failing))
    assert failed.released == ["hold-1"] and failed.settled == []


# ── the chat ladder: a refusal ends the turn, it is never a fallback ──────────

class _Memory(SessionManager):
    """The operator-memory block _generate always appends, as a new operator
    gets it (no profile row), with the operator id left empty as the floor
    leaves it. Stubbing memory out entirely hid that every real turn carries
    this block: the floor was 58 tokens short of the smallest real turn."""

    async def get_user_context(self, _user_id):
        return {"user_id": "", "tenant_id": self.ctx.tenant_id, "found": False,
                "parameters": _profile_to_dict(None), "recent_interactions": []}


def _quiet_turn(monkeypatch):
    """What _generate reads besides its tiers, stubbed: no brokerage, channel
    or request-parsing lookups, and a new operator's memory with no database."""
    import frame_constraints
    import tool_offer

    async def brokerage(_ctx):
        return None

    async def gaps(_ctx):
        return {}

    monkeypatch.setenv(frame_constraints.ENV_FLAG, "off")
    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", _Memory)
    monkeypatch.setattr(tool_offer, "channel_gaps", gaps)


def _ladder(monkeypatch, refuse_with_receipt=False):
    seen = {"fireworks": 0, "foundry": 0, "bedrock": 0, "local": 0}

    async def local_fallback(_ctx, _bundle, _prompt, _aid="", applied=None, **kw):
        if kw.get("gateway_provider") is None:
            seen["local"] += 1
            return "local", []
        if refuse_with_receipt:
            applied.append({"ok": True, "action_id": "act-1", "status": "applied"})
        raise _exhausted()

    async def fireworks(*_a, **_k):
        seen["fireworks"] += 1
        return "fw", [], "fireworks"

    async def foundry(*_a, **_k):
        seen["foundry"] += 1
        return "foundry", [], "foundry"

    def converse(*_a, **_k):
        seen["bedrock"] += 1
        return {"output": {"message": {"content": [{"text": "bedrock"}]}}}

    provider = llm_gateway.Provider(name="fireworks", model="fireworks_ai/test")
    _quiet_turn(monkeypatch)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *a, **k: [provider])
    monkeypatch.setattr(ai_chat_agent, "_local_fallback", local_fallback)
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", True)
    monkeypatch.setattr(ai_chat_agent, "_fireworks_generate", fireworks)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "azure-foundry")
    monkeypatch.setattr(ai_chat_agent, "_foundry_generate", foundry)
    monkeypatch.setattr(ai_chat_agent, "_converse", converse)
    return seen


BUNDLE = {"messages": [{"role": "user", "content": "hi"}], "record": None, "attachments": [],
          "assistant": {"context_type": "client", "context_id": "c-1"}}


def test_a_budget_refusal_on_the_first_tier_runs_no_other_tier(monkeypatch):
    seen = _ladder(monkeypatch)
    with pytest.raises(SpendRefused) as refused:
        asyncio.run(ai_chat_agent._generate(AGENT, dict(BUNDLE), "assistant-1"))
    assert refused.value.code == "AI_BUDGET_EXHAUSTED"
    assert seen == {"fireworks": 0, "foundry": 0, "bedrock": 0, "local": 0}


def test_a_mid_turn_refusal_keeps_the_receipts_of_writes_already_applied(monkeypatch):
    _ladder(monkeypatch, refuse_with_receipt=True)
    with pytest.raises(SpendRefused) as refused:
        asyncio.run(ai_chat_agent._generate(AGENT, dict(BUNDLE), "assistant-2"))
    assert [r["action_id"] for r in refused.value.applied] == ["act-1"]


def test_every_hosted_model_call_in_the_chat_agent_is_spend_guarded():
    """A hosted call outside spend_guarded would be neither checked nor metered.
    Pinned structurally: every Foundry and Bedrock call runs in a worker
    thread through spend_guarded_thread (held until the thread returns), and
    the direct hosted HTTP round goes through spend_guarded."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ai_chat_agent))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}

    def calls(fn):
        return [ast.unparse(n) for n in ast.walk(functions[fn]) if isinstance(n, ast.Call)]

    foundry = calls("_foundry_generate")
    assert not any(c.startswith(("asyncio.to_thread(", "spend_guarded(")) for c in foundry)
    assert sum(c.startswith("spend_guarded_thread(") and "_foundry_response" in c for c in foundry) == 2
    assert not any("record_inference" in c for c in foundry), "usage is recorded by the scope's settle"
    bedrock = calls("_bedrock_round")
    assert any(c.startswith("spend_guarded_thread(") and "_converse" in c for c in bedrock)
    assert all("_bedrock_round" in c or "_converse" not in c for c in calls("_generate"))
    local = calls("_local_fallback")
    assert any(c.startswith("spend_guarded(") and "_local_chat" in c for c in local)


# ── the direct tiers, by behaviour: held, metered, and stopped before sending ─

class _FoundryClient:
    """The Foundry OpenAI client: `responses.create` answers, and with_options
    records the per-request options it is given (and returns the client)."""

    def __init__(self, create):
        self.responses = type("Responses", (), {"create": staticmethod(create)})()
        self.options: list[dict] = []

    def with_options(self, **options):
        self.options.append(options)
        return self


class _Transports:
    """Every transport the chat tiers send through, counted. The gateway tier
    fails with a plain outage (not a refusal), so the ladder walks on —
    unless `gateway_down` is False, and llm_gateway's own round runs."""

    def __init__(self, monkeypatch, gateway_down=True):
        self.sent: list[str] = []
        transports = self

        async def gateway_round(*_a, **_k):
            transports.sent.append("gateway")
            raise RuntimeError("gateway provider down")

        async def http_chat(_payload, url="", api_key="", timeout=0.0):
            transports.sent.append("fireworks" if api_key else "local")
            return {"choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 40}}

        def converse(*_a, **_k):
            transports.sent.append("bedrock")
            return {"output": {"message": {"content": [{"text": "answer"}]}},
                    "usage": {"inputTokens": 900, "outputTokens": 40}}

        def foundry_create(**_k):
            transports.sent.append("foundry")
            return type("R", (), {"output": [], "output_text": "answer"})()

        client = _FoundryClient(foundry_create)
        if gateway_down:
            monkeypatch.setattr(llm_gateway, "tool_round", gateway_round)
        monkeypatch.setattr(ai_chat_agent, "_local_chat", http_chat)
        monkeypatch.setattr(ai_chat_agent, "_converse", converse)
        monkeypatch.setattr(ai_chat_agent, "_foundry_openai_client", lambda: client)
        monkeypatch.setattr(ai_chat_agent, "FIREWORKS_API_KEY", "fw-test")


#: Each direct tier as the only hosted tier configured.
ONLY = {
    "foundry": {"FIREWORKS_ENABLED": False, "AI_PROVIDER": "azure-foundry", "BEDROCK_FALLBACK_ENABLED": False},
    "fireworks": {"FIREWORKS_ENABLED": True, "AI_PROVIDER": "fireworks", "BEDROCK_FALLBACK_ENABLED": False},
    "bedrock": {"FIREWORKS_ENABLED": False, "AI_PROVIDER": "bedrock", "BEDROCK_FALLBACK_ENABLED": True},
    "none": {"FIREWORKS_ENABLED": False, "AI_PROVIDER": "local", "BEDROCK_FALLBACK_ENABLED": False},
}


def _only(monkeypatch, name):
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *a, **k: [])
    for flag, value in ONLY[name].items():
        monkeypatch.setattr(ai_chat_agent, flag, value)


def _tier(name, bundle=None):
    """One tier's first call, as _generate makes it (the real persona, the
    tools the bundle's offer leaves)."""
    import neoh_persona

    bundle = dict(bundle or BUNDLE)
    if name == "foundry":
        return ai_chat_agent._foundry_generate(AGENT, bundle, "assistant-t", "", applied=[])
    persona = neoh_persona.build_system_prompt()
    if name == "fireworks":
        return ai_chat_agent._fireworks_generate(AGENT, bundle, persona, "assistant-t", [])
    if name == "gateway":
        (provider,) = ai_chat_agent._gateway_chat_providers()
        return ai_chat_agent._local_fallback(
            AGENT, bundle, persona, "assistant-t", applied=[], model=provider.model,
            max_tokens=ai_chat_agent.FIREWORKS_MAX_TOKENS, disable_thinking=False,
            gateway_provider=provider, max_rounds=ai_chat_agent._HOSTED_TOOL_ROUNDS)
    tool_config = ai_chat_agent._offered_tool_config(bundle["assistant"].get("context_type"),
                                                     ai_chat_agent._withheld(bundle))
    return ai_chat_agent._bedrock_round(ai_chat_agent._bedrock_messages(bundle), persona, tool_config)


@pytest.mark.parametrize("name", ["foundry", "fireworks", "bedrock"])
def test_a_refused_direct_tier_never_reaches_its_provider(monkeypatch, name):
    transports = _Transports(monkeypatch)
    _only(monkeypatch, name)
    scope = _Scope(refuse=_exhausted())
    with pytest.raises(SpendRefused):
        _in_scope(scope, _tier(name))
    assert [lane for lane, _ in scope.before_calls] == [name]
    assert transports.sent == [], "the budget must refuse BEFORE the spend"


@pytest.mark.parametrize("name", ["foundry", "fireworks", "bedrock"])
def test_an_admitted_direct_tier_is_held_then_metered_with_its_response(monkeypatch, name):
    transports = _Transports(monkeypatch)
    _only(monkeypatch, name)
    scope = _Scope()
    _in_scope(scope, _tier(name))
    assert transports.sent == [name]
    assert [lane for lane, _ in scope.before_calls] == [name]
    assert len(scope.settled) == 1 and scope.settled[0][0] == "hold-1"
    assert scope.released == []
    # Admission's floor is a lower bound: a real first call holds at least it
    # (no request parse here, so none in the floor either).
    import frame_constraints

    monkeypatch.setenv(frame_constraints.ENV_FLAG, "off")
    floor = ai_chat_agent.smallest_request_hold(words=True, files=False)
    assert 0 < floor <= scope.before_calls[0][1]


def test_with_no_hosted_tier_no_request_needs_budget(monkeypatch):
    _only(monkeypatch, "none")
    assert ai_chat_agent.smallest_request_hold() == 0


@pytest.mark.parametrize("refused,config", [
    ("fireworks", {"FIREWORKS_ENABLED": True, "AI_PROVIDER": "azure-foundry", "BEDROCK_FALLBACK_ENABLED": True}),
    ("foundry", {"FIREWORKS_ENABLED": False, "AI_PROVIDER": "azure-foundry", "BEDROCK_FALLBACK_ENABLED": True}),
    ("bedrock", {"FIREWORKS_ENABLED": False, "AI_PROVIDER": "bedrock", "BEDROCK_FALLBACK_ENABLED": True}),
])
def test_a_refusal_on_a_later_tier_ends_the_turn_and_runs_no_other_tier(monkeypatch, refused, config):
    """The gateway is down (an outage, so the ladder walks on); the next tier's
    hold is refused. The refusal ends the turn with its own code: no later
    tier, and never the local fallback answering as if nothing happened."""
    _quiet_turn(monkeypatch)
    transports = _Transports(monkeypatch)
    provider = llm_gateway.Provider(name="fireworks", model="fireworks_ai/test")
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *a, **k: [provider])
    for name, value in config.items():
        monkeypatch.setattr(ai_chat_agent, name, value)
    scope = _Scope(refuse=_exhausted())
    with pytest.raises(SpendRefused) as stopped:
        _in_scope(scope, ai_chat_agent._generate(AGENT, dict(BUNDLE), f"assistant-{refused}"))
    assert stopped.value.code == "AI_BUDGET_EXHAUSTED"
    assert [lane for lane, _ in scope.before_calls] == [refused]
    assert transports.sent == ["gateway"]


PHOTO = {"media_type": "image/jpeg", "data": bytes(3 * 1024 * 1024), "filename": "kitchen.jpg"}
SCAN = {"media_type": "application/pdf", "data": bytes(2 * 1024 * 1024), "filename": "offer.pdf",
        "extracted_text": "Purchase price 410,000."}


@pytest.mark.parametrize("name", ["foundry", "bedrock"])
def test_a_photo_turn_holds_an_allowance_not_its_encoded_size(monkeypatch, name):
    """Measured before the fix: one 3 MB photo held 1,409,037 tokens on Foundry
    and 3,689,086 on Bedrock against ~12k for the same turn without it."""
    _Transports(monkeypatch)
    _only(monkeypatch, name)
    holds = []
    for attachments in ([], [PHOTO], [PHOTO, SCAN]):
        scope = _Scope()
        _in_scope(scope, _tier(name, dict(BUNDLE, attachments=attachments)))
        holds.append(scope.before_calls[0][1])
    text_only, one_photo, photo_and_scan = holds
    allowance = llm_gateway.ATTACHMENT_TOKENS
    assert allowance <= one_photo - text_only <= allowance + 100
    # Foundry sends a PDF as its extracted text, which is counted as text.
    assert photo_and_scan - one_photo <= allowance + 100


def test_a_bedrock_pdf_is_held_at_its_extracted_text_with_the_allowance_as_a_floor(monkeypatch):
    """Bedrock is sent a PDF as raw bytes, so its hold was the flat allowance
    however long the document: a 38k-token disclosure held 8k. Its extracted
    text is in the bundle (Foundry already counts it), so it is held at that
    text's size, never below the allowance."""
    _quiet_turn(monkeypatch)
    _Transports(monkeypatch)
    _only(monkeypatch, "bedrock")
    long_text = "Seller disclosure, page after page. " * 3_200          # 115,200 characters
    holds = []
    for attachments in ([], [SCAN], [dict(SCAN, extracted_text=None)], [dict(SCAN, extracted_text=long_text)]):
        scope = _Scope()
        _in_scope(scope, ai_chat_agent._generate(AGENT, dict(BUNDLE, attachments=attachments), "assistant-pdf"))
        holds.append(scope.before_calls[0][1])
    no_file, short_text, no_text, long_pdf = holds
    allowance = llm_gateway.ATTACHMENT_TOKENS
    assert allowance <= short_text - no_file <= allowance + 100      # the floor
    assert allowance <= no_text - no_file <= allowance + 100
    text_tokens = -(-len(long_text) // llm_gateway.CHARS_PER_TOKEN)
    assert text_tokens <= long_pdf - no_file <= text_tokens + 100


# ── a thread-backed call stays held until its thread returns ─────────────────

class _SlowThread:
    """A provider call in a worker thread that finishes only when told to. As
    with the Foundry SDK and boto3, cancelling the task that awaits it does not
    stop it: the request is still in flight, and the provider bills it."""

    def __init__(self, response=None, error=None):
        self.started = threading.Event()
        self.finish = threading.Event()
        self.response = response
        self.error = error

    def __call__(self, *_a, **_k):
        self.started.set()
        self.finish.wait(10)
        if self.error is not None:
            raise self.error
        return self.response


async def _until(done, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


def _cut_off_while_running(scope, slow, call):
    """Start `call` in scope, cancel it once its thread runs (the turn
    deadline), report what the scope did by then, then let the thread finish."""
    seen = {}

    async def run():
        token = llm_gateway.spend_scope.set(scope)
        try:
            task = asyncio.ensure_future(call())
        finally:
            llm_gateway.spend_scope.reset(token)
        await _until(slow.started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        seen["while_running"] = (list(scope.released), list(scope.settled))
        slow.finish.set()
        await _until(lambda: scope.released or scope.settled)

    asyncio.run(run())
    return seen["while_running"]


@pytest.mark.parametrize("fails", [False, True])
def test_a_cut_off_thread_call_keeps_its_hold_until_the_thread_returns(fails):
    """spend_guarded released the hold on cancellation, but a deadline cannot
    stop a worker thread: other turns were admitted into room the call was
    still spending, and its usage was never recorded."""
    usage = {"usage": {"inputTokens": 900, "outputTokens": 40}}
    slow = _SlowThread(response=usage, error=RuntimeError("provider down") if fails else None)
    scope = _Scope()
    while_running = _cut_off_while_running(
        scope, slow, lambda: llm_gateway.spend_guarded_thread("bedrock", 10, slow))
    assert while_running == ([], []), "held while the thread still runs"
    if fails:
        assert scope.released == ["hold-1"] and scope.settled == []
    else:
        assert scope.settled == [("hold-1", usage)] and scope.released == []


@pytest.mark.parametrize("name", ["foundry", "bedrock"])
def test_a_tier_call_cut_off_by_the_turn_deadline_is_metered_when_it_returns(monkeypatch, name):
    _only(monkeypatch, name)
    if name == "foundry":
        response = type("R", (), {"output": [], "output_text": "late answer"})()
        slow = _SlowThread(response=response)
        client = _FoundryClient(slow)
        monkeypatch.setattr(ai_chat_agent, "_foundry_openai_client", lambda: client)
    else:
        response = {"output": {"message": {"content": [{"text": "late"}]}},
                    "usage": {"inputTokens": 900, "outputTokens": 40}}
        slow = _SlowThread(response=response)
        monkeypatch.setattr(ai_chat_agent, "_converse", slow)
    scope = _Scope()
    while_running = _cut_off_while_running(scope, slow, lambda: _tier(name))
    assert while_running == ([], [])
    assert scope.settled == [("hold-1", response)] and scope.released == []


# ── the floor is what the smallest real turn holds through its first answer ──

#: The request parse's answer for "hi": a question, nothing constrained.
_HI_FRAME = json.dumps({
    "goals": [{"label": "question", "negated": False, "evidence": [{"source": "utterance", "quote": "hi"}]}],
    "mentions": [], "times": [], "constraints": [], "scope": "mine", "required_data": [],
    "effect": "read", "unfilled_slots": [], "ambiguities": []})
#: The least a file adds on each file tier: Foundry is sent a PDF as its
#: extracted text; Bedrock holds any file at the attachment allowance or more.
SMALLEST_PDF = {"media_type": "application/pdf", "filename": "", "extracted_text": "x", "data": b""}
SMALLEST_IMAGE = {"media_type": "image/png", "filename": "", "data": b""}
#: The lane each tier's first answering call is held on.
LANE = {"gateway": "fireworks", "fireworks": "fireworks", "foundry": "foundry", "bedrock": "bedrock"}


def _hi(text="hi", attachments=()):
    """The smallest agent turn: `text` (and files), no record, history or context."""
    return {"messages": [{"role": "user", "content": text}], "record": None,
            "attachments": list(attachments), "assistant": {"context_type": None, "context_id": None}}


class _ParseAndAnswer:
    """litellm under the gateway: a request parse (asked for JSON) gets a frame
    with nothing in it, a chat round a plain answer."""

    def __init__(self):
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("response_format"):
            return _FakeResponse(_HI_FRAME)
        return {"choices": [{"message": {"content": "answer"}}]}


def _smallest_turns(monkeypatch, ladder, *, frames):
    """The REAL _generate for the smallest turn: `ladder` as the hosted chat
    tiers, no brokerage, memory or channel set up, and the request parser on
    (through llm_gateway, as deployed) or off. Use with the `gateway` fixture,
    which sets the provider environment."""
    import frame_constraints
    import semantic_turn
    import tool_offer

    fake = _ParseAndAnswer()
    monkeypatch.setattr(llm_gateway, "_litellm", lambda: fake)
    _Transports(monkeypatch, gateway_down=False)
    _quiet_turn(monkeypatch)

    async def no_channels(_ctx):
        return {channel: "not set up" for channel in tool_offer.CHANNEL_TOOLS}

    monkeypatch.setattr(tool_offer, "channel_gaps", no_channels)
    provider = llm_gateway.Provider(name="fireworks", model="fireworks_ai/test")
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers",
                        lambda *a, **k: [provider] if "gateway" in ladder else [])
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", "fireworks" in ladder)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "azure-foundry" if "foundry" in ladder else "local")
    monkeypatch.setattr(ai_chat_agent, "BEDROCK_FALLBACK_ENABLED", "bedrock" in ladder)
    if frames:
        monkeypatch.setenv(frame_constraints.ENV_FLAG, "on")
        monkeypatch.setattr(semantic_turn, "GATEWAY", None)


class _Budget(_Scope):
    """A cap with nothing used, judged hold by hold as ai_budget._reserve
    judges it. A settled call keeps its whole hold as used (the most it is
    estimated to spend); a released one gives it back."""

    def __init__(self, cap):
        super().__init__()
        self.left = cap
        self.admitted: list[tuple[str, int]] = []

    async def before(self, lane, estimate):
        self.before_calls.append((lane, estimate))
        if estimate > self.left:
            raise ai_budget.refusal("insufficient", AGENT)
        self.left -= estimate
        self.admitted.append((lane, estimate))
        return (lane, estimate)

    async def release(self, hold):
        self.released.append(hold)
        self.left += hold[1]


def _run(scope, bundle, assistant_id="assistant-floor"):
    _in_scope(scope, ai_chat_agent._generate(AGENT, bundle, assistant_id))


def _refused_at_admission(monkeypatch, floor):
    """Whether admission refuses with one token less than `floor` left."""
    _budget_reads(monkeypatch, cap=floor - 1, used=0)
    with pytest.raises(SpendRefused) as refused:
        asyncio.run(ai_budget.admission(AGENT, floor=floor))
    return refused.value.code == "AI_BUDGET_INSUFFICIENT"


@pytest.mark.parametrize("frames", [False, True])
@pytest.mark.parametrize("tier", ["gateway", "fireworks", "foundry", "bedrock"])
def test_the_floor_is_what_the_smallest_real_turn_holds_through_its_first_answer(
        monkeypatch, gateway, tier, frames):
    """The floor was built from the persona alone. _generate also appends the
    tool offer's note (an agent's broker-owner reads are always withheld, and
    named: ~1,400 characters), and with the parser on the request parse is
    held and spent first. Measured: the smallest real first call held 13,103 /
    10,402 / 10,802 tokens (gateway / Foundry / Bedrock) against a floor of
    12,623 / 9,895 / 10,344, so admission and the panel said 'ok' for a turn
    whose first answering call was then refused."""
    _smallest_turns(monkeypatch, [tier], frames=frames)
    floor = ai_chat_agent.smallest_request_hold(words=True, files=False)

    at_floor = _Budget(floor)
    _run(at_floor, _hi())
    assert [lane for lane, _ in at_floor.admitted] == (["fireworks"] if frames else []) + [LANE[tier]]
    assert at_floor.left == 0, "the floor is the parse and the first answering call, exactly"

    short = _Budget(floor - 1)
    with pytest.raises(SpendRefused):
        _run(short, _hi())
    assert short.admitted == at_floor.admitted[:-1], "refused at its first answering call"
    assert _refused_at_admission(monkeypatch, floor)


def test_a_text_turn_is_floored_by_the_first_tier_not_a_cheaper_later_one(monkeypatch, gateway):
    """A refusal on the first tier ends the turn: Foundry, cheaper here, never runs."""
    _smallest_turns(monkeypatch, ["gateway", "foundry"], frames=False)
    floor = ai_chat_agent.smallest_request_hold(words=True, files=False)
    assert floor > ai_chat_agent._first_call_holds()["text"]["foundry"]
    short = _Budget(floor - 1)
    with pytest.raises(SpendRefused):
        _run(short, _hi())
    assert [lane for lane, _ in short.before_calls] == ["fireworks"]


@pytest.mark.parametrize("frames", [False, True])
@pytest.mark.parametrize("text", ["hi", ""])
@pytest.mark.parametrize("tier,attachment", [("foundry", SMALLEST_PDF), ("bedrock", SMALLEST_IMAGE)])
def test_a_file_turn_is_floored_by_what_its_file_tier_is_sent(monkeypatch, gateway, tier, attachment, text, frames):
    """A turn with files skips the text-only gateway and starts on the file
    tier, and a turn with no words has no request to parse. The with-files
    floor added an attachment allowance on Foundry too, which is sent a PDF
    as text (_foundry_inputs); replacing that term with the first tier's text
    hold left every test green."""
    _smallest_turns(monkeypatch, ["gateway", tier], frames=frames)
    floor = ai_chat_agent.smallest_request_hold(words=bool(text), files=True)

    at_floor = _Budget(floor)
    _run(at_floor, _hi(text, [attachment]), "assistant-file")
    parse = ["fireworks"] if frames and text else []
    assert [lane for lane, _ in at_floor.admitted] == parse + [tier]
    assert at_floor.left == 0

    short = _Budget(floor - 1)
    with pytest.raises(SpendRefused):
        _run(short, _hi(text, [attachment]), "assistant-file")
    assert _refused_at_admission(monkeypatch, floor)


def test_a_foundry_pdf_turn_is_not_refused_for_an_allowance_it_never_carries(monkeypatch, gateway):
    """Measured: with [gateway, Foundry] the floor was 12,623 while a real
    Foundry PDF first call held 10,448, so admission refused PDF turns the
    per-call stop admitted."""
    _smallest_turns(monkeypatch, ["gateway", "foundry"], frames=False)
    scope = _Budget(10**9)
    _run(scope, _hi("hi", [SCAN]), "assistant-pdf")
    ((lane, real),) = scope.admitted
    assert lane == "foundry"
    floor = ai_chat_agent.smallest_request_hold(words=True, files=True)
    assert floor <= real < ai_chat_agent.smallest_request_hold(words=True, files=False)
    _budget_reads(monkeypatch, cap=real, used=0)
    assert asyncio.run(ai_budget.admission(AGENT, floor=floor)) in ("ok", "warning")


def test_the_panel_judges_against_the_least_any_turn_needs(monkeypatch, gateway):
    """With the parser on, a turn with files and no words skips the parse."""
    _smallest_turns(monkeypatch, ["gateway", "foundry"], frames=True)
    kinds = {(words, files): ai_chat_agent.smallest_request_hold(words=words, files=files)
             for words, files in ((True, False), (True, True), (False, True))}
    assert ai_chat_agent.smallest_request_hold() == min(kinds.values()) == kinds[(False, True)]


# ── a Foundry tool round holds what it carries ───────────────────────────────

class _FoundryItem:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, **_k):
        return dict(self.__dict__)


def test_a_foundry_tool_round_holds_at_least_the_round_before_it_and_its_receipts(monkeypatch):
    """Round 2 carries round 1's input, the model's call and the receipt: its
    hold can only grow. Nothing pinned it: replacing the later-round estimate
    with 1 left every test green."""
    _only(monkeypatch, "foundry")
    call = _FoundryItem(type="function_call", name="get_client_summary", arguments="{}", call_id="c-1")
    replies = [type("R", (), {"output": [call], "output_text": ""})(),
               type("R", (), {"output": [], "output_text": "answer"})()]

    receipt = {"ok": True, "summary": "x" * 6_000}

    async def execute(*_a, **_k):
        return receipt

    client = _FoundryClient(lambda **_k: replies.pop(0))
    monkeypatch.setattr(ai_chat_agent, "_foundry_openai_client", lambda: client)
    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", execute)
    scope = _Scope()
    _in_scope(scope, _tier("foundry"))
    (lane1, first), (lane2, second) = scope.before_calls
    assert lane1 == lane2 == "foundry" and len(scope.settled) == 2
    assert second >= first + len(json.dumps(receipt)) // llm_gateway.CHARS_PER_TOKEN


def test_a_bedrock_tool_round_still_holds_the_pdf_at_its_text(monkeypatch):
    """Every Bedrock round re-sends the PDF as bytes; only the `documents`
    _generate passes holds it at its text. Dropping them from the second
    round left every test green, and that round was held ~30k tokens short."""
    _quiet_turn(monkeypatch)
    _only(monkeypatch, "bedrock")
    replies = [{"output": {"message": {"role": "assistant", "content": [
                   {"toolUse": {"toolUseId": "t-1", "name": "get_client_summary", "input": {}}}]}}},
               {"output": {"message": {"role": "assistant", "content": [{"text": "answer"}]}}}]
    monkeypatch.setattr(ai_chat_agent, "_converse", lambda *_a: replies.pop(0))

    async def execute(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", execute)
    long_text = "Seller disclosure, page after page. " * 3_200          # 115,200 characters
    scope = _Scope()
    _in_scope(scope, ai_chat_agent._generate(
        AGENT, dict(BUNDLE, attachments=[dict(SCAN, extracted_text=long_text)]), "assistant-r2"))
    (lane1, first), (lane2, second) = scope.before_calls
    assert lane1 == lane2 == "bedrock" and len(scope.settled) == 2
    assert second >= first >= llm_gateway.document_tokens(long_text)


# ── a Foundry request cannot outlive its hold, and is never retried ─────────

def test_a_foundry_request_in_a_turn_is_bounded_below_its_hold_and_not_retried(monkeypatch):
    """The SDK's defaults are 600 s and two retries per request. The turn stops
    waiting at 90 s but the thread runs on: a request could outlive its hold
    (ai_budget.HOLD_SECONDS), and a retried attempt is billed while only the
    final response's usage is settled. The ladder is the fallback."""
    _only(monkeypatch, "foundry")
    client = _FoundryClient(lambda **_k: type("R", (), {"output": [], "output_text": "answer"})())
    monkeypatch.setattr(ai_chat_agent, "_foundry_openai_client", lambda: client)
    _in_scope(_Scope(), _tier("foundry"))
    bound = ai_chat_agent._FOUNDRY_REQUEST_SECONDS
    assert client.options == [{"timeout": bound, "max_retries": 0}]
    assert 0 < bound <= min(ai_chat_agent._FOUNDRY_ROUND_SECONDS, ai_chat_agent._TURN_DEADLINE_SECONDS)
    assert bound < ai_budget.HOLD_SECONDS
    # Outside a spend scope (the phone path) the client is used as configured.
    client.options.clear()
    ai_chat_agent._foundry_response([], None)
    assert client.options == []


# ── the job: an honest refusal, never "unavailable" ───────────────────────────

PAYLOAD = {"tenant_id": TENANT, "user_id": "agent@tenant.test",
           "assistant_id": "22222222-2222-2222-2222-222222222222", "request_id": "r1"}


def _job(monkeypatch, generate):
    calls = {"updates": [], "broadcasts": []}

    async def bundle(_ctx, _aid):
        return {"assistant": {"status": "pending"}, "messages": [{"content": "hi"}]}

    async def update(_ctx, _aid, **kw):
        calls["updates"].append(kw)

    async def broadcast(_t, _u, msg):
        calls["broadcasts"].append(msg)

    async def release(_ctx):
        return None

    class _Sessions:
        def __init__(self, _ctx):
            pass

        async def record_interaction(self, *_a):
            return None

    monkeypatch.setattr(ai_chat_agent, "load_response_bundle", bundle)
    monkeypatch.setattr(ai_chat_agent, "_generate", generate)
    monkeypatch.setattr(ai_chat_agent, "update_assistant", update)
    monkeypatch.setattr(ai_chat_agent.ws_hub, "broadcast_user", broadcast)
    monkeypatch.setattr(ai_chat_agent, "release_concurrency", release)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", _Sessions)
    return calls


def test_a_refused_turn_says_why_with_the_budget_code(monkeypatch):
    async def generate(*_a, **_k):
        raise _exhausted()

    calls = _job(monkeypatch, generate)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["status"] == "failed" and out["code"] == "AI_BUDGET_EXHAUSTED"
    final = calls["updates"][-1]
    assert final["status_value"] == "failed" and final["error_code"] == "AI_BUDGET_EXHAUSTED"
    frame = calls["broadcasts"][-1]
    assert frame["type"] == "AI_CHAT_ERROR" and frame["code"] == "AI_BUDGET_EXHAUSTED"
    assert frame["message"] == "The monthly AI budget is used up."
    assert frame["budget_state"] == "exhausted"


def test_an_unreadable_budget_is_its_own_retryable_code(monkeypatch):
    async def generate(*_a, **_k):
        raise SpendUnavailable("unknown", "AI_BUDGET_UNAVAILABLE", "Try again in a moment.")

    calls = _job(monkeypatch, generate)
    asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert calls["broadcasts"][-1]["code"] == "AI_BUDGET_UNAVAILABLE"


def test_a_refusal_after_writes_completes_the_turn_with_its_receipts(monkeypatch):
    async def generate(*_a, **_k):
        refused = _exhausted()
        refused.applied = [{"ok": True, "action_id": "act-9", "status": "applied"}]
        raise refused

    calls = _job(monkeypatch, generate)
    monkeypatch.setattr(ai_chat_agent, "_broadcast_chunks", _noop)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["status"] == "degraded"
    final = calls["updates"][-1]
    assert final["status_value"] == "completed" and final["error_code"] == "AI_BUDGET_EXHAUSTED"
    assert "was applied" in final["content"] and "budget" in final["content"]
    frame = calls["broadcasts"][-1]
    assert frame["type"] == "AI_CHAT_COMPLETE" and frame["actions"][0]["action_id"] == "act-9"
    assert frame["budget_state"] == "exhausted" and frame["code"] == "AI_BUDGET_EXHAUSTED"


async def _noop(*_a, **_k):
    return None


#: What a refusal may say only while the turn has made no hosted call.
_NOTHING_SENT = ("Nothing was sent", "didn't ask a model", "before asking a model")


def _turn_refused(monkeypatch, state, *, admitted):
    """Run the job with a real SpendScope: the turn's first `admitted` hosted
    calls are held (and sent), the next one is refused with `state`. Returns
    what the person was told."""
    async def reserve(ctx, call_key, estimate):
        if int(call_key.rsplit(":", 1)[1]) < admitted:
            return ai_budget.Hold(call_key=call_key, tokens=estimate, state="ok", held=True)
        raise ai_budget.refusal(state, ctx)

    async def generate(_ctx, _bundle, _aid):
        scope = llm_gateway.spend_scope.get()
        for _ in range(admitted + 1):
            await scope.before("fireworks", 100)
        raise AssertionError("the last call must have been refused")

    monkeypatch.setattr(ai_budget, "reserve", reserve)
    monkeypatch.setattr(ai_budget, "settle", _noop)
    calls = _job(monkeypatch, generate)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    frame = calls["broadcasts"][-1]
    assert out["status"] == "failed" and frame["code"] == ai_budget.CODES[state]
    return frame["message"]


@pytest.mark.parametrize("state", ["exhausted", "insufficient", "platform_halted", "unknown"])
def test_a_refusal_after_the_turn_already_asked_a_model_never_says_nothing_was_sent(monkeypatch, state):
    """Round 1 (or the request parse) was sent and metered; round 2 was
    refused. The person was told 'Nothing was sent to a model' / 'didn't ask
    a model'."""
    message = _turn_refused(monkeypatch, state, admitted=1)
    assert not any(claim in message for claim in _NOTHING_SENT), message
    assert "next model call" in message


@pytest.mark.parametrize("state", ["exhausted", "insufficient", "platform_halted", "unknown"])
def test_a_refusal_before_any_model_call_says_nothing_was_asked(monkeypatch, state):
    message = _turn_refused(monkeypatch, state, admitted=0)
    assert any(claim in message for claim in _NOTHING_SENT), message


def test_the_turn_runs_inside_its_own_spend_scope(monkeypatch):
    seen = {}

    async def generate(ctx, _bundle, assistant_id):
        scope = llm_gateway.spend_scope.get()
        seen["scope"] = scope
        scope.state = "warning"      # as if the last hold crossed warn_pct
        return "answer", [], "model"

    calls = _job(monkeypatch, generate)
    monkeypatch.setattr(ai_chat_agent, "_broadcast_chunks", _noop)
    asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert isinstance(seen["scope"], ai_budget.SpendScope)
    assert seen["scope"].turn_key == f"chat:{PAYLOAD['assistant_id']}"
    assert llm_gateway.spend_scope.get() is None
    frame = calls["broadcasts"][-1]
    assert frame["type"] == "AI_CHAT_COMPLETE" and frame["budget_state"] == "warning"


# ── the socket: refused at admission, nothing persisted or queued ─────────────

class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, frame):
        self.sent.append(frame)


@pytest.mark.parametrize("refusal", [
    SpendRefused("paused", "AI_SPEND_PAUSED", "Your broker has paused Neoh's AI."),
    SpendRefused("platform_halted", "AI_SPEND_HALTED", "Paused across the platform."),
    SpendUnavailable("unknown", "AI_BUDGET_UNAVAILABLE", "Try again in a moment."),
])
def test_the_socket_refuses_before_anything_is_persisted(monkeypatch, refusal):
    import ai_chat_api

    asked = {}

    async def admission(_ctx, *, floor):
        asked["floor"] = floor
        raise refusal

    async def never(*_a, **_k):
        raise AssertionError("nothing may be persisted or queued for a refused turn")

    monkeypatch.setattr(ai_chat_api, "_enabled", lambda: True)
    monkeypatch.setattr(ai_budget, "admission", admission)
    monkeypatch.setattr(ai_chat_api, "create_chat_request", never)
    monkeypatch.setattr(ai_chat_api, "enqueue_job", never)
    socket = _Socket()
    frame = {"type": "AI_CHAT_SEND", "version": 1, "request_id": "33333333-3333-3333-3333-333333333333",
             "content": "hello"}
    kinds = []
    monkeypatch.setattr(ai_chat_agent, "smallest_request_hold", lambda **kind: kinds.append(kind) or 7_321)
    asyncio.run(ai_chat_api.handle_chat_websocket(AGENT, socket, frame))
    assert socket.sent == [{"type": "AI_CHAT_REJECTED", "version": 1,
                            "request_id": "33333333-3333-3333-3333-333333333333",
                            "code": refusal.code, "message": refusal.message}]
    # Admission is asked about the smallest request this kind of turn makes
    # (words, no files: a request parse, then a text tier), not 0.
    assert asked == {"floor": 7_321}
    assert kinds == [{"words": True, "files": False}]


# ── the budget itself, without a database ────────────────────────────────────

def test_an_unreadable_budget_never_passes(monkeypatch):
    from db import connection

    monkeypatch.setattr(connection, "_pool", None)
    monkeypatch.delenv("ORACLE_AI_INFERENCE_HALT", raising=False)
    with pytest.raises(SpendUnavailable) as held:
        asyncio.run(ai_budget.reserve(AGENT, "chat:x:fireworks:0", 1000))
    assert held.value.state == "unknown" and held.value.code == "AI_BUDGET_UNAVAILABLE"
    with pytest.raises(SpendUnavailable):
        asyncio.run(ai_budget.admission(AGENT))


def test_the_deploy_time_kill_switch_refuses_without_reading_anything(monkeypatch):
    from db import connection

    monkeypatch.setattr(connection, "_pool", None)
    monkeypatch.setenv("ORACLE_AI_INFERENCE_HALT", "1")
    with pytest.raises(SpendRefused) as refused:
        asyncio.run(ai_budget.reserve(AGENT, "chat:x:fireworks:0", 1000))
    assert refused.value.state == "platform_halted" and refused.value.code == "AI_SPEND_HALTED"
    assert not isinstance(refused.value, SpendUnavailable)


def test_the_refusal_tells_an_agent_to_ask_and_an_owner_where_to_change_it():
    agent = ai_budget.refusal("exhausted", AGENT)
    owner = ai_budget.refusal("exhausted", OWNER)
    assert "broker" in agent.message and "Settings" not in agent.message
    assert "Settings" in owner.message
    assert ai_budget.refusal("paused", OWNER).code == "AI_SPEND_PAUSED"
    assert ai_budget.refusal("platform_halted", AGENT).code == "AI_SPEND_HALTED"
    assert isinstance(ai_budget.refusal("unknown", AGENT), SpendUnavailable)
    # Room is left, just not enough for this request: never "used up".
    short = ai_budget.refusal("insufficient", AGENT)
    assert short.code == "AI_BUDGET_INSUFFICIENT" and not isinstance(short, SpendUnavailable)
    assert "used up" not in short.message and "this request" in short.message and "broker" in short.message
    assert "Settings" in ai_budget.refusal("insufficient", OWNER).message


def _budget_reads(monkeypatch, *, cap, used, reserved=0):
    from datetime import datetime, timezone

    async def read(_ctx, *, totals=True):
        controls = {"cap": cap, "warn_pct": 80, "paused": False, "halted": False}
        return controls, {"prompt": used, "completion": 0, "reserved": reserved,
                          "start_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
                          "end_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}

    monkeypatch.delenv("ORACLE_AI_INFERENCE_HALT", raising=False)
    monkeypatch.setattr(ai_budget, "_read", read)


def test_with_less_left_than_the_smallest_request_a_turn_is_refused_at_admission(monkeypatch):
    """Admission used to refuse only at the cap while each call refused when
    its estimate did not fit: with room for neither request, a turn was
    accepted, spent its request parse, and was then told the budget was
    'used up' while the owner's panel said 'Within budget'."""
    _budget_reads(monkeypatch, cap=10_000, used=3_000, reserved=1_000)      # 6,000 left
    assert asyncio.run(ai_budget.admission(AGENT, floor=6_000)) == "ok"
    with pytest.raises(SpendRefused) as refused:
        asyncio.run(ai_budget.admission(AGENT, floor=6_001))
    assert refused.value.state == "insufficient" and refused.value.code == "AI_BUDGET_INSUFFICIENT"
    state = asyncio.run(ai_budget.state(OWNER, floor=6_001))
    assert state["state"] == "insufficient" and state["smallest_request_estimate"] == 6_001
    assert asyncio.run(ai_budget.state(OWNER, floor=6_000))["state"] == "ok"
    # At the cap it is still "exhausted"; with no cap the floor never applies.
    _budget_reads(monkeypatch, cap=10_000, used=10_000)
    assert asyncio.run(ai_budget.state(OWNER, floor=6_001))["state"] == "exhausted"
    _budget_reads(monkeypatch, cap=None, used=10**9)
    assert asyncio.run(ai_budget.state(OWNER, floor=6_001))["state"] == "unlimited"


def test_a_scope_numbers_each_lane_so_every_call_has_its_own_key(monkeypatch):
    keys = []

    async def reserve(_ctx, call_key, estimate):
        keys.append(call_key)
        return ai_budget.Hold(call_key=call_key, tokens=estimate, state="ok", held=True)

    monkeypatch.setattr(ai_budget, "reserve", reserve)
    scope = ai_budget.SpendScope(AGENT, "chat:abc")

    async def run():
        for lane in ("fireworks", "fireworks", "foundry", "fireworks"):
            await scope.before(lane, 10)

    asyncio.run(run())
    assert keys == ["chat:abc:fireworks:0", "chat:abc:fireworks:1", "chat:abc:foundry:0",
                    "chat:abc:fireworks:2"]
    assert scope.state == "ok"


# ── who may change a budget (AC18.05) ─────────────────────────────────────────

def _client(ctx):
    import ai_budget_api

    app = FastAPI()
    app.include_router(ai_budget_api.router)
    app.dependency_overrides[require_context] = lambda: ctx
    return TestClient(app)


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/billing/budgets/ai-tokens", None),
    ("put", "/api/billing/budgets/ai-tokens", {"monthly_cap_tokens": 10, "warn_pct": 80, "paused": False}),
    ("get", "/api/admin/ai-spend", None),
    ("put", "/api/admin/ai-spend", {"halted": True, "reason": "test"}),
])
def test_an_agent_cannot_read_or_change_a_budget(method, path, body):
    response = getattr(_client(AGENT), method)(path, **({"json": body} if body else {}))
    assert response.status_code == 403


@pytest.mark.parametrize("method,body", [("get", None), ("put", {"halted": True, "reason": "x"})])
def test_a_broker_owner_cannot_touch_the_platform_switch(method, body):
    response = getattr(_client(OWNER), method)("/api/admin/ai-spend", **({"json": body} if body else {}))
    assert response.status_code == 403


@pytest.mark.parametrize("body", [
    {"monthly_cap_tokens": 0, "warn_pct": 80, "paused": False},
    {"monthly_cap_tokens": -5, "warn_pct": 80, "paused": False},
    {"monthly_cap_tokens": 1000, "warn_pct": 0, "paused": False},
    {"monthly_cap_tokens": 1000, "warn_pct": 101, "paused": False},
    {"warn_pct": 80, "paused": False},
    {"monthly_cap_tokens": 1000, "warn_pct": 80, "paused": False, "extra": 1},
])
def test_a_bad_budget_is_refused_before_anything_is_written(body):
    assert _client(OWNER).put("/api/billing/budgets/ai-tokens", json=body).status_code == 422


def test_an_unreadable_budget_is_503_and_never_unlimited(monkeypatch):
    from db import connection

    monkeypatch.setattr(connection, "_pool", None)
    response = _client(OWNER).get("/api/billing/budgets/ai-tokens")
    assert response.status_code == 503
    assert "unlimited" not in response.text


def _saving(monkeypatch, *, write_fails=False, read_fails=False):
    import ai_budget_api

    written: dict = {}

    async def settings(_ctx):
        if read_fails:
            raise ConnectionError("database down")
        return {"monthly_cap_tokens": None, "warn_pct": 80, "paused": False}

    async def set_budget(_ctx, **values):
        if write_fails:
            raise ConnectionError("database down")
        written.update(values)

    async def unreadable_state(ctx, **_kw):
        raise ai_budget.refusal("unknown", ctx)

    async def audit(*_a, **_k):
        return None

    monkeypatch.setattr(ai_budget, "settings", settings)
    monkeypatch.setattr(ai_budget, "set_budget", set_budget)
    monkeypatch.setattr(ai_budget, "state", unreadable_state)
    monkeypatch.setattr(ai_budget_api, "audit_now", audit)
    return written


def test_a_committed_budget_change_is_reported_saved_when_usage_cannot_be_read(monkeypatch):
    """The pause or cap is in force once set_budget commits. A usage read that
    fails after it must not answer '503 Nothing was changed'."""
    written = _saving(monkeypatch)
    body = {"monthly_cap_tokens": 5_000_000, "warn_pct": 70, "paused": True}
    response = _client(OWNER).put("/api/billing/budgets/ai-tokens", json=body)
    assert response.status_code == 200, response.text
    assert written == {"cap": 5_000_000, "warn_pct": 70, "paused": True}
    saved = response.json()
    assert saved["saved"] is True
    assert (saved["cap"], saved["warn_pct"], saved["paused"]) == (5_000_000, 70, True)
    # Usage is missing evidence, never a zero.
    assert saved["used"] is None and saved["reserved"] is None
    assert saved["state"] == "unknown" and saved["evidence_status"] == "unavailable"
    assert "Nothing was changed" not in response.text


def test_a_saved_budget_is_reported_against_the_smallest_request(monkeypatch):
    """The PUT's answer is judged as the GET's and the socket's are: computed
    without the floor, a cap the smallest request no longer fits read 'ok'
    right after it was saved."""
    _saving(monkeypatch)
    asked = {}

    async def state(_ctx, *, floor=0):
        asked["floor"] = floor
        return {"state": "insufficient" if floor > 4_000 else "ok", "smallest_request_estimate": floor}

    monkeypatch.setattr(ai_budget, "state", state)
    monkeypatch.setattr(ai_chat_agent, "smallest_request_hold", lambda: 9_876)
    response = _client(OWNER).put("/api/billing/budgets/ai-tokens",
                                  json={"monthly_cap_tokens": 5_000, "warn_pct": 80, "paused": False})
    assert response.status_code == 200, response.text
    assert asked == {"floor": 9_876}
    saved = response.json()
    assert saved["saved"] is True
    assert saved["state"] == "insufficient" and saved["smallest_request_estimate"] == 9_876


def test_a_budget_write_that_fails_cannot_say_nothing_was_changed(monkeypatch):
    """A connection dropped during COMMIT may have committed the change: the
    outcome is unknown, so the 503 says so instead of 'Nothing was changed'."""
    _saving(monkeypatch, write_fails=True)
    response = _client(OWNER).put("/api/billing/budgets/ai-tokens",
                                  json={"monthly_cap_tokens": None, "warn_pct": 80, "paused": True})
    assert response.status_code == 503
    assert "Nothing was changed" not in response.text
    detail = response.json()["detail"]
    assert "could not confirm" in detail.lower() and "reload" in detail.lower()


def test_a_budget_change_that_never_reached_its_write_says_nothing_was_changed(monkeypatch):
    written = _saving(monkeypatch, read_fails=True)
    response = _client(OWNER).put("/api/billing/budgets/ai-tokens",
                                  json={"monthly_cap_tokens": None, "warn_pct": 80, "paused": True})
    assert response.status_code == 503 and "Nothing was changed" in response.text
    assert written == {}


def test_the_owner_read_reports_against_the_smallest_request(monkeypatch):
    asked = {}

    async def state(_ctx, *, floor):
        asked["floor"] = floor
        return {"state": "insufficient"}

    monkeypatch.setattr(ai_budget, "state", state)
    monkeypatch.setattr(ai_chat_agent, "smallest_request_hold", lambda: 9_876)
    assert _client(OWNER).get("/api/billing/budgets/ai-tokens").json() == {"state": "insufficient"}
    assert asked == {"floor": 9_876}


# ── the migration ─────────────────────────────────────────────────────────────

def test_the_migration_forces_rls_guards_writes_and_documents_its_rollback():
    sql = MIGRATION.read_text()
    body = re.sub(r"--[^\n]*", "", sql)
    for table in ("platform_spend_controls", "ai_token_reservations"):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in body, table
    assert "tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling()" in body
    guard = body[body.index("FUNCTION tenant_action_budgets_guard()"):]
    assert "SECURITY DEFINER" in guard[:200] and "SET search_path = public, pg_temp" in guard[:300]
    assert "REVOKE ALL ON FUNCTION tenant_action_budgets_guard() FROM PUBLIC" in body
    assert "ERRCODE = '42501'" in guard and "broker_owner" in guard
    assert "REVOKE INSERT, DELETE ON platform_spend_controls FROM oracle_app" in body
    assert "REVOKE UPDATE ON ai_token_reservations FROM oracle_app" in body
    assert "Rollback:" in sql and "NULL = no cap" in sql


def test_the_migration_is_additive_for_the_previous_release():
    from migration_safety import ADDITIVE, classify_files

    (verdict,) = classify_files([MIGRATION])
    assert verdict.classification == ADDITIVE, verdict
