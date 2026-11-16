"""A data-subject request covers what is attached to the person's client
record (external audit 29.8 follow-up), against real PostgreSQL.

Notes, tasks, activities, tags, buyer profiles, showings and the AI's summary
of the client were neither discovered nor returned by an access request,
though a deletion erased them (ON DELETE CASCADE). Drafts and approvals that
named the person were neither returned nor mentioned, and a deletion left
them in place unlinked. Now access returns all of them, and the deletion
receipt says what went with the client and what was kept unlinked.

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


def test_access_returns_and_deletion_reports_what_hangs_off_the_client(monkeypatch, tmp_path):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "a" * 64)
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import privacy_requests
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    x = str(uuid.uuid4())
    cmd = str(uuid.uuid4())
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                T, f"kids-{s}", f"Brokerage {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name, email) VALUES ($1,$2,$3,$4)",
                                x, T, f"X {s}", f"x-{s}@client.test")
            await admin.execute("INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
                                "VALUES ($1,$2,$3,$4)", T, x, f"note {s}", owner)
            await admin.execute("INSERT INTO client_tasks (tenant_id, client_id, title, created_by, status) "
                                "VALUES ($1,$2,$3,$4,'open')", T, x, f"task {s}", owner)
            await admin.execute("INSERT INTO client_activities (tenant_id, client_id, kind, summary, actor) "
                                "VALUES ($1,$2,'note',$3,$4)", T, x, f"activity {s}", owner)
            await admin.execute("INSERT INTO client_tags (tenant_id, client_id, tag) VALUES ($1,$2,$3)",
                                T, x, f"tag-{s}")
            lead = await admin.fetchval("INSERT INTO leads (tenant_id, parcel_id, state, motivation_score) "
                                        "VALUES ($1,$2,'DE',1) RETURNING id", T, f"P-{s}")
            await admin.execute("INSERT INTO showings (tenant_id, client_id, lead_id, feedback) VALUES ($1,$2,$3,$4)",
                                T, x, lead, f"feedback {s}")
            await admin.execute("INSERT INTO client_ai_state (tenant_id, client_id, summary) VALUES ($1,$2,$3)",
                                T, x, f"ai summary {s}")
            await admin.execute(
                "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, "
                "draft, state, idempotency_key, created_by) VALUES ($1,$2,'EMAIL','external','outreach',"
                "$6::jsonb,$3::jsonb,'awaiting_approval',$4,$5)",   # client_id derived from target (0142)
                cmd, T, json.dumps({"body": f"draft {s}"}), f"k-{s}", owner, json.dumps({"client_id": x}))
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        async def ask(kind):
            return await privacy_requests.handle_subject_request(
                octx, kind=kind, email=f"x-{s}@client.test", phone=None, reason="asked", preview=False)

        found = await ask("dsr_access")
        records = found["records"]
        for table, text in (("client_notes", f"note {s}"), ("client_tasks", f"task {s}"),
                            ("client_activities", f"activity {s}"), ("client_tags", f"tag-{s}"),
                            ("showings", f"feedback {s}"), ("client_ai_state", f"ai summary {s}"),
                            ("command_executions", f"draft {s}")):
            assert table in records and text in json.dumps(records[table]), table
        assert set(found["matches"]) <= set(records)

        done = await ask("dsr_delete")
        assert done["removed_with_client"]["client_notes"] == 1 and done["removed_with_client"]["showings"] == 1
        assert done["kept_unlinked"] == {"command_executions": 1}
        for table in ("client_notes", "client_tasks", "client_activities", "client_tags", "showings", "client_ai_state"):
            assert await admin.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id=$1", T) == 0, table
        # Kept as the brokerage's action record, no longer linked to the person.
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1", cmd) is None
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
