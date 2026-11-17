"""A carrier's call outcome that arrives before its call session is kept and
applied later, never lost (external audit 24.4), against real PostgreSQL.

The worker writes live_call_sessions only after the provider acknowledges the
call. A verified Plivo status callback that arrived first updated zero rows,
wrote no timeline entry, and was acknowledged (204): the outcome was gone. Now
the callback stores it in provider_call_outcomes before answering, and it is
applied when the session exists — by the worker, or by the reconciliation
sweep — once, in arrival order.

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


def test_an_early_call_outcome_waits_for_its_session():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import commands_api
    import reconciliation
    from db import connection

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    client = str(uuid.uuid4())
    call_id, other = f"req-{s}", f"req2-{s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"calls-{s}", f"B {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,$3)",
                                client, T, f"X {s}")
            commands = []
            for key in ("a", "b"):
                commands.append(await admin.fetchval(
                    "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
                    "draft, state, idempotency_key, created_by, provider, provider_reference) "
                    "VALUES ($1,'CALL','external','outreach',$2::jsonb,'{}'::jsonb,'executing',$3,$4,'plivo',$5) "
                    "RETURNING id", T, json.dumps({"phone": "+13025550142", "client_id": client}),
                    f"call-{s}-{key}", owner, call_id if key == "a" else other))

        # The carrier's 'completed' beats the session row: stored, nothing applied yet.
        assert await commands_api.accept_call_outcome("plivo", call_id, "completed", 42) == 0
        assert await commands_api.accept_call_outcome("plivo", call_id, "completed", 42) == 0   # a retry: one row
        assert await admin.fetchval("SELECT count(*) FROM provider_call_outcomes WHERE provider_call_id=$1 "
                                    "AND applied_at IS NULL", call_id) == 1

        # Before the fix this is all that happened: nothing to update.
        assert await admin.fetchval("SELECT count(*) FROM live_call_sessions WHERE provider_call_id=$1",
                                    call_id) == 0

        async def make_session(command_id, provider_call_id):
            await admin.execute(
                "INSERT INTO live_call_sessions (tenant_id, command_id, client_id, consent_recorded, consent_basis, "
                "transcript_status, provider_call_id, created_by) VALUES ($1,$2,$3,false,"
                "'awaiting_explicit_live_transcription_consent','pending',$4,$5)",
                T, command_id, client, provider_call_id, owner)

        # The worker writes the session, then applies what was waiting.
        await make_session(commands[0], call_id)
        assert await commands_api.apply_pending_call_outcomes(call_id) == 1
        session = await admin.fetchrow("SELECT ended_at, outcome FROM live_call_sessions WHERE provider_call_id=$1",
                                       call_id)
        assert session["ended_at"] is not None and session["outcome"] == "completed"
        timeline = await admin.fetch("SELECT summary, meta FROM client_activities WHERE tenant_id=$1 "
                                     "AND meta->>'provider_call_id' = $2", uuid.UUID(T), call_id)
        assert len(timeline) == 1 and "0:42" in timeline[0]["summary"]
        assert await commands_api.apply_pending_call_outcomes(call_id) == 0          # applied once

        # A second call: two early statuses, applied in arrival order by the sweep.
        await commands_api.accept_call_outcome("plivo", other, "in-progress")
        await commands_api.accept_call_outcome("plivo", other, "no-answer")
        await make_session(commands[1], other)
        swept = await reconciliation.run_sweep(only_tenant=T)
        assert swept["call_outcomes_applied"] >= 2
        assert await admin.fetchval("SELECT outcome FROM live_call_sessions WHERE provider_call_id=$1",
                                    other) == "no-answer"
        assert await admin.fetchval("SELECT count(*) FROM provider_call_outcomes WHERE provider_call_id = "
                                    "ANY($1::text[]) AND applied_at IS NULL", [call_id, other]) == 0

        # The app role may use the table (grants), and old rows expire.
        await admin.execute("UPDATE provider_call_outcomes SET received_at = now() - interval '31 days' "
                            "WHERE provider_call_id = ANY($1::text[])", [call_id, other])
        assert await commands_api.expire_call_outcomes() >= 3
        assert await admin.fetchval("SELECT count(*) FROM provider_call_outcomes WHERE provider_call_id = "
                                    "ANY($1::text[])", [call_id, other]) == 0
    finally:
        await admin.execute("DELETE FROM provider_call_outcomes WHERE provider_call_id = ANY($1::text[])",
                            [call_id, other])
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
