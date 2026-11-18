"""A carrier's "completed" with no duration is not narrated as an answer
(external audit 24.8), against real PostgreSQL.

call_status_stage("completed", None) already refused to call such a call
delivered, but the client's timeline said "Call answered and ended". Now the
timeline says "answered" only with positive billed time or after an explicit
answer status for the same session, and the row's meta says whether the answer
was confirmed.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_timeline_says_answered_only_when_the_carrier_did(monkeypatch):
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import commands_api as ca
    from db import connection

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    client = str(uuid.uuid4())
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)

    async def session(key):
        call_id = f"{key}-{s}"
        command = await admin.fetchval(
            "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
            "draft, state, idempotency_key, created_by, provider, provider_reference) "
            "VALUES ($1,'CALL','external','outreach',$2::jsonb,'{}'::jsonb,'succeeded',$3,$4,'plivo',$5) "
            "RETURNING id", T, json.dumps({"phone": "+13025550142", "client_id": client}),
            f"call-{s}-{key}", owner, call_id)
        await admin.execute(
            "INSERT INTO live_call_sessions (tenant_id, command_id, client_id, consent_recorded, consent_basis, "
            "transcript_status, provider_call_id, created_by) VALUES ($1,$2,$3,false,"
            "'awaiting_explicit_live_transcription_consent','pending',$4,$5)", T, command, client, call_id, owner)
        return call_id

    async def timeline(call_id):
        row = await admin.fetchrow("SELECT summary, meta FROM client_activities WHERE tenant_id=$1 "
                                   "AND meta->>'provider_call_id' = $2", uuid.UUID(T), call_id)
        return row["summary"], json.loads(row["meta"]).get("answer_confirmed")

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"w-{s}", f"W {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,$3)", client, T, f"C {s}")

        # The carrier finished the call and said nothing about time or pickup.
        unknown = await session("unknown")
        await ca.accept_call_outcome("plivo", unknown, "completed", None, tenant_id=T)
        assert await timeline(unknown) == ("Call completed; answer not confirmed", False)

        # An explicit answer status was recorded first: then it was answered.
        picked_up = await session("picked-up")
        await ca.accept_call_outcome("plivo", picked_up, "in-progress", None, tenant_id=T)
        await ca.accept_call_outcome("plivo", picked_up, "completed", None, tenant_id=T)
        assert await timeline(picked_up) == ("Call answered and ended", True)

        # Positive billed time is an answer; zero is Plivo's never-answered call.
        timed = await session("timed")
        await ca.accept_call_outcome("plivo", timed, "completed", 41, tenant_id=T)
        assert await timeline(timed) == ("Call answered and ended (0:41)", True)
        zero = await session("zero")
        await ca.accept_call_outcome("plivo", zero, "completed", 0, tenant_id=T)
        assert await timeline(zero) == ("Call not answered", False)

        # started_at alone is not an answer: an agent's transcript telemetry
        # can set it while the call rings.
        typed = await session("typed")
        await admin.execute("UPDATE live_call_sessions SET started_at = now() WHERE provider_call_id=$1", typed)
        await ca.accept_call_outcome("plivo", typed, "completed", None, tenant_id=T)
        assert await timeline(typed) == ("Call completed; answer not confirmed", False)

        # The final status applied before the answer: the late answer
        # corrects the row, once.
        late = await session("late-answer")
        await ca.accept_call_outcome("plivo", late, "completed", None, tenant_id=T)
        assert await timeline(late) == ("Call completed; answer not confirmed", False)
        await ca.accept_call_outcome("plivo", late, "in-progress", None, tenant_id=T)
        assert await timeline(late) == ("Call answered and ended", True)
        await ca.accept_call_outcome("plivo", late, "answered", None, tenant_id=T)
        assert await admin.fetchval("SELECT count(*) FROM client_activities WHERE tenant_id=$1 "
                                    "AND meta->>'provider_call_id' = $2", uuid.UUID(T), late) == 1

        # Twilio's completed is an answer by Twilio's rules (L17-44).
        tw = await session("twilio")
        await ca.accept_call_outcome("twilio", tw, "completed", None, tenant_id=T)
        assert await timeline(tw) == ("Call answered and ended", True)

        busy = await session("busy")
        await ca.accept_call_outcome("plivo", busy, "busy", None, tenant_id=T)
        assert await timeline(busy) == ("Call not connected — line busy", False)
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in await admin.fetch(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                    "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
