"""AI32-01: once a turn's answer is committed as completed, nothing after it
(the session-memory mirror, the completion frame) can turn it into a failed
turn. Before: a memory write that raised sent the turn down the failure path,
which overwrote the saved answer with an error and told the user to retry."""

from __future__ import annotations

import asyncio

import ai_chat_agent

PAYLOAD = {"tenant_id": "00000000-0000-0000-0000-0000000000aa", "user_id": "agent-1",
           "assistant_id": "11111111-1111-1111-1111-111111111111", "request_id": "r1"}


def _patch(monkeypatch, *, memory_failures=None, broadcast_fails=False, generate_fails=False,
           commit_then_raise=False):
    calls = {"updates": [], "broadcasts": [], "memory": []}
    failures = dict(memory_failures or {})

    async def bundle(_ctx, _aid):
        return {"assistant": {"status": "pending"}, "messages": [{"content": "move Sam to warm"}]}

    async def generate(*_a, **_kw):
        if generate_fails:
            raise RuntimeError("provider down")
        return "Done: Sam is warm.", [{"tool": "update_client"}], "model-x"

    async def update(_ctx, _aid, **kw):
        calls["updates"].append(kw)
        if commit_then_raise and kw["status_value"] == "completed":
            raise ConnectionResetError("lost after COMMIT")
        if kw.get("unless_status") == "completed" and commit_then_raise:
            return False  # the row is already completed
        return True

    async def broadcast(_t, _u, msg):
        if broadcast_fails and msg.get("type") == "AI_CHAT_COMPLETE":
            raise ConnectionError("socket gone")
        calls["broadcasts"].append(msg)

    async def nothing(*_a, **_kw):
        return None

    class Sessions:
        def __init__(self, _ctx):
            pass

        async def record_interaction(self, _user, role, text):
            calls["memory"].append(role)
            if failures.get(role, 0) > 0:
                failures[role] -= 1
                raise OSError("memory store unavailable")

    monkeypatch.setattr(ai_chat_agent, "load_response_bundle", bundle)
    monkeypatch.setattr(ai_chat_agent, "_generate", generate)
    monkeypatch.setattr(ai_chat_agent, "update_assistant", update)
    monkeypatch.setattr(ai_chat_agent.ws_hub, "broadcast_user", broadcast)
    monkeypatch.setattr(ai_chat_agent, "release_concurrency", nothing)
    monkeypatch.setattr(ai_chat_agent, "_broadcast_chunks", nothing)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", Sessions)
    monkeypatch.setattr(ai_chat_agent, "MEMORY_MIRROR_RETRY_SECONDS", 0)
    return calls


def _statuses(calls):
    return [u["status_value"] for u in calls["updates"]]


def test_a_memory_write_that_keeps_failing_leaves_the_answer_completed(monkeypatch):
    calls = _patch(monkeypatch, memory_failures={"assistant": 99})
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["action_count"] == 1
    assert _statuses(calls) == ["streaming", "completed"], "never rewritten to failed"
    assert [b["type"] for b in calls["broadcasts"]] == ["AI_CHAT_COMPLETE"]
    assert calls["memory"] == ["user", "assistant", "assistant"], "the failed write was retried once"


def test_a_memory_write_that_fails_once_is_retried(monkeypatch):
    calls = _patch(monkeypatch, memory_failures={"user": 1})
    asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert calls["memory"] == ["user", "user", "assistant"]
    assert _statuses(calls) == ["streaming", "completed"]


def test_a_lost_completion_frame_does_not_fail_the_turn(monkeypatch):
    calls = _patch(monkeypatch, broadcast_fails=True)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["model_id"] == "model-x"
    assert _statuses(calls) == ["streaming", "completed"]
    assert all(b["type"] != "AI_CHAT_ERROR" for b in calls["broadcasts"])


def test_a_failure_before_the_answer_is_saved_still_fails_but_cannot_overwrite_a_completed_row(monkeypatch):
    calls = _patch(monkeypatch, generate_fails=True)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["status"] == "failed"
    failed = calls["updates"][-1]
    assert failed["status_value"] == "failed" and failed["unless_status"] == "completed"
    assert calls["broadcasts"][-1]["type"] == "AI_CHAT_ERROR"


def test_a_completed_write_that_raised_after_committing_is_not_reported_as_failed(monkeypatch):
    """AI32-01 review: the guarded failure update finds the row completed; the
    person must get a completion, not "try again" after tool writes applied."""
    calls = _patch(monkeypatch, commit_then_raise=True)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["status"] == "completed"
    assert calls["updates"][-1]["unless_status"] == "completed"
    assert [b["type"] for b in calls["broadcasts"]] == ["AI_CHAT_COMPLETE"]


def test_the_completion_frame_goes_out_before_the_memory_mirror(monkeypatch):
    calls = _patch(monkeypatch)
    order = []
    original = ai_chat_agent._mirror_turn_to_memory

    async def mirror(*a, **kw):
        order.append(("mirror", len(calls["broadcasts"])))
        return await original(*a, **kw)

    monkeypatch.setattr(ai_chat_agent, "_mirror_turn_to_memory", mirror)
    asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert order == [("mirror", 1)], "AI_CHAT_COMPLETE was already sent when the mirror ran"
