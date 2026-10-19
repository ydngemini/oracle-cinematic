"""A name the parser could not place is looked up AS THE CALLER (T05.10,
AC05.03, L05-10), against real PostgreSQL with FORCE RLS as the application
login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Two clients named Sarah: agent A's and agent B's. Agent A says "Text Sarah"
with nothing selected: the real people search, run as A, finds only A's Sarah,
so the mention resolves to her and nothing is asked — B's Sarah is never a
candidate, never named, never counted. The broker owner, who can open both,
gets the one precise question naming both.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_lookup_sees_only_what_the_caller_may_open(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-lookup-master-key-0123456789abcdef")
    monkeypatch.delenv("ORACLE_SEMANTIC_FRAME_ENABLED", raising=False)
    asyncio.run(_scenario())


class _Gateway:
    """The parser's answer for "Text Sarah": a person it could not place."""

    async def complete(self, prompt, **kwargs):
        ev = [{"source": "utterance", "quote": "Sarah"}]
        return json.dumps({
            "goals": [{"label": "send_message", "negated": False, "evidence": [{"source": "utterance", "quote": "Text Sarah"}]}],
            "mentions": [{"text": "Sarah", "kind": "person", "known": [], "evidence": ev}],
            "times": [], "constraints": [], "scope": "mine", "required_data": [],
            "effect": "external_effect", "unfilled_slots": [], "ambiguities": [],
        })


async def _scenario():
    import asyncpg

    import semantic_turn
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = str(uuid.uuid4())
    tag = tenant[:8]
    agent = {k: f"{k}-{tag}@lookup.test" for k in ("owner", "a", "b")}
    sarah = {"a": uuid.uuid4(), "b": uuid.uuid4()}

    def ctx(who):
        return TenantContext(agent_id=agent[who], tenant_id=tenant,
                             role=Role.BROKER_OWNER if who == "owner" else Role.AGENT)

    def bundle():
        return {"assistant": {"context_type": None, "context_id": None, "id": str(uuid.uuid4())},
                "record": None, "attachments": [],
                "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Text Sarah about the showing"}]}

    previous = semantic_turn.GATEWAY
    semantic_turn.GATEWAY = _Gateway()
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Lookup')", uuid.UUID(tenant), f"lookup-{tag}")
        for who in agent:
            await admin.execute(
                "INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,$3,$2)",
                uuid.UUID(tenant), agent[who], "broker_owner" if who == "owner" else "agent")
        for who, cid in sarah.items():
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                cid, uuid.UUID(tenant), f"Sarah {'Chen' if who == 'a' else 'Lee'} {tag}", agent[who])

        # Agent A: only their own Sarah is found, so she is the answer.
        turn = await semantic_turn.prepare_turn(ctx("a"), bundle(), str(uuid.uuid4()))
        (mention,) = turn.frame.mentions
        assert [(c.record_type, str(c.id)) for c in mention.candidates] == [("client", str(sarah["a"]))]
        assert turn.question is None and turn.pending() is None
        # B's Sarah never reached the frame: not a candidate, not even a dropped count.
        assert turn.frame.dropped_candidates == 0
        assert str(sarah["b"]) not in json.dumps(turn.summary())

        # The owner can open both: one precise question naming both.
        turn = await semantic_turn.prepare_turn(ctx("owner"), bundle(), str(uuid.uuid4()))
        assert turn.question is not None
        assert {c.id for c in turn.question.choices} == {str(sarah["a"]), str(sarah["b"])}
        assert {c.label for c in turn.question.choices} == {f"Sarah Chen {tag}", f"Sarah Lee {tag}"}
        assert turn.pending().records == {str(sarah["a"]), str(sarah["b"])}
        assert "Sarah" not in json.dumps(turn.summary()), "the stored summary never carries a name"
    finally:
        semantic_turn.GATEWAY = previous
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("clients", "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
