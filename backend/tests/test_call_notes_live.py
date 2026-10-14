"""Notes the AI takes on a call reach a BROKERAGE client's timeline, against
real PostgreSQL with FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The bridge writes in the platform context for a call that belongs to a
brokerage. A platform-tenant fixture would pass by coincidence (0128 shipped
after exactly that), so the tenant here is a fresh brokerage.

Also proves (D-2026-10-12-03, prompt 09 T09.12/T09.14):
- the note lands 'proposed' by default and 'approved' for an agent who chose
  direct;
- the live frame goes only to the requesting and the assigned agent, never
  tenant-wide, and only while they may read the client (L03-08: a requester
  with no access to the client gets nothing);
- an agent approves a proposed note through the real route, under RLS;
- the note text is sealed at rest (0132, audit A01): body is empty, the
  ciphertext opens under the brokerage's key, and the routes return the text.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


MASTER = "live-test-call-notes-master-key-0123456789"


def test_consented_call_notes_reach_the_brokerage_clients_timeline(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import call_note_settings
    import client_enterprise
    import commands_api
    import ws_hub
    from db import connection
    from crypto import derive_tenant_key
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client = str(uuid.uuid4()), str(uuid.uuid4())
    ref, declined_ref = f"notes-{uuid.uuid4()}", f"declined-{uuid.uuid4()}"
    direct_ref = f"direct-{uuid.uuid4()}"
    agent_c = f"agent-c-{uuid.uuid4().hex[:8]}@live.test"
    pushes, tenant_wide = [], []

    async def fake_user_push(tenant_id, user_id, payload):
        pushes.append((tenant_id, user_id, payload))
        return 1

    async def fake_tenant_push(tenant_id, payload):
        tenant_wide.append(payload)
        return 1

    async def fake_users_push(tenant_id, user_ids, payload):
        for user_id in user_ids:
            pushes.append((tenant_id, user_id, payload))
        return len(user_ids)

    real_user_push, real_tenant_push = ws_hub.broadcast_user, ws_hub.broadcast
    real_users_push = ws_hub.broadcast_users
    ws_hub.broadcast_user, ws_hub.broadcast = fake_user_push, fake_tenant_push
    ws_hub.broadcast_users = fake_users_push
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Call notes test')",
                            tenant, f"notes-{tenant[:8]}")
        # The assignee must be a member (0131 rejects an assignee who is nobody).
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-b','agent','agent-b','scrypt$x$y')", uuid.uuid4(), tenant)
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Sarah','agent-b')",
            client, tenant)
        # The requester of the first call works the client through an edit share.
        agent_a_id = uuid.uuid4()
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-a','agent','agent-a','scrypt$x$y')", agent_a_id, tenant)
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'edit','owner')", tenant, client, agent_a_id)
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,$3,'agent',$3,'scrypt$x$y')", uuid.uuid4(), tenant, agent_c)
        for provider_ref, creator in ((ref, "agent-a"), (declined_ref, "agent-a"), (direct_ref, agent_c)):
            await admin.execute(
                """INSERT INTO live_call_sessions (tenant_id, client_id, consent_recorded, consent_basis,
                                                   transcript_status, provider_call_id, created_by)
                   VALUES ($1,$2,false,'awaiting_explicit_live_transcription_consent','pending',$3,$4)""",
                tenant, client, provider_ref, creator)

        # Before a yes, nothing is written.
        assert await commands_api.deliver_call_notes(ref, ["Prefers afternoons"]) is None
        assert await commands_api.record_call_note_consent(ref, True) is True
        assert await commands_api.record_call_note_consent(f"missing-{uuid.uuid4()}", True) is False

        notes = ["Prefers afternoon showings", "Would love waterfront or a pond in the backyard"]
        note_id = await commands_api.deliver_call_notes(ref, notes, "conv-1")
        assert note_id, "consented notes were not written for a brokerage client"
        assert await commands_api.deliver_call_notes(ref, notes) is None, "delivered twice"

        note = await admin.fetchrow(
            "SELECT tenant_id, client_id, body, author_id, review_state, "
            "pgp_sym_decrypt(body_ciphertext, $2) AS opened FROM client_notes WHERE id=$1",
            uuid.UUID(note_id), derive_tenant_key(tenant, MASTER))
        assert str(note["tenant_id"]) == tenant and str(note["client_id"]) == client
        assert note["body"] == "", "an AI call note was stored in plaintext"
        assert "- Would love waterfront or a pond in the backyard" in note["opened"]
        assert note["author_id"] == "neoh-ai-call"
        assert note["review_state"] == "proposed", "review is the default mode"
        activity = await admin.fetchrow(
            "SELECT summary, meta FROM client_activities WHERE client_id=$1 AND meta->>'source'='ai_call_notes'",
            uuid.UUID(client))
        assert activity["summary"] == "Call notes from Neoh (2) — waiting for your review"

        # The live frame reached exactly the requester and the assignee; nothing tenant-wide.
        assert tenant_wide == []
        assert sorted(user for _, user, _ in pushes) == ["agent-a", "agent-b"]
        assert all(p["notes"] == notes and p["review_state"] == "proposed" for _, _, p in pushes)

        # An agent approves it through the real route, under RLS.
        agent_b = TenantContext(agent_id="agent-b", tenant_id=tenant, role=Role.AGENT)
        approved = await client_enterprise.approve_note(client, note_id, ctx=agent_b)
        assert approved["review_state"] == "approved" and approved["reviewed_by"] == "agent-b"
        assert "- Prefers afternoon showings" in approved["body"]
        listed = await client_enterprise.list_notes(client, ctx=agent_b)
        assert any("- Prefers afternoon showings" in n["body"] for n in listed["notes"])
        with pytest.raises(Exception) as again:
            await client_enterprise.approve_note(client, note_id, ctx=agent_b)
        assert getattr(again.value, "status_code", None) == 409

        # An agent who chose 'direct' gets an approved note at once.
        ctx_c = TenantContext(agent_id=agent_c, tenant_id=tenant, role=Role.AGENT)
        async with connection.tenant_tx(ctx_c) as conn:
            assert await call_note_settings.write_mode(conn, tenant, agent_c, "direct") == "direct"
            assert await call_note_settings.read_mode(conn, tenant, agent_c.upper()) == "direct"
        assert await commands_api.record_call_note_consent(direct_ref, True) is True
        pushes.clear()
        direct_id = await commands_api.deliver_call_notes(direct_ref, ["Call back Friday"])
        # agent_c requested the call but cannot read the client (no share, not
        # the assignee): only the assignee is told live.
        assert [user for _, user, _ in pushes] == ["agent-b"] and tenant_wide == []
        assert await admin.fetchval("SELECT review_state FROM client_notes WHERE id=$1",
                                    uuid.UUID(direct_id)) == "approved"
        session = await admin.fetchrow(
            "SELECT consent_recorded, consent_basis, transcript_status FROM live_call_sessions "
            "WHERE provider_call_id=$1", ref)
        assert tuple(session) == (True, "verbal_consent_on_ai_call", "complete")

        # A no is recorded as a no, and keeps nothing.
        assert await commands_api.record_call_note_consent(declined_ref, False) is True
        assert await commands_api.deliver_call_notes(declined_ref, ["anything"]) is None
        declined = await admin.fetchrow(
            "SELECT consent_recorded, consent_basis FROM live_call_sessions WHERE provider_call_id=$1",
            declined_ref)
        assert tuple(declined) == (False, "declined_on_ai_call")
    finally:
        ws_hub.broadcast_user, ws_hub.broadcast = real_user_push, real_tenant_push
        ws_hub.broadcast_users = real_users_push
        await admin.execute("SET session_replication_role = replica")
        try:
            # Every row the throwaway brokerage owns, whichever table (approving a
            # note also queues a client-AI job).
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
