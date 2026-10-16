"""One person, many channels, one contact (T04.13, AC04.01, L04-04), against
real PostgreSQL with FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The same person texts, then calls, then arrives through a signed lead
webhook. Pins: the webhook creates exactly one client and one contact, and
the earlier text and call — stored with no contact because none existed —
are linked to it; a later text links on arrival; a text from another number,
a call already linked to someone else and another brokerage's rows are not
touched; a replayed webhook creates nothing new.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

PHONE, OTHER = "+13025550161", "+13025550162"


def test_one_person_across_channels_is_one_contact(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-linking-master-key-0123456789abcdef")
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import contact_truth
    import lead_routing_api as lr
    import messaging_data
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, elsewhere = str(uuid.uuid4()), str(uuid.uuid4())
    owner = TenantContext(agent_id=f"owner-{tenant[:8]}@link.test", tenant_id=tenant, role=Role.BROKER_OWNER)

    async def text_in(t, phone):
        return await messaging_data.record_inbound_message(
            tenant_id=t, agent_id=owner.agent_id, provider="telnyx", provider_message_id=f"in-{uuid.uuid4()}",
            from_e164=phone, to_e164="+13025550100", text="Is the house on Main still available?",
            media=[], opted_out=False)

    routes: dict[str, uuid.UUID] = {}

    async def call_in(t, phone, contact_id=None):
        if t not in routes:
            routes[t] = await admin.fetchval(
                "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider, provider_account_id, "
                "forward_on_request, forward_when_ai_unavailable) "
                "VALUES ($1,$2,$3,'plivo','MAlink',false,false) RETURNING id",
                uuid.UUID(t), owner.agent_id, f"+1302555{uuid.uuid4().int % 10000:04d}")
        call = uuid.uuid4()
        await admin.execute(
            "INSERT INTO inbound_voice_calls (id, tenant_id, route_id, provider_call_sid, provider_status, direction, "
            "caller_phone_lookup_hash, contact_id, intake_mode, caller_phone_ciphertext, disclosure_version, "
            "disclosed_at) VALUES ($1,$2,$3,$4,'completed','inbound',$5,$6,'auto','\\x00'::bytea,'v1',now())",
            call, uuid.UUID(t), routes[t], f"CA{uuid.uuid4().hex}", contact_truth.lookup_hash(t, "phone", phone),
            contact_id)
        return call

    class _Request:
        def __init__(self, body):
            self._body = body

        async def body(self):
            return self._body

    async def webhook(public_id, secret, event_id):
        body = json.dumps({"external_event_id": event_id, "full_name": "Jordan Reyes",
                           "email": "jordan.reyes@example.com", "phone": PHONE, "intent": "buyer"}).encode()
        ts = str(int(time.time()))
        return await lr.receive_lead(public_id, _Request(body), ts, lr._webhook_signature(secret, ts, body))

    async def counts(t):
        return (await admin.fetchval("SELECT count(*) FROM clients WHERE tenant_id=$1", uuid.UUID(t)),
                await admin.fetchval("SELECT count(*) FROM agent_contacts WHERE tenant_id=$1", uuid.UUID(t)))

    try:
        for t, name in ((tenant, "Linking"), (elsewhere, "Elsewhere")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                uuid.UUID(t), f"link-{t[:8]}", name)
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,'broker_owner',$2)",
                            uuid.UUID(tenant), owner.agent_id)

        # ── before the contact exists: stored, linked to nobody ─────────────
        early_text = await text_in(tenant, PHONE)
        early_call = await call_in(tenant, PHONE)
        stranger_text = await text_in(tenant, OTHER)
        foreign_text = await text_in(elsewhere, PHONE)
        someone = uuid.uuid4()
        await admin.execute("INSERT INTO agent_contacts (id, tenant_id, source, data_state) "
                            "VALUES ($1,$2,'manual','pending_encryption')", someone, uuid.UUID(tenant))
        claimed_call = await call_in(tenant, PHONE, contact_id=someone)
        assert await admin.fetchval("SELECT contact_id FROM sms_messages WHERE id=$1::uuid", early_text) is None
        assert await counts(tenant) == (0, 1)

        # ── the lead webhook: one client, one contact, earlier rows join ────
        created = await lr.create_connector(lr.ConnectorCreate(source_key="website", name="Website form"), ctx=owner)
        public_id = uuid.UUID(created["connector"]["public_id"])
        secret = created["webhook_secret_once"]
        await webhook(public_id, secret, "evt-1")
        assert await counts(tenant) == (1, 2)
        contact = await admin.fetchrow(
            "SELECT id, legacy_client_id FROM agent_contacts WHERE tenant_id=$1 AND id<>$2", uuid.UUID(tenant), someone)
        text_row = await admin.fetchrow("SELECT contact_id, client_id FROM sms_messages WHERE id=$1::uuid", early_text)
        assert text_row["contact_id"] == contact["id"] and text_row["client_id"] == contact["legacy_client_id"]
        call_row = await admin.fetchrow("SELECT contact_id, client_id FROM inbound_voice_calls WHERE id=$1", early_call)
        assert call_row["contact_id"] == contact["id"] and call_row["client_id"] == contact["legacy_client_id"]

        # ── not touched: another number, a claimed call, another brokerage ──
        assert await admin.fetchval("SELECT contact_id FROM sms_messages WHERE id=$1::uuid", stranger_text) is None
        assert await admin.fetchval("SELECT contact_id FROM inbound_voice_calls WHERE id=$1", claimed_call) == someone
        assert await admin.fetchval("SELECT contact_id FROM sms_messages WHERE id=$1::uuid", foreign_text) is None

        # ── afterwards a text links on arrival; a replay creates nothing ────
        later = await text_in(tenant, PHONE)
        assert await admin.fetchval("SELECT contact_id FROM sms_messages WHERE id=$1::uuid", later) == contact["id"]
        await webhook(public_id, secret, "evt-1")
        assert await counts(tenant) == (1, 2)
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for t in (tenant, elsewhere):
                for table in ("client_activities", "sms_messages", "inbound_voice_calls", "lead_intake_events",
                              "lead_source_connectors", "agent_routing_state", "telephony_routes", "agent_contacts", "clients", "users"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
