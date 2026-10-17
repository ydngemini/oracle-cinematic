"""The one precise question's choices reach the chat UI and come back as the
next turn's context (prompt 05, T05.10 / T05.04; ledger L05-09).

The assistant row keeps the record ids only (metadata is plaintext, so never a
name); the turn-end frame carries the labels the turn already had; listing
the thread re-reads the labels as the caller, so a record they can no longer
open is not offered again.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import ai_chat_agent
import ai_chat_store
import semantic_turn
from tenancy import Role, TenantContext

TENANT = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="a@t.test", tenant_id=TENANT, role=Role.AGENT)
SARAH_A, SARAH_B = str(uuid.uuid4()), str(uuid.uuid4())
QUESTION = semantic_turn.Question("Sarah", (
    semantic_turn.Choice("client", SARAH_A, "Sarah Chen"), semantic_turn.Choice("client", SARAH_B, "Sarah Lee")))


class _Conn:
    def __init__(self, rows=()):
        self.rows, self.statements = list(rows), []

    async def execute(self, sql, *args):
        self.statements.append((" ".join(sql.split()), args))
        return "UPDATE 1"

    async def fetch(self, sql, *_a):
        return self.rows if "FROM ai_chat_messages" in sql else []

    async def fetchrow(self, *_a):
        return None


def _tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn
    return tx


def test_update_assistant_writes_the_ids_only_and_leaves_metadata_alone_without_choices(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(ai_chat_store, "tenant_tx", _tx(conn))
    monkeypatch.setattr(ai_chat_store, "tenant_key", lambda _ctx: "k" * 32)

    async def seal(_conn, text, _key):
        return text.encode()

    monkeypatch.setattr(ai_chat_store, "_encrypt_text", seal)
    choices = [{"type": "client", "id": SARAH_A, "label": "Sarah Chen"}, {"type": "client", "id": SARAH_B, "label": "Sarah Lee"}]
    asyncio.run(ai_chat_store.update_assistant(CTX, "asst-1", content="Which Sarah?", status_value="completed",
                                               model_id="m", choices=choices))
    sql, args = conn.statements[-1]
    assert "jsonb_build_object('choices', $8::jsonb)" in sql and "CASE WHEN $8::jsonb IS NULL THEN metadata" in sql
    assert json.loads(args[7]) == [{"type": "client", "id": SARAH_A}, {"type": "client", "id": SARAH_B}]
    assert "Sarah" not in args[7], "names never reach plaintext metadata"
    asyncio.run(ai_chat_store.update_assistant(CTX, "asst-2", content="Done.", status_value="completed"))
    assert conn.statements[-1][1][7] is None


def test_the_turn_end_carries_the_labels_and_the_row_the_ids(monkeypatch):
    import ws_hub

    seen = {"frames": [], "updates": []}

    async def live_role(_requester):
        return "unknown", None

    async def load(_ctx, _assistant_id):
        return {"assistant": {"status": "pending", "context_type": "client", "context_id": SARAH_A},
                "messages": [{"role": "user", "content": "Text Sarah"}], "record": None, "attachments": []}

    async def generate(_ctx, bundle, _assistant_id):
        bundle["question"] = QUESTION
        return "Which Sarah — Sarah Chen or Sarah Lee?", [], "fake:model"

    async def update(ctx, assistant_id, **kw):
        seen["updates"].append(kw)

    async def chunks(*_a, **_k):
        return None

    class Memory:
        def __init__(self, _ctx): pass
        async def record_interaction(self, *_a, **_k): return None

    async def broadcast(_tenant, _user, frame):
        seen["frames"].append(frame)

    monkeypatch.setattr(ai_chat_agent, "_live_role", live_role)
    monkeypatch.setattr(ai_chat_agent, "load_response_bundle", load)
    monkeypatch.setattr(ai_chat_agent, "_generate", generate)
    monkeypatch.setattr(ai_chat_agent, "update_assistant", update)
    monkeypatch.setattr(ai_chat_agent, "_broadcast_chunks", chunks)
    monkeypatch.setattr(ai_chat_agent, "SessionManager", Memory)
    monkeypatch.setattr(ws_hub, "broadcast_user", broadcast)
    monkeypatch.setattr(ai_chat_agent, "release_concurrency", chunks, raising=False)
    payload = {"tenant_id": TENANT, "user_id": CTX.agent_id, "assistant_id": "asst-q", "request_id": "r-1", "role": "agent"}
    asyncio.run(ai_chat_agent.handle_ai_chat_job(payload, None))

    done = [u for u in seen["updates"] if u.get("status_value") == "completed"]
    assert done[-1]["choices"] == [{"type": "client", "id": SARAH_A}, {"type": "client", "id": SARAH_B}]
    complete = [f for f in seen["frames"] if f["type"] == "AI_CHAT_COMPLETE"]
    assert complete[-1]["choices"] == [
        {"type": "client", "id": SARAH_A, "label": "Sarah Chen"}, {"type": "client", "id": SARAH_B, "label": "Sarah Lee"}]

    async def no_question(_ctx, bundle, _assistant_id):
        return "Done.", [], "fake:model"

    monkeypatch.setattr(ai_chat_agent, "_generate", no_question)
    asyncio.run(ai_chat_agent.handle_ai_chat_job({**payload, "assistant_id": "asst-plain"}, None))
    assert seen["updates"][-1]["choices"] is None and seen["frames"][-1]["choices"] == []


def test_listing_the_thread_relabels_the_choices_as_the_caller_and_drops_what_they_cannot_open(monkeypatch):
    now = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)

    def row(**over):
        base = {"id": uuid.uuid4(), "request_id": uuid.uuid4(), "role": "assistant", "content_ciphertext": b"x",
                "status": "completed", "context_type": None, "context_id": None, "attachments": "[]",
                "model_id": "m", "error_code": None, "created_at": now, "updated_at": now, "metadata": {}}
        base.update(over)
        return base

    rows = [row(metadata={"choices": [{"type": "client", "id": SARAH_A}, {"type": "client", "id": SARAH_B},
                                      {"type": "client"}]}),
            row(role="user", metadata={})]
    conn = _Conn(rows)
    monkeypatch.setattr(ai_chat_store, "tenant_tx", _tx(conn))
    monkeypatch.setattr(ai_chat_store, "tenant_key", lambda _ctx: "k" * 32)

    async def open_(_conn, _cipher, _key):
        return "text"

    async def resolve(_conn, _ctx, rtype, rid):
        if rid == SARAH_B:
            raise LookupError("no longer visible")
        return {"id": rid, "label": "Sarah Chen"}

    monkeypatch.setattr(ai_chat_store, "_decrypt_text", open_)
    monkeypatch.setattr(ai_chat_store, "resolve_record", resolve)
    out = asyncio.run(ai_chat_store.list_messages(CTX, limit=10))
    assistant = [m for m in out if m["role"] == "assistant"][0]
    assert assistant["choices"] == [{"type": "client", "id": SARAH_A, "label": "Sarah Chen"}]
    assert [m for m in out if m["role"] == "user"][0]["choices"] == []
