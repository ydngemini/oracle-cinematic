"""AI32-03: a staged approval (a text, email, call or event awaiting the
person's approval) is a durable side effect like an applied record change.
Once a tier has staged one, no later provider tier may regenerate the turn
and stage another; the reply says what was staged."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import ai_chat_agent
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="owner@a.test", tenant_id="aaaaaaaa-0000-0000-0000-00000000000a",
                    role=Role.BROKER_OWNER)


def _bundle():
    return {"messages": [], "record": None, "attachments": [],
            "assistant": {"context_type": None, "context_id": None}}


def _ladder(monkeypatch, first_tier):
    calls = []

    async def no_brokerage(_ctx):
        return None

    class Memory:
        def __init__(self, _ctx):
            pass

        async def inject_jit_prompt(self, _agent, base):
            return base

    async def local(_ctx, bundle, _prompt, _aid, applied=None, **kw):
        calls.append(kw.get("gateway_provider").name)
        if len(calls) == 1:
            return await first_tier(bundle, applied)
        return "second tier answer", []

    providers = [SimpleNamespace(name="p1", model="m1"), SimpleNamespace(name="p2", model="m2")]
    monkeypatch.setattr(ai_chat_agent, "_resolve_brokerage_context", no_brokerage)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", Memory)
    monkeypatch.setattr(ai_chat_agent, "_gateway_chat_providers", lambda *_a, **_k: providers)
    monkeypatch.setattr(ai_chat_agent, "FIREWORKS_ENABLED", False)
    monkeypatch.setattr(ai_chat_agent, "AI_PROVIDER", "local")
    monkeypatch.setattr(ai_chat_agent, "_local_fallback", local)
    text, actions, _model = asyncio.run(ai_chat_agent._generate(CTX, _bundle(), "assistant-1"))
    return text, actions, calls


def test_a_tier_that_fails_after_staging_ends_the_ladder(monkeypatch):
    async def stage_then_fail(bundle, applied):
        ai_chat_agent._note_staged(bundle, {"ok": True, "command_id": "cmd-1", "approval_id": "ap-1", "sent": False})
        raise RuntimeError("provider dropped the stream")

    text, actions, calls = _ladder(monkeypatch, stage_then_fail)
    assert calls == ["p1"], "the second provider must not regenerate the turn"
    assert actions == [] and "staged for your approval" in text


def test_applied_and_staged_effects_are_both_named(monkeypatch):
    async def apply_stage_fail(bundle, applied):
        applied.append({"ok": True, "action_id": "a-1"})
        ai_chat_agent._note_staged(bundle, {"ok": True, "command_id": "cmd-2"})
        raise RuntimeError("timeout")

    text, actions, calls = _ladder(monkeypatch, apply_stage_fail)
    assert calls == ["p1"] and actions == [{"ok": True, "action_id": "a-1"}]
    assert "applied" in text and "staged for your approval" in text


def test_a_tier_that_failed_before_any_effect_still_falls_through(monkeypatch):
    async def fail(bundle, applied):
        raise RuntimeError("503")

    text, _actions, calls = _ladder(monkeypatch, fail)
    assert calls == ["p1", "p2"] and text == "second tier answer"


def test_only_a_successful_staging_counts():
    bundle = {}
    ai_chat_agent._note_staged(bundle, {"ok": False, "command_id": "x"})
    ai_chat_agent._note_staged(bundle, {"ok": True, "result": "read"})
    ai_chat_agent._note_staged(bundle, "not a receipt")
    assert not ai_chat_agent._turn_has_effects(bundle, [])
    ai_chat_agent._note_staged(bundle, {"ok": True, "approval_id": "ap-9"})
    assert bundle["staged_effects"] == ["ap-9"] and ai_chat_agent._turn_has_effects(bundle, [])


def test_every_tool_execution_site_records_staged_effects():
    """A new tier that runs tools must record what it staged, or the ladder
    cannot see it."""
    source = Path(ai_chat_agent.__file__).read_text()
    sites = len(re.findall(r"receipt = await execute_safe_tool\(", source))
    assert sites >= 3 and source.count("_note_staged(bundle, receipt)") == sites
