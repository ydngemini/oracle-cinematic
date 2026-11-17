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
        assert await commands_api.accept_call_outcome("plivo", call_id, "completed", 42, tenant_id=T) == 0
        assert await commands_api.accept_call_outcome("plivo", call_id, "completed", 42, tenant_id=T) == 0   # a retry: one row
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

        async def timeline(provider_call_id):
            return await admin.fetch("SELECT summary FROM client_activities WHERE tenant_id=$1 "
                                     "AND meta->>'provider_call_id' = $2", uuid.UUID(T), provider_call_id)

        # The worker writes the session; the worker, the webhook's retry and the
        # sweep then race to apply it: exactly once, one timeline entry.
        await make_session(commands[0], call_id)
        raced = await asyncio.gather(commands_api.apply_pending_call_outcomes([call_id]),
                                     commands_api.apply_pending_call_outcomes([call_id]),
                                     commands_api.apply_pending_call_outcomes(only_tenant=T))
        assert sum(raced) == 1
        session = await admin.fetchrow("SELECT ended_at, outcome FROM live_call_sessions WHERE provider_call_id=$1",
                                       call_id)
        assert session["ended_at"] is not None and session["outcome"] == "completed"
        rows = await timeline(call_id)
        assert len(rows) == 1 and "0:42" in rows[0]["summary"]

        # A callback that beats the provider ack knows the call by Neoh's
        # correlation and the carrier's ids; the session is written under the
        # request id. It still applies — the worker's own hook finds it.
        correlation, carrier_call = str(uuid.uuid4()), str(uuid.uuid4())
        assert await commands_api.accept_call_outcome("plivo", correlation, "busy", aliases=[carrier_call, other, correlation], tenant_id=T) == 0
        await make_session(commands[1], other)
        assert await commands_api.apply_pending_call_outcomes([other]) == 1          # the worker's hook
        assert await admin.fetchval("SELECT outcome FROM live_call_sessions WHERE provider_call_id=$1",
                                    other) == "busy"

        # Another brokerage's signed callback naming this brokerage's call id
        # (as an alias it chose) never reaches this session.
        intruder = str(uuid.uuid4())
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", intruder,
                            f"intruder-{s}", f"I {s}")
        try:
            assert await commands_api.accept_call_outcome("plivo", f"x-{s}", "completed", 9,
                                                          aliases=[other, call_id], tenant_id=intruder) == 0
            assert await commands_api.apply_pending_call_outcomes() >= 0
            assert await admin.fetchval("SELECT applied_at FROM provider_call_outcomes WHERE provider_call_id=$1",
                                        f"x-{s}") is None
        finally:
            await admin.execute("DELETE FROM tenants WHERE id=$1", intruder)   # cascades its outcome

        # An outcome for a session that already exists is applied at once.
        assert await commands_api.accept_call_outcome("plivo", other, "completed", 0, tenant_id=T) == 1

        # The sweep, confined to this brokerage, has nothing left.
        swept = await reconciliation.run_sweep(only_tenant=T)
        assert swept["call_outcomes_applied"] == 0
        assert await admin.fetchval(
            "SELECT count(*) FROM provider_call_outcomes WHERE applied_at IS NULL "
            "AND (provider_call_id = ANY($1::text[]) OR call_aliases && $1::text[])",
            [call_id, other, correlation]) == 0

        # Old rows expire; ones that never matched are reported as such.
        await commands_api.accept_call_outcome("plivo", f"lost-{s}", "completed", 5, tenant_id=T)
        await admin.execute("UPDATE provider_call_outcomes SET received_at = now() - interval '31 days' "
                            "WHERE provider_call_id = ANY($1::text[])", [call_id, other, correlation, f"lost-{s}"])
        expired = await commands_api.expire_call_outcomes()
        assert expired["expired_applied"] >= 3 and expired["expired_unmatched"] >= 1
    finally:
        await admin.execute("DELETE FROM provider_call_outcomes WHERE provider_call_id = ANY($1::text[]) "
                            "OR call_aliases && $1::text[]", [call_id, other, f"lost-{s}"])
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
