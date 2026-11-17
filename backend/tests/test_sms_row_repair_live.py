"""A text Telnyx accepted always gets its log row (external audit 25.5),
against real PostgreSQL.

The worker sends first, then writes sms_messages. When that write failed the
command still succeeded with the provider's message id and no row ever
existed: no delivery status, no timeline entry, and every receipt webhook
answered 503 for a row that would never appear. The reconciliation sweep now
rebuilds the row from the command — never by sending again — and a receipt
then attaches to it.

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


def test_a_sent_text_without_a_log_row_is_repaired_not_resent(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "c" * 64)
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import messaging_data
    import messaging_provider
    import sealed_text
    from db import connection

    def no_send(*_a, **_k):
        raise AssertionError("a repair must never send")

    monkeypatch.setattr(messaging_provider, "get_messaging_provider", no_send)

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    agent, lost = f"agent-{s}@live.test", f"lost-{s}@live.test"
    client = str(uuid.uuid4())
    sender = "+1302" + str(secrets.randbelow(10 ** 7)).zfill(7)
    msg, orphan = f"msg-{s}", f"orphan-{s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"sms-{s}", f"B {s}")
            for who in (agent, lost):
                await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                    "VALUES ($1,$2,'agent',$2,'scrypt$x$y')", T, who)
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,$3)",
                                client, T, f"X {s}")
            account = "AC" + secrets.token_hex(16)
            await admin.execute(
                "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, active, provider, "
                "twilio_account_sid, provider_account_id, voice_caller_id_e164, voice_caller_id_verified, "
                "agent_forward_e164, outbound_verification_status) "
                "VALUES ($1,$2,$3,true,'twilio',$4,$5,$6,true,'+13025550188','verified')",
                T, agent, sender, account, account, sender)
            for who, ref, key in ((agent, msg, "a"), (lost, orphan, "b")):
                await admin.execute(
                    "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
                    "draft, state, idempotency_key, created_by, provider, provider_reference, provider_submitted_at, "
                    "updated_at) VALUES ($1,'SMS','external','outreach',$2::jsonb,$3::jsonb,'succeeded',$4,$5,"
                    "'telnyx',$6, now() - interval '10 minutes', now() - interval '10 minutes')",
                    T, json.dumps({"phone": "+13025550142", "client_id": client}),
                    json.dumps({"content": {"body": f"hello {s}"}, "context": {}}), f"sms-{s}-{key}", who, ref)

        # Before: the receipt has nothing to attach to (the webhook answers 503).
        assert await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=msg, status="delivered", error_reason=None) is False

        # Two sweeps at once: one row, one timeline entry.
        first, second = await asyncio.gather(messaging_data.repair_missing_outbound_rows(T),
                                             messaging_data.repair_missing_outbound_rows(T))
        assert first["no_sender"] == 1 and second["no_sender"] == 1      # the agent with no line: left for a person
        row = await admin.fetchrow(
            "SELECT agent_id, client_id, from_e164, to_e164, status, activity_id, "
            "pgp_sym_decrypt(body_ciphertext, $2) AS body FROM sms_messages WHERE provider='telnyx' "
            "AND provider_message_id=$1", msg, sealed_text.key_for(T))
        assert row is not None
        assert (row["agent_id"], str(row["client_id"]), row["from_e164"], row["to_e164"], row["body"]) == (
            agent, client, sender, "+13025550142", f"hello {s}")
        assert row["activity_id"] is not None
        assert await admin.fetchval("SELECT count(*) FROM sms_messages WHERE provider_message_id=$1", msg) == 1
        assert await admin.fetchval("SELECT count(*) FROM client_activities WHERE tenant_id=$1", T) == 1
        assert await admin.fetchval("SELECT count(*) FROM sms_messages WHERE provider_message_id=$1", orphan) == 0

        # After: the carrier's retry attaches.
        assert await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=msg, status="delivered", error_reason=None) is True
        assert await admin.fetchval("SELECT status FROM sms_messages WHERE provider_message_id=$1", msg) == "delivered"
        again = await messaging_data.repair_missing_outbound_rows(T)
        assert again["repaired"] == 0
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
