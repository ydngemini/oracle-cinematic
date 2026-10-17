"""The model is offered only what this actor can use, and is told why the rest
is not (prompt 07, T07.04, AC07.01; ledger L07-03).

The release's offer (offered_tool_names) is untouched; tool_offer subtracts the
broker-owner reads from an agent's turn and the channels the brokerage has
definitively not set up, names each in the system prompt, and the executor
refuses the same names with the same reason. Unknown channel state withholds
nothing.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import ai_chat_agent
import ai_chat_api
import ai_chat_store
import tool_offer
from ai_tools_read import OPS_ROLE_TOOLS
from tenancy import Role, TenantContext

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT, role=Role.AGENT)
OWNER = TenantContext(agent_id="owner@tenant.test", tenant_id=TENANT, role=Role.BROKER_OWNER)
ADMIN = TenantContext(agent_id="admin@platform.test", tenant_id=TENANT, role=Role.PLATFORM_ADMIN)
SMS_GAP = "texting isn't set up: no registered text number for this agent. Set one up under Business number."


# ── the offer itself ─────────────────────────────────────────────────────────

def test_an_agent_is_not_offered_the_owner_only_reads_and_is_told_why():
    release = ai_chat_agent.offered_tool_names("client")
    offer = tool_offer.for_actor(AGENT, "client")
    assert set(offer.withheld) == set(OPS_ROLE_TOOLS) & release
    assert offer.offered == release - OPS_ROLE_TOOLS
    assert offer.offered.isdisjoint(offer.withheld)
    for reason in offer.withheld.values():
        # What this session lacks — never who the person "is" (a job queued
        # without a recorded role runs as an agent by default, not by proof).
        assert "broker-owner" in reason and "signed in as" not in reason


@pytest.mark.parametrize("ctx", [OWNER, ADMIN])
def test_the_owner_and_a_platform_admin_get_the_release_offer_unchanged(ctx):
    for context in (None, "client", "listing", "lead"):
        offer = tool_offer.for_actor(ctx, context)
        assert offer.offered == ai_chat_agent.offered_tool_names(context)
        assert offer.withheld == {} and offer.prompt_note() == ""


def test_a_channel_gap_withholds_that_channels_tool_with_the_gap_as_the_reason():
    offer = tool_offer.for_actor(OWNER, "client", {"sms": SMS_GAP})
    assert offer.withheld == {"draft_sms": SMS_GAP}
    assert "draft_sms" not in offer.offered and "draft_email" in offer.offered


def test_a_gap_for_a_tool_this_record_never_unlocks_says_nothing():
    # A listing unlocks no outreach, so there is no text tool to withhold.
    offer = tool_offer.for_actor(OWNER, "listing", {"sms": SMS_GAP, "calendar": "no calendar"})
    assert offer.withheld == {}


def test_unknown_channel_state_withholds_nothing():
    for gaps in (None, {}, {"sms": ""}, {"fax": "no fax"}):
        assert tool_offer.for_actor(OWNER, "client", gaps).withheld == {}


def test_the_prompt_note_names_each_withheld_tool_and_its_reason():
    offer = tool_offer.for_actor(AGENT, "client", {"sms": SMS_GAP, "voice": "Neoh calls aren't set up."})
    note = offer.prompt_note()
    assert note.startswith(tool_offer.PROMPT_LEAD)
    assert "never claim to have done it" in note
    assert f"- draft_sms: {SMS_GAP}" in note
    assert "- call_contact: Neoh calls aren't set up." in note
    assert "- get_tenant_health: limited to the broker-owner" in note
    assert note.count("\n- ") == len(offer.withheld)


def test_the_refusal_carries_the_reason_or_the_old_wording_for_an_unknown_tool():
    offer = tool_offer.for_actor(AGENT, "client", {"sms": SMS_GAP})
    assert offer.refusal("add_client_note") is None
    assert offer.refusal("draft_sms") == f"draft_sms is not offered in this conversation: {SMS_GAP}"
    assert "broker-owner" in offer.refusal("get_tenant_health")
    assert offer.refusal("move_deal_stage") == "move_deal_stage is not available for this conversation."


# ── reading the channel state ────────────────────────────────────────────────

def test_without_a_pool_the_channel_state_is_unknown_and_nothing_is_read(monkeypatch):
    import channel_setup
    import db.connection as connection

    monkeypatch.setattr(connection, "get_pool", lambda: None)

    async def must_not_run(_ctx):
        raise AssertionError("no pool, no reads")

    monkeypatch.setattr(channel_setup, "gaps", must_not_run)
    assert asyncio.run(tool_offer.channel_gaps(AGENT)) == {}


def test_a_failing_or_slow_read_is_unknown_not_missing(monkeypatch):
    import channel_setup
    import db.connection as connection

    monkeypatch.setattr(connection, "get_pool", lambda: object())

    async def boom(_ctx):
        raise RuntimeError("provider_credentials unreadable")

    monkeypatch.setattr(channel_setup, "gaps", boom)
    assert asyncio.run(tool_offer.channel_gaps(AGENT)) == {}

    async def slow(_ctx):
        await asyncio.sleep(30)

    monkeypatch.setattr(channel_setup, "gaps", slow)
    monkeypatch.setattr(tool_offer, "_GAPS_TIMEOUT_S", 0.01)
    assert asyncio.run(tool_offer.channel_gaps(AGENT)) == {}

    async def gaps(_ctx):
        return {"sms": SMS_GAP, "email": ""}

    monkeypatch.setattr(channel_setup, "gaps", gaps)
    assert asyncio.run(tool_offer.channel_gaps(AGENT)) == {"sms": SMS_GAP}


# ── the executor answers with the same offer the model saw ──────────────────

def test_the_executor_refuses_a_withheld_tool_with_the_reason_before_touching_anything(monkeypatch):
    async def must_not_dispatch(*_a, **_k):
        raise AssertionError("a withheld tool must never reach the dispatcher")

    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", must_not_dispatch)
    offer = tool_offer.for_actor(AGENT, "client", {"sms": SMS_GAP})
    tool_offer.remember(AGENT, "turn-1", offer)
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        AGENT, AGENT.agent_id, "turn-1", "draft_sms", {"client_id": "c-1", "body": "hi"}, "client", "c-1", 0))
    assert receipt == {"ok": False, "error": f"draft_sms is not offered in this conversation: {SMS_GAP}"}


def test_an_agent_calling_an_owner_only_read_is_refused_by_the_offer_not_the_handler(monkeypatch):
    async def must_not_dispatch(*_a, **_k):
        raise AssertionError("the role reason comes from the offer")

    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", must_not_dispatch)
    # No remembered turn: the role-only offer still applies.
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        AGENT, AGENT.agent_id, "never-remembered", "get_tenant_health", {}, None, None))
    assert receipt["ok"] is False and "broker-owner" in receipt["error"]


def test_the_owner_passes_the_gate_for_the_same_read(monkeypatch):
    async def dispatched(*_a, **_k):
        return {"ok": True, "tenant_health": "fine"}

    monkeypatch.setattr(ai_chat_store, "_execute_safe_tool", dispatched)
    receipt = asyncio.run(ai_chat_store.execute_safe_tool(
        OWNER, OWNER.agent_id, "never-remembered", "get_tenant_health", {}, None, None))
    assert receipt["ok"] is True


def test_a_remembered_offer_is_used_only_for_its_own_turn_and_record():
    offer = tool_offer.for_actor(OWNER, "client", {"sms": SMS_GAP})
    tool_offer.remember(OWNER, "turn-2", offer)
    assert tool_offer.current(OWNER, "turn-2", "client") is offer
    # Another turn, or the same turn against another record, falls back to the
    # role-only offer rather than reusing a stale channel verdict.
    assert tool_offer.current(OWNER, "turn-3", "client").withheld == {}
    assert tool_offer.current(OWNER, "turn-2", "lead").withheld == {}
    other = TenantContext(agent_id="owner@tenant.test", tenant_id="22222222-2222-2222-2222-222222222222",
                          role=Role.BROKER_OWNER)
    assert tool_offer.current(other, "turn-2", "client").withheld == {}


def test_remembered_turns_are_bounded_oldest_first(monkeypatch):
    from collections import OrderedDict

    monkeypatch.setattr(tool_offer, "_TURNS", OrderedDict())
    monkeypatch.setattr(tool_offer, "_REMEMBERED_TURNS", 3)
    offer = tool_offer.for_actor(OWNER, None)
    for i in range(5):
        tool_offer.remember(OWNER, f"bounded-{i}", offer)
    assert [key[1] for key in tool_offer._TURNS] == ["bounded-2", "bounded-3", "bounded-4"]


# ── every provider's tool list drops the withheld names ─────────────────────

def test_the_local_and_gateway_payload_omits_withheld_tools(monkeypatch):
    sent = []

    async def fake_chat(payload, **_kwargs):
        sent.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(ai_chat_agent, "_local_chat", fake_chat)
    bundle = {"attachments": [], "record": None, "messages": [{"role": "user", "content": "hi"}],
              "assistant": {"context_type": "client", "context_id": "c-1"},
              "tool_offer": tool_offer.for_actor(AGENT, "client", {"sms": SMS_GAP})}
    asyncio.run(ai_chat_agent._local_fallback(SimpleNamespace(agent_id="a"), bundle, "system", "asst-1"))
    names = {t["function"]["name"] for t in sent[0]["tools"]}
    assert "draft_sms" not in names and "get_tenant_health" not in names
    assert {"add_client_note", "draft_email", "search_clients"} <= names
    assert names == {t["name"] for t in ai_chat_agent._foundry_tools("client")} - set(bundle["tool_offer"].withheld)


def test_the_foundry_request_omits_withheld_tools(monkeypatch):
    seen = {}

    class Responses:
        def create(self, **kw):
            seen.update(kw)
            return "response"

    monkeypatch.setattr(ai_chat_agent, "_foundry_openai_client", lambda: SimpleNamespace(responses=Responses()))
    withheld = frozenset({"draft_sms", "get_tenant_health"})
    assert ai_chat_agent._foundry_response([], "client", withheld) == "response"
    names = {t["name"] for t in seen["tools"]}
    assert names.isdisjoint(withheld) and "draft_email" in names
    # Nothing withheld: the release's list, exactly as before.
    ai_chat_agent._foundry_response([], "client")
    assert {t["name"] for t in seen["tools"]} == {t["name"] for t in ai_chat_agent._foundry_tools("client")}


def test_the_bedrock_tool_config_omits_withheld_tools_and_is_none_when_nothing_is_left(monkeypatch):
    withheld = frozenset({"draft_sms", "get_tenant_health"})
    config = ai_chat_agent._offered_tool_config("client", withheld)
    names = {t["toolSpec"]["name"] for t in config["tools"]}
    assert names.isdisjoint(withheld) and "draft_email" in names
    assert ai_chat_agent._offered_tool_config("client", frozenset()) == ai_chat_agent._tool_config("client")
    monkeypatch.setattr(ai_chat_agent, "_tool_config", lambda _ct: {"tools": [{"toolSpec": {"name": "draft_sms"}}]})
    assert ai_chat_agent._offered_tool_config("client", withheld) is None


def test_a_bedrock_turn_sends_the_filtered_tool_config(monkeypatch):
    seen = {}

    async def fake_brokerage(_ctx):
        return None

    class FakeMemory:
        def __init__(self, _ctx): pass
        async def inject_jit_prompt(self, _agent, base): return base

    def fake_converse(messages, system_prompt, tool_config):
        seen["tool_config"], seen["prompt"] = tool_config, system_prompt
        return {"output": {"message": {"content": [{"text": "Texting needs setup here."}]}}}

    async def gaps(_ctx):
        return {"sms": SMS_GAP}

    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", fake_brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", FakeMemory)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *a, **k: [])
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", False)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "bedrock")
    monkeypatch.setattr(ai_chat_agent, "_converse", fake_converse)
    monkeypatch.setattr(tool_offer, "channel_gaps", gaps)
    bundle = {"messages": [], "record": None, "attachments": [],
              "assistant": {"context_type": "client", "context_id": "c-1"}}
    text, actions, _model = asyncio.run(ai_chat_agent._generate(AGENT, bundle, "assistant-bedrock-1"))
    assert text == "Texting needs setup here." and actions == []
    names = {t["toolSpec"]["name"] for t in seen["tool_config"]["tools"]}
    assert "draft_sms" not in names and "get_tenant_health" not in names and "draft_email" in names
    assert f"- draft_sms: {SMS_GAP}" in seen["prompt"]


def test_a_foundry_turn_omits_withheld_tools_and_keys_the_executor_on_the_turn(monkeypatch):
    seen = {"tools": [], "executed": []}

    class Call:
        type = "function_call"
        name, call_id, arguments = "add_client_note", "call-1", '{"note": "prefers mornings"}'

        def model_dump(self, **_kw):
            return {"type": "function_call", "name": self.name, "call_id": self.call_id, "arguments": self.arguments}

    class Done:
        output, output_text = [], "Saved the note."

    responses = [SimpleNamespace(output=[Call()], output_text=""), Done()]

    class Responses:
        def create(self, **kw):
            seen["tools"].append({t["name"] for t in kw["tools"]})
            return responses.pop(0)

    async def execute(ctx, user_id, message_id, tool_name, tool_input, context_type, context_id, call_index):
        seen["executed"].append((message_id, tool_name, context_type, context_id, call_index))
        return {"ok": True, "action_id": "act-1"}

    monkeypatch.setattr(ai_chat_agent, "_foundry_openai_client", lambda: SimpleNamespace(responses=Responses()))
    monkeypatch.setattr(ai_chat_agent, "execute_safe_tool", execute)
    offer = tool_offer.for_actor(AGENT, "client", {"sms": SMS_GAP})
    bundle = {"messages": [], "record": None, "attachments": [],
              "assistant": {"context_type": "client", "context_id": "c-1"}, "tool_offer": offer}
    text, actions, _model = asyncio.run(ai_chat_agent._foundry_generate(AGENT, bundle, "assistant-foundry-1", ""))
    assert text == "Saved the note." and len(actions) == 1
    for names in seen["tools"]:
        assert names.isdisjoint(offer.withheld) and "draft_email" in names
    # The executor is keyed on the assistant turn id — the key _generate remembers the offer under.
    assert seen["executed"] == [("assistant-foundry-1", "add_client_note", "client", "c-1", 0)]


def test_the_note_leads_the_foundry_runtime_context(monkeypatch):
    seen = {}

    async def fake_brokerage(_ctx):
        return None

    class FakeMemory:
        def __init__(self, _ctx): pass
        async def inject_jit_prompt(self, _agent, base): return base + "\n\nOPERATOR MEMORY: prefers mornings."

    async def fake_foundry(_ctx, _bundle, _assistant_id, runtime_context, applied=None):
        seen["runtime_context"] = runtime_context
        return ("ok", [], "azure-foundry:test")

    async def gaps(_ctx):
        return {"sms": SMS_GAP}

    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", fake_brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", FakeMemory)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *a, **k: [])
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", False)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "azure-foundry")
    monkeypatch.setattr(ai_chat_agent, "_foundry_generate", fake_foundry)
    monkeypatch.setattr(tool_offer, "channel_gaps", gaps)
    bundle = {"messages": [], "record": None, "attachments": [],
              "assistant": {"context_type": "client", "context_id": "c-1"}}
    asyncio.run(ai_chat_agent._generate(AGENT, bundle, "assistant-foundry-2"))
    rc = seen["runtime_context"]
    # Ahead of the memory block: _foundry_inputs keeps the first 12k characters.
    assert rc.index(tool_offer.PROMPT_LEAD) < rc.index("OPERATOR MEMORY")


def test_a_turn_names_the_gap_in_the_prompt_and_remembers_the_offer_for_the_executor(monkeypatch):
    """_generate end to end, up to the first provider: the system prompt carries
    the note, every provider gets the filtered bundle, and the executor would
    answer from the same offer."""
    seen = {}

    async def fake_brokerage(_ctx):
        return None

    class FakeMemory:
        def __init__(self, _ctx): pass
        async def inject_jit_prompt(self, _agent, base): return base

    async def fake_local(_ctx, bundle, system_prompt, *a, **k):
        seen["prompt"], seen["bundle"] = system_prompt, bundle
        return ("ok", [])

    async def gaps(_ctx):
        return {"sms": SMS_GAP}

    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", fake_brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", FakeMemory)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *a, **k: [])
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", False)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "local")
    monkeypatch.setattr(ai_chat_agent, "_local_fallback", fake_local)
    monkeypatch.setattr(tool_offer, "channel_gaps", gaps)
    bundle = {"messages": [], "record": None, "attachments": [],
              "assistant": {"context_type": "client", "context_id": "c-1"}}
    asyncio.run(ai_chat_agent._generate(AGENT, bundle, "assistant-offer-1"))

    assert tool_offer.PROMPT_LEAD in seen["prompt"]
    assert f"- draft_sms: {SMS_GAP}" in seen["prompt"]
    assert "- get_tenant_health: limited to the broker-owner" in seen["prompt"]
    offer = seen["bundle"]["tool_offer"]
    assert "draft_sms" in offer.withheld and "draft_email" in offer.offered
    assert tool_offer.current(AGENT, "assistant-offer-1", "client") is offer


# ── the turn runs as the person who asked ───────────────────────────────────

@pytest.mark.parametrize("value, role", [
    ("broker_owner", Role.BROKER_OWNER),
    ("platform_admin", Role.BROKER_OWNER),  # never wider than the brokerage
    ("agent", Role.AGENT),
    (None, Role.AGENT),                     # a job queued before the role was recorded
    ("superuser", Role.AGENT),              # anything unexpected is the least it could be
])
def test_the_worker_role_follows_the_requester_but_never_the_platform(value, role):
    assert ai_chat_agent._worker_role(value) is role


def test_the_job_carries_who_asked():
    src = inspect.getsource(ai_chat_api)
    call = src[src.index('job_type="ai_chat:response"'):]
    call = call[:call.index(")\n")]
    assert '"role": ctx.role.value' in call


@pytest.mark.parametrize("live, recorded, role", [
    (("ok", "broker_owner"), "agent", Role.BROKER_OWNER),      # promoted since: the live row wins
    (("ok", "agent"), "broker_owner", Role.AGENT),             # demoted since: never the stale grant
    (("ok", "platform_admin"), None, Role.BROKER_OWNER),       # never wider than the brokerage
    (("missing", None), "broker_owner", Role.AGENT),           # no longer an active member here
    (("missing", None), "platform_admin", Role.BROKER_OWNER),  # never a member: the verified role stands, narrowed
    (("unknown", None), "broker_owner", Role.BROKER_OWNER),    # unreadable: the recorded role stands
    (("unknown", None), None, Role.AGENT),                     # a job queued before roles were recorded
])
def test_the_turn_runs_as_the_requesters_current_role(monkeypatch, live, recorded, role):
    seen = {}

    async def live_role(requester):
        seen["requester"] = requester
        return live

    async def capture(ctx, assistant_id):
        seen["ctx"] = ctx
        return {"assistant": {"status": "completed"}}

    monkeypatch.setattr(ai_chat_agent, "_live_role", live_role)
    monkeypatch.setattr(ai_chat_agent, "load_response_bundle", capture)
    payload = {"tenant_id": TENANT, "user_id": "owner@tenant.test", "assistant_id": "a-1", "request_id": "r-1"}
    if recorded is not None:
        payload["role"] = recorded
    asyncio.run(ai_chat_agent.handle_ai_chat_job(payload, None))
    assert seen["ctx"].role is role and seen["ctx"].tenant_id == TENANT
    # The membership check itself runs as the least the person could be.
    assert seen["requester"].role is Role.AGENT and seen["requester"].agent_id == "owner@tenant.test"


def test_an_unreadable_membership_is_unknown_not_missing(monkeypatch):
    import db.connection as connection

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    assert asyncio.run(ai_chat_agent._live_role(OWNER)) == ("unknown", None)
