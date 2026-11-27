"""A Twilio outbound call's status is stored and applied through
record_call_outcome, like Plivo's (external audit 24.3), against real
PostgreSQL.

Before the fix the Twilio status route only ran an UPDATE on
live_call_sessions: nothing reached provider_call_outcomes, the client's
timeline or the first-response ledger, and a status that beat the session row
updated nothing and was acknowledged.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


async def _signature_checked(*_a, **_k):
    """Stands in for _verify_outbound_twilio_signature (its own tests:
    test_twilio_tenant_signatures.py)."""
    return None


class _Request:
    def __init__(self, form):
        from starlette.datastructures import FormData

        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": "sig", "host": "internal.invalid"}
        self.query_params = {}
        self.url = SimpleNamespace(netloc="internal.invalid", scheme="http", query=urlencode({}))

    async def form(self):
        return self._form


def test_a_twilio_call_outcome_reaches_the_session_and_the_timeline(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import commands_api
    import twilio_voice_routes
    import speed_to_lead
    import twilio_call_handler
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    client = str(uuid.uuid4())
    sid = "CA" + secrets.token_hex(16)
    early = "CA" + secrets.token_hex(16)
    reordered = "CA" + secrets.token_hex(16)
    states = {sid: {"tenant_id": T, "direction": "outbound"},
              reordered: {"tenant_id": T, "direction": "outbound"}}
    answered = []

    async def load_state(call_sid, **_k):
        return states.get(call_sid)

    async def cleanup(call_sid):
        states.pop(call_sid, None)

    real_answered = commands_api._record_call_answered

    async def spy_answered(call_id):
        answered.append(call_id)
        await real_answered(call_id)

    monkeypatch.setattr(twilio_voice_routes, "_verify_outbound_twilio_signature", _signature_checked)
    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", load_state)
    monkeypatch.setattr(twilio_call_handler, "cleanup_twilio_call", cleanup)
    monkeypatch.setattr(commands_api, "_record_call_answered", spy_answered)

    async def post(form):
        return (await commands_api.twilio_status_webhook(_Request(form))).status_code

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"tw-{s}", f"B {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,$3)",
                                client, T, f"X {s}")
            commands = []
            for key, call_sid in (("a", sid), ("b", reordered)):
                commands.append(await admin.fetchval(
                    "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, "
                    "target, draft, state, idempotency_key, created_by, provider, provider_reference) "
                    "VALUES ($1,'CALL','external','outreach',$2::jsonb,'{}'::jsonb,'succeeded',$3,$4,'twilio',$5) "
                    "RETURNING id", T, json.dumps({"phone": "+13025550142", "client_id": client}),
                    f"call-{s}-{key}", owner, call_sid))
            command = commands[0]
        # A speed-to-lead first response for this call: the answer delivers it
        # and meters lead_engaged, once (the audit's headline effect).
        worker = TenantContext(agent_id="command-worker", tenant_id=T, role=Role.PLATFORM_ADMIN)
        await speed_to_lead._record_response_event(
            worker, intake_event_id=str(uuid.uuid4()), contact_id=None, client_id=None, lead_id=None,
            channel="voice", disposition="staged", command_id=str(command),
            lead_created_at=datetime.now(timezone.utc) - timedelta(seconds=30))

        async def engaged():
            return await admin.fetchval("SELECT count(*) FROM billing_usage_events WHERE tenant_id=$1 "
                                        "AND metric='lead_engaged'", uuid.UUID(T))

        # The answer beats the worker's session row: stored, nothing applied.
        assert await post({"CallSid": sid, "CallStatus": "in-progress"}) == 204
        assert await admin.fetchval("SELECT count(*) FROM provider_call_outcomes WHERE provider='twilio' "
                                    "AND provider_call_id=$1 AND tenant_id=$2 AND applied_at IS NULL",
                                    sid, uuid.UUID(T)) == 1

        await admin.execute(
            "INSERT INTO live_call_sessions (tenant_id, command_id, client_id, consent_recorded, consent_basis, "
            "transcript_status, provider_call_id, created_by) VALUES ($1,$2,$3,false,"
            "'awaiting_explicit_live_transcription_consent','pending',$4,$5)", T, command, client, sid, owner)

        # The hang-up applies both, in arrival order, through record_call_outcome.
        assert await post({"CallSid": sid, "CallStatus": "completed", "CallDuration": "42"}) == 204
        assert await post({"CallSid": sid, "CallStatus": "completed", "CallDuration": "42"}) == 204  # a retry
        session = await admin.fetchrow("SELECT started_at, ended_at, outcome FROM live_call_sessions "
                                       "WHERE provider_call_id=$1", sid)
        assert session["started_at"] is not None and session["ended_at"] is not None
        assert session["outcome"] == "completed"
        assert answered and set(answered) == {sid}         # the first response is told it was answered
        assert await admin.fetchval("SELECT disposition FROM lead_response_events WHERE command_id=$1",
                                    command) == "delivered"
        assert await engaged() == 1                        # once, despite in-progress + two completed
        rows = await admin.fetch("SELECT summary, meta FROM client_activities WHERE tenant_id=$1 "
                                 "AND meta->>'provider_call_id' = $2", uuid.UUID(T), sid)
        assert len(rows) == 1 and "0:42" in rows[0]["summary"]
        assert await admin.fetchval("SELECT count(*) FROM provider_call_outcomes WHERE provider_call_id=$1 "
                                    "AND applied_at IS NULL", sid) == 0
        assert sid not in states                           # cleaned up after the terminal outcome

        # A call nobody can attribute yet is not acknowledged.
        assert await post({"CallSid": early, "CallStatus": "completed", "CallDuration": "3"}) == 503
        assert await admin.fetchval("SELECT count(*) FROM provider_call_outcomes WHERE provider_call_id=$1",
                                    early) == 0

        # After cleanup a late status still finds its brokerage through the session.
        assert await post({"CallSid": sid, "CallStatus": "busy"}) == 204
        assert await admin.fetchval("SELECT tenant_id::text FROM provider_call_outcomes "
                                    "WHERE provider_call_id=$1 AND call_status='busy'", sid) == T
        assert await admin.fetchval("SELECT outcome FROM live_call_sessions WHERE provider_call_id=$1",
                                    sid) == "completed"     # the first final outcome stands
        assert await engaged() == 1

        # Out of order (a retried answer after the hang-up): the call never
        # starts after it ended, and a 0 s Twilio 'completed' is not "not answered".
        await admin.execute(
            "INSERT INTO live_call_sessions (tenant_id, command_id, client_id, consent_recorded, consent_basis, "
            "transcript_status, provider_call_id, created_by) VALUES ($1,$2,$3,false,"
            "'awaiting_explicit_live_transcription_consent','pending',$4,$5)",
            T, commands[1], client, reordered, owner)
        assert await post({"CallSid": reordered, "CallStatus": "completed", "CallDuration": "0"}) == 204
        await asyncio.sleep(0.05)
        assert await post({"CallSid": reordered, "CallStatus": "in-progress"}) == 204
        session = await admin.fetchrow("SELECT started_at, ended_at FROM live_call_sessions "
                                       "WHERE provider_call_id=$1", reordered)
        assert session["started_at"] <= session["ended_at"]
        summary = await admin.fetchval("SELECT summary FROM client_activities WHERE tenant_id=$1 "
                                       "AND meta->>'provider_call_id' = $2", uuid.UUID(T), reordered)
        assert summary.startswith("Call answered and ended")
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
