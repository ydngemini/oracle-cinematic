"""The backfill copies CRM do-not-contact flags set before 26.1 into the
outreach ledger (L04-07), against real PostgreSQL (skipped without
ORACLE_LIVE_DB_ADMIN_DSN): sealed contacts by their own details, legacy
contacts by their client's; linked clients' numbers too; idempotent."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_existing_flags_reach_the_ledger(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import contacts_api
    import crm_suppression_backfill as backfill
    from contacts_api import ContactCreate
    from db import connection
    from tenancy import Role, TenantContext

    if not os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-master-key-" + secrets.token_hex(16))
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    owner = f"owner-{tag}@bf.test"
    ctx = TenantContext(agent_id=owner, tenant_id=tenant, role=Role.BROKER_OWNER)

    async def ledger() -> set[tuple[str, str]]:
        return {(r["contact"], r["channel"]) for r in await admin.fetch(
            "SELECT contact, channel FROM outreach_suppression WHERE tenant_id=$1 AND lifted_at IS NULL",
            uuid.UUID(tenant))}

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Backfill')", tenant, f"bf-{tag}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash, is_active) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y',true)", tenant, owner)
        # A sealed contact flagged dnc + email, linked to a client holding another number.
        linked = uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, phone) VALUES ($1,$2,'Linked','302-555-0420')",
                            linked, tenant)
        sealed = (await contacts_api.create_contact(ContactCreate(
            full_name="Sealed", phone="+13025550410", email=f"Sealed-{tag}@bf.test", client_id=str(linked),
            suppression={"dnc": True, "email": True}), ctx=ctx))["contact"]
        await admin.execute("UPDATE clients SET phone='302-555-0420' WHERE id=$1", linked)   # drifted
        # A legacy contact (unsealed) flagged sms: its details are its client's.
        legacy_client, legacy = uuid.uuid4(), uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, phone) VALUES ($1,$2,'Old','(302) 555-0430')",
                            legacy_client, tenant)
        await admin.execute(
            "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state, legacy_client_id, "
            "suppression) VALUES ($1,$2,$3,'legacy','pending_encryption',$4,$5::jsonb)",
            legacy, tenant, owner, legacy_client, json.dumps({"sms": True}))
        # A soft-deleted flagged contact: included (a restore would not rewrite it).
        gone = (await contacts_api.create_contact(ContactCreate(
            full_name="Gone", phone="+13025550450", suppression={"voice": True}), ctx=ctx))["contact"]
        await admin.execute("UPDATE agent_contacts SET deleted_at=now() WHERE id=$1", uuid.UUID(gone["id"]))
        # A sealed contact whose record cannot be opened: skipped, listed, its
        # client's details still written.
        broken_client, broken = uuid.uuid4(), uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, phone) VALUES ($1,$2,'Broken','+13025550460')",
                            broken_client, tenant)
        await admin.execute(
            "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state, legacy_client_id, "
            "suppression, pii_ciphertext) VALUES ($1,$2,$3,'legacy','sealed',$4,$5::jsonb,'\\x00ff'::bytea)",
            broken, tenant, owner, broken_client, json.dumps({"sms": True}))
        # An unflagged contact adds nothing.
        await contacts_api.create_contact(ContactCreate(full_name="Clear", phone="+13025550440"), ctx=ctx)
        # Before 26.1 none of this reached the ledger.
        await admin.execute("DELETE FROM outreach_suppression WHERE tenant_id=$1", uuid.UUID(tenant))
        assert await ledger() == set()

        # Only this brokerage (the shared database holds others); a batch of one
        # exercises the keyset.
        out = await backfill.backfill(batch_size=1, tenant_ids=[tenant])
        assert (out["contacts"], out["unreadable"]) == (4, 1)
        assert await ledger() == {
            ("+13025550410", "*"), (f"sealed-{tag}@bf.test", "email"),     # the sealed contact's own
            ("+13025550420", "*"),                                         # its drifted client's number
            ("+13025550430", "sms"),                                       # the legacy contact's client
            ("+13025550450", "voice"),                                     # the soft-deleted contact
            ("+13025550460", "sms"),                                       # the unreadable one's client
        }
        assert out["rows_added"] == 6
        again = await backfill.backfill(batch_size=50, tenant_ids=[tenant])
        assert again["rows_added"] == 0                                    # idempotent
        assert await backfill.flagged_count([tenant]) == 4                 # --count, scoped
        with pytest.raises(SystemExit) as bad:                             # a malformed id: usage error
            await backfill.main(["--count", "--tenant", "not-a-uuid"])
        assert bad.value.code == 2
        assert sealed["id"]
    finally:
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
