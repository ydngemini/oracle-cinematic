"""Stated constraints, stored and enforced as the person asking — against real
PostgreSQL with FORCE RLS, as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 05, AC05.04 / T05.04 / T05.07; ledger L05-03. No model is called: the
parser's gateway is a fake. What is real: `authorize_frame` and the discourse
re-read run in agent A's own RLS context (records follow 0131-0150), the frame
summary is written to A's own assistant row, a refused tool call is recorded in
ai_tool_operations as A, and agent B can neither write A's summary nor read A's
refusal.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


class _Gateway:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    async def complete(self, prompt, **_kw):
        self.prompts.append(prompt)
        return self.answer


def test_constraints_are_stored_and_enforced_as_the_caller(monkeypatch):
    import ai_chat_agent
    from ai_tool_policy import TOOL_RISK

    monkeypatch.delenv("ORACLE_SEMANTIC_FRAME_ENABLED", raising=False)
    monkeypatch.setattr(ai_chat_agent, "offered_tool_names", lambda _ct: frozenset(TOOL_RISK))
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, top to bottom
    import asyncpg

    import ai_chat_store
    import semantic_turn
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    agent = {k: f"{k}-{tag}@constraint.test" for k in ("a", "b")}
    ctx = {k: TenantContext(agent_id=agent[k], tenant_id=tenant, role=Role.AGENT) for k in agent}
    client_a, client_b = uuid.uuid4(), uuid.uuid4()
    assistant, prior = uuid.uuid4(), uuid.uuid4()
    name_b = f"Bee {tag}"

    text = "Don't message her yet"
    ev = lambda q: [{"source": "utterance", "quote": q}]  # noqa: E731
    answer = json.dumps({
        "goals": [{"label": "send_message", "negated": True, "evidence": ev("Don't message her")}],
        "mentions": [{"text": "her", "kind": "person", "known": [1], "evidence": ev("her")}],
        "times": [],
        "constraints": [{"kind": "no_send", "text": "not yet", "time": None, "mention": 0,
                         "evidence": ev("Don't message her yet")}],
        "scope": "mine", "required_data": [], "effect": "read",
        "unfilled_slots": [], "ambiguities": [],
    })
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Constraint live')",
                            tenant, f"constraint-{tag}")
        for k in agent:
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,'agent',$3,'scrypt$x$y',$4,true)",
                uuid.uuid4(), tenant, agent[k], f"Agent {k.upper()}")
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
            client_a, tenant, f"Ay {tag}", agent["a"])
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
            client_b, tenant, name_b, agent["b"])
        for mid, role in ((prior, "assistant"), (assistant, "assistant")):
            await admin.execute(
                "INSERT INTO ai_chat_messages (id, tenant_id, user_id, role, content_ciphertext, status, "
                "request_id, context_type, context_id) VALUES ($1,$2,$3,$4,'\\x00'::bytea,'streaming',$5,"
                "'client',$6)", mid, tenant, agent["a"], role, uuid.uuid4(), client_a)

        # The previous turn (as if from a stale or forged summary) names B's
        # client. It is re-read as A, so A's parser is never offered it.
        bundle = {
            "assistant": {"context_type": "client", "context_id": str(client_a)},
            "record": {"label": f"Ay {tag}"},
            "messages": [
                {"id": str(prior), "role": "assistant", "content": "ok", "metadata": {
                    "semantic_frame": {"active": [], "mentions": [
                        {"kind": "person", "records": [{"type": "client", "id": str(client_b)}]}]}}},
                {"id": str(uuid.uuid4()), "role": "user", "content": text},
            ],
            "attachments": [],
        }
        gw = _Gateway(answer)
        monkeypatch.setattr(semantic_turn, "GATEWAY", gw)
        turn = await semantic_turn.prepare_turn(ctx["a"], bundle, str(assistant))
        assert turn.status == "parsed" and turn.frame.authorized
        assert name_b not in gw.prompts[0], "a record A cannot open was offered to A's parser"
        [c] = turn.active
        assert c.kind == "no_send" and c.records == (str(client_a),)

        stored = await admin.fetchval(
            "SELECT metadata->'semantic_frame' FROM ai_chat_messages WHERE id=$1", assistant)
        stored = json.loads(stored)
        assert stored["active"][0]["records"] == [str(client_a)]
        assert "Ay" not in json.dumps(stored) and text not in json.dumps(stored)

        # B cannot write A's discourse (RLS: the row is A's).
        await semantic_turn._persist(ctx["b"], str(assistant), {"active": []})
        again = json.loads(await admin.fetchval(
            "SELECT metadata->'semantic_frame' FROM ai_chat_messages WHERE id=$1", assistant))
        assert again == stored

        # The chokepoint refuses as A and records it in the ledger as A.
        semantic_turn.bind(str(assistant), turn.active)
        receipt = await ai_chat_store.execute_safe_tool(
            ctx["a"], agent["a"], str(assistant), "draft_sms",
            {"client_id": str(client_a), "body": "Hi"}, "client", str(client_a), 0)
        assert receipt["ok"] is False and receipt["refused"] and receipt["constraint"] == "no_send"
        row = await admin.fetchrow(
            "SELECT user_id, tool_name, status, error_code FROM ai_tool_operations "
            "WHERE assistant_id=$1 AND call_index=0", assistant)
        assert dict(row) == {"user_id": agent["a"], "tool_name": "draft_sms",
                             "status": "failed", "error_code": "FRAME_NO_SEND"}
        assert await admin.fetchval(
            "SELECT count(*) FROM command_executions WHERE tenant_id=$1", uuid.UUID(tenant)) == 0

        async with connection.tenant_tx(ctx["b"]) as conn:
            seen = await conn.fetchval(
                "SELECT count(*) FROM ai_tool_operations WHERE assistant_id=$1", assistant)
        assert seen == 0, "B read A's refusal"
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
