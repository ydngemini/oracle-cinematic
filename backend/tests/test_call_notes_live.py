"""Notes the AI takes on a call reach a BROKERAGE client's timeline, against
real PostgreSQL with FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The bridge writes in the platform context for a call that belongs to a
brokerage. A platform-tenant fixture would pass by coincidence (0128 shipped
after exactly that), so the tenant here is a fresh brokerage.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_consented_call_notes_reach_the_brokerage_clients_timeline():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import commands_api
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client = str(uuid.uuid4()), str(uuid.uuid4())
    ref, declined_ref = f"notes-{uuid.uuid4()}", f"declined-{uuid.uuid4()}"
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Call notes test')",
                            tenant, f"notes-{tenant[:8]}")
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'Sarah')",
                            client, tenant)
        for provider_ref in (ref, declined_ref):
            await admin.execute(
                """INSERT INTO live_call_sessions (tenant_id, client_id, consent_recorded, consent_basis,
                                                   transcript_status, provider_call_id, created_by)
                   VALUES ($1,$2,false,'awaiting_explicit_live_transcription_consent','pending',$3,'agent-a')""",
                tenant, client, provider_ref)

        # Before a yes, nothing is written.
        assert await commands_api.deliver_call_notes(ref, ["Prefers afternoons"]) is None
        assert await commands_api.record_call_note_consent(ref, True) is True
        assert await commands_api.record_call_note_consent(f"missing-{uuid.uuid4()}", True) is False

        notes = ["Prefers afternoon showings", "Would love waterfront or a pond in the backyard"]
        note_id = await commands_api.deliver_call_notes(ref, notes, "conv-1")
        assert note_id, "consented notes were not written for a brokerage client"
        assert await commands_api.deliver_call_notes(ref, notes) is None, "delivered twice"

        note = await admin.fetchrow("SELECT tenant_id, client_id, body, author_id FROM client_notes WHERE id=$1",
                                    uuid.UUID(note_id))
        assert str(note["tenant_id"]) == tenant and str(note["client_id"]) == client
        assert "- Would love waterfront or a pond in the backyard" in note["body"]
        assert note["author_id"] == "neoh-ai-call"
        activity = await admin.fetchrow(
            "SELECT summary, meta FROM client_activities WHERE client_id=$1 AND meta->>'source'='ai_call_notes'",
            uuid.UUID(client))
        assert activity["summary"] == "Call notes from Neoh (2)"
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
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("client_activities", "client_notes", "live_call_sessions", "clients"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", uuid.UUID(tenant))
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
