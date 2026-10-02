"""A chat turn whose worker died mid-turn is never regenerated.

Worker-kill drill (2026-10-02): the turn's job was re-claimed when its lease
lapsed and the whole turn ran again. The first attempt may already have run
CRM tool writes, and the user had stopped waiting. A re-claimed turn found
`streaming` now ends with an honest product error instead.
"""

from __future__ import annotations

import asyncio

import ai_chat_agent


def _patch(monkeypatch, status):
    calls = {"generate": 0, "updates": [], "broadcasts": [], "released": 0}

    async def bundle(_ctx, _aid):
        return {"assistant": {"status": status}, "messages": [{"content": "hi"}]}

    async def generate(*_a, **_kw):
        calls["generate"] += 1
        return "answer", [], "model"

    async def update(_ctx, _aid, **kw):
        calls["updates"].append(kw)

    async def broadcast(_t, _u, msg):
        calls["broadcasts"].append(msg)

    async def release(_ctx):
        calls["released"] += 1

    monkeypatch.setattr(ai_chat_agent, "load_response_bundle", bundle)
    monkeypatch.setattr(ai_chat_agent, "_generate", generate)
    monkeypatch.setattr(ai_chat_agent, "update_assistant", update)
    monkeypatch.setattr(ai_chat_agent.ws_hub, "broadcast_user", broadcast)
    monkeypatch.setattr(ai_chat_agent, "release_concurrency", release)
    return calls


PAYLOAD = {"tenant_id": "00000000-0000-0000-0000-0000000000aa", "user_id": "agent-1",
           "assistant_id": "11111111-1111-1111-1111-111111111111", "request_id": "r1"}


def test_interrupted_turn_is_failed_not_regenerated(monkeypatch):
    calls = _patch(monkeypatch, "streaming")
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["status"] == "interrupted"
    assert calls["generate"] == 0
    assert calls["updates"][-1]["status_value"] == "failed"
    assert calls["updates"][-1]["error_code"] == "AI_RESPONSE_INTERRUPTED"
    assert calls["broadcasts"][-1]["type"] == "AI_CHAT_ERROR"
    assert calls["released"] == 1


def test_already_failed_turn_is_left_alone(monkeypatch):
    calls = _patch(monkeypatch, "failed")
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert out["status"] == "failed"
    assert calls["generate"] == 0 and calls["updates"] == [] and calls["broadcasts"] == []


def test_pending_turn_still_runs(monkeypatch):
    calls = _patch(monkeypatch, "pending")

    async def no_chunks(*_a, **_kw):
        return None

    monkeypatch.setattr(ai_chat_agent, "_broadcast_chunks", no_chunks)

    class _Sessions:
        def __init__(self, _ctx):
            pass

        async def record_interaction(self, *_a):
            return None

    monkeypatch.setattr(ai_chat_agent, "SessionManager", _Sessions)
    out = asyncio.run(ai_chat_agent.handle_ai_chat_job(PAYLOAD, reporter=None))
    assert calls["generate"] == 1 and out["model_id"] == "model"
