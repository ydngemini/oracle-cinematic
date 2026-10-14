"""No payment card number reaches the database, against real PostgreSQL with
FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN). PCI DSS SAQ A, ledger
L17-07.

A fresh brokerage, as the real roles: an AI call where the callee read out a
card (the platform-context bridge path), an inbound text with a card in it, and
an agent's note through the real route under the app role. Each stored row is
read back as the superuser — the ciphertext opened under the brokerage's key —
and must hold "[card number removed]", never the digits. The PCI_REDACTION
audit rows that record each removal hold the surface and counts only.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "live-test-pan-redaction-master-key-0123456789"
PAN = "4111111111111111"


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def test_no_card_number_is_stored(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import client_enterprise
    import commands_api
    import messaging_data
    import pan_redaction
    import ws_hub
    from crypto import derive_tenant_key
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client = str(uuid.uuid4()), str(uuid.uuid4())
    ref = f"pan-{uuid.uuid4()}"
    key = derive_tenant_key(tenant, MASTER)
    frames = []

    async def fake_users_push(tenant_id, user_ids, payload, *, mirror=True):
        frames.append(payload)
        return len(user_ids)

    async def fake_user_push(tenant_id, user_id, payload):
        frames.append(payload)
        return 1

    real_users_push, real_user_push = ws_hub.broadcast_users, ws_hub.broadcast_user
    ws_hub.broadcast_users, ws_hub.broadcast_user = fake_users_push, fake_user_push
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'PAN redaction test')",
                            tenant, f"pan-{tenant[:8]}")
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-a','agent','agent-a','scrypt$x$y')", uuid.uuid4(), tenant)
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Pat','agent-a')",
            client, tenant)
        await admin.execute(
            """INSERT INTO live_call_sessions (tenant_id, client_id, consent_recorded, consent_basis,
                                               transcript_status, provider_call_id, created_by)
               VALUES ($1,$2,false,'awaiting_explicit_live_transcription_consent','pending',$3,'agent-a')""",
            tenant, client, ref)

        # 1. An AI call: the callee agreed to notes, then read out a card.
        assert await commands_api.record_call_note_consent(ref, True) is True
        note_id = await commands_api.deliver_call_notes(
            ref, ["Wants to pay the deposit with card 4111 1111 1111 1111, cvv 123",
                  "Prefers afternoon showings"], "conv-pan")
        assert note_id
        note = await admin.fetchrow(
            "SELECT body, pgp_sym_decrypt(body_ciphertext, $2) AS opened FROM client_notes WHERE id=$1",
            uuid.UUID(note_id), key)
        assert note["body"] == ""
        assert pan_redaction.PAN_PLACEHOLDER in note["opened"]
        assert pan_redaction.CVV_PLACEHOLDER in note["opened"]
        assert PAN not in _digits(note["opened"]) and "- Prefers afternoon showings" in note["opened"]
        assert frames and all(PAN not in _digits(json.dumps(f)) for f in frames), "live frame carried the card"

        # 2. An inbound text with a card in it (brokerage-scope pipeline write).
        message_id = await messaging_data.record_inbound_message(
            tenant_id=tenant, agent_id="agent-a", provider="telnyx",
            provider_message_id=f"pan-{uuid.uuid4()}", from_e164="+13025550100",
            to_e164="+13025550199", text=f"here is my card {PAN} exp 12/27", media=[], opted_out=False)
        # Sealed at rest since 0152 (L17-21): body is NULL, the text is in body_ciphertext.
        stored = await admin.fetchrow(
            "SELECT body, pgp_sym_decrypt(body_ciphertext, $2) AS opened FROM sms_messages WHERE id=$1",
            uuid.UUID(message_id), key)
        assert stored["body"] is None
        assert stored["opened"] == f"here is my card {pan_redaction.PAN_PLACEHOLDER} exp 12/27"

        # 3. An agent's note through the real route, as the app role under RLS.
        agent = TenantContext(agent_id="agent-a", tenant_id=tenant, role=Role.AGENT)
        created = await client_enterprise.create_note(
            client, client_enterprise.NoteCreate(body="Card on file? 5555-5555-5555-4444"), ctx=agent)
        assert created["body"] == f"Card on file? {pan_redaction.PAN_PLACEHOLDER}"
        opened = await admin.fetchval(
            "SELECT pgp_sym_decrypt(body_ciphertext, $2) FROM client_notes WHERE id=$1",
            uuid.UUID(str(created["id"])), key)
        assert "5555555555554444" not in _digits(opened)

        # Nothing anywhere in this brokerage's rows holds either number.
        for table, column in (("client_activities", "meta::text"), ("client_activities", "summary"),
                              ("sms_messages", "body"), ("sms_messages", "pgp_sym_decrypt(body_ciphertext, $2)")):
            rows = await admin.fetch(
                f"SELECT {column} AS v FROM {table} WHERE tenant_id=$1"
                + (" AND $2::text IS NOT NULL" if "$2" not in column else ""), uuid.UUID(tenant), key)
            assert all(PAN not in _digits(r["v"]) for r in rows), (table, column)

        # The removals were recorded, with counts and no text.
        await asyncio.gather(*list(pan_redaction._pending))
        audit = await admin.fetch(
            "SELECT action, metadata, target_id FROM audit_ledger "
            "WHERE category='PCI_REDACTION' AND tenant_id=$1", uuid.UUID(tenant))
        surfaces = sorted(json.loads(r["metadata"])["surface"] if isinstance(r["metadata"], str)
                          else r["metadata"]["surface"] for r in audit)
        assert surfaces == ["ai_call_note", "client_note", "sms_inbound"], surfaces
        for row in audit:
            assert row["action"] == "payment_card_data_removed"
            assert PAN not in _digits(row["metadata"]) and "5555555555554444" not in _digits(row["metadata"])
    finally:
        ws_hub.broadcast_users, ws_hub.broadcast_user = real_users_push, real_user_push
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                # The audit chain is global: removing rows would break every
                # later entry's prev_hash. The rows hold counts only.
                if row["table_name"] == "audit_ledger":
                    continue
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
