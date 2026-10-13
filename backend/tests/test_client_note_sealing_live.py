"""Client notes are sealed at rest, against real PostgreSQL with FORCE RLS
(audit A01, ledger L17-03; skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Proves, as the application role:
- a note written through the API stores no plaintext, and the readers (the
  notes route, the client detail route and the client-AI bundle) still
  return its text;
- a pre-0132 plaintext note keeps reading; the backfill seals it without
  marking it edited, an interrupted run resumes, and --unseal reverses it;
- the database refuses a plaintext AI call note (F17.03) and a row that holds
  both shapes.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
MASTER = "live-test-client-note-master-key-0123456789"


def test_client_notes_are_sealed_and_still_read(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import backfill_client_notes
    import client_ai_automation
    import client_enterprise
    import client_note_crypto
    from crypto import derive_tenant_key
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client = str(uuid.uuid4()), str(uuid.uuid4())
    key = derive_tenant_key(tenant, MASTER)
    agent = TenantContext(agent_id="agent-seal", tenant_id=tenant, role=Role.AGENT)
    queued = []

    async def no_queue(*args, **_kw):
        queued.append(args)

    real_queue = client_enterprise._queue_client_ai
    client_enterprise._queue_client_ai = no_queue
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Note sealing test')",
                            tenant, f"seal-{tenant[:8]}")
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-seal','agent','agent-seal','scrypt$x$y')", uuid.uuid4(), tenant)
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Sealed Client','agent-seal')",
            client, tenant)

        # ── a new note stores no plaintext; every reader opens it ─────────
        made = await client_enterprise.create_note(
            client, client_enterprise.NoteCreate(body="Divorce pending; wants a quiet sale"), ctx=agent)
        assert made["body"] == "Divorce pending; wants a quiet sale"
        stored = await admin.fetchrow(
            "SELECT body, body_ciphertext, pgp_sym_decrypt(body_ciphertext, $2) AS opened "
            "FROM client_notes WHERE id=$1", uuid.UUID(made["id"]), key)
        assert stored["body"] == "" and stored["body_ciphertext"] is not None
        assert stored["opened"] == "Divorce pending; wants a quiet sale"

        patched = await client_enterprise.update_note(
            client, made["id"], client_enterprise.NotePatch(body="Wants a quiet sale"), ctx=agent)
        assert patched["body"] == "Wants a quiet sale"
        assert await admin.fetchval("SELECT body FROM client_notes WHERE id=$1", uuid.UUID(made["id"])) == ""
        pinned = await client_enterprise.update_note(
            client, made["id"], client_enterprise.NotePatch(pinned=True), ctx=agent)
        assert pinned["body"] == "Wants a quiet sale" and pinned["pinned"] is True

        # ── a pre-0132 row (plaintext) still reads ─────────────────────────
        legacy_id = await admin.fetchval(
            "INSERT INTO client_notes (tenant_id, client_id, body, author_id, created_at, updated_at) "
            "VALUES ($1,$2,'Legacy plaintext note','agent-seal', now() - interval '2 days', "
            "now() - interval '2 days') RETURNING id", tenant, client)
        legacy_updated = await admin.fetchval("SELECT updated_at FROM client_notes WHERE id=$1", legacy_id)
        listed = await client_enterprise.list_notes(client, ctx=agent)
        assert {n["body"] for n in listed["notes"]} == {"Wants a quiet sale", "Legacy plaintext note"}
        detail = await client_enterprise.get_client_detail(client, ctx=agent)
        assert {n["body"] for n in detail["notes"]} == {"Wants a quiet sale", "Legacy plaintext note"}
        async with connection.tenant_tx(agent) as conn:
            bundle = await client_ai_automation._load_bundle(conn, client)
        assert {n["body"] for n in bundle["notes"]} == {"Wants a quiet sale", "Legacy plaintext note"}

        # ── the backfill seals it, keeps updated_at, and resumes ───────────
        second_legacy = await admin.fetchval(
            "INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
            "VALUES ($1,$2,'Second legacy note','agent-seal') RETURNING id", tenant, client)
        assert await backfill_client_notes.backfill_tenant(tenant, batch_size=1) == 2
        assert await backfill_client_notes.remaining(tenant) == 0
        assert await backfill_client_notes.backfill_tenant(tenant, batch_size=1) == 0, "not idempotent"
        row = await admin.fetchrow(
            "SELECT body, updated_at, pgp_sym_decrypt(body_ciphertext, $2) AS opened "
            "FROM client_notes WHERE id=$1", legacy_id, key)
        assert row["body"] == "" and row["opened"] == "Legacy plaintext note"
        assert row["updated_at"] == legacy_updated, "a seal-only rewrite marked the note edited"
        listed = await client_enterprise.list_notes(client, ctx=agent)
        assert {n["body"] for n in listed["notes"]} == {
            "Wants a quiet sale", "Legacy plaintext note", "Second legacy note"}
        # A real edit still moves updated_at.
        before = await admin.fetchval("SELECT updated_at FROM client_notes WHERE id=$1", second_legacy)
        await client_enterprise.update_note(
            client, str(second_legacy), client_enterprise.NotePatch(body="Edited"), ctx=agent)
        assert await admin.fetchval("SELECT updated_at FROM client_notes WHERE id=$1", second_legacy) > before

        # ── the database guards ────────────────────────────────────────────
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                "INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
                "VALUES ($1,$2,'Caller said X','neoh-ai-call')", tenant, client)
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                "INSERT INTO client_notes (tenant_id, client_id, body, body_ciphertext, author_id) "
                "VALUES ($1,$2,'both',pgp_sym_encrypt('both',$3),'agent-seal')", tenant, client, key)

        # ── rollback path: --unseal restores plaintext ─────────────────────
        assert await backfill_client_notes.backfill_tenant(tenant, batch_size=2, unseal=True) == 3
        assert await admin.fetchval(
            "SELECT count(*) FROM client_notes WHERE tenant_id=$1 AND body_ciphertext IS NOT NULL", tenant) == 0
        assert await admin.fetchval("SELECT body FROM client_notes WHERE id=$1", legacy_id) == "Legacy plaintext note"
        assert await admin.fetchval("SELECT updated_at FROM client_notes WHERE id=$1", legacy_id) == legacy_updated

        # A sealed row without its key fails closed instead of reading as empty.
        await backfill_client_notes.backfill_tenant(tenant, batch_size=10)
        os.environ.pop("ORACLE_ENCRYPTION_MASTER_KEY")
        try:
            async with connection.tenant_tx(agent) as conn:
                rows = await conn.fetch(
                    f"SELECT id, body, {client_note_crypto.SEALED_COLUMNS} FROM client_notes WHERE client_id=$1",
                    uuid.UUID(client))
                with pytest.raises(RuntimeError):
                    await client_note_crypto.open_rows(conn, rows)
        finally:
            os.environ["ORACLE_ENCRYPTION_MASTER_KEY"] = MASTER
    finally:
        client_enterprise._queue_client_ai = real_queue
        await admin.execute("SET session_replication_role = replica")
        try:
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
