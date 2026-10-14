"""A client's linked contact moves with the client (0137, owner 2026-10-13),
against real PostgreSQL with FORCE RLS, as the application role (skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

The move uses 0134's reassignment guard and the caller's own RLS, and is
skipped — never raised — when the caller may not move the contact or the new
assignee is inactive, so a client reassignment never fails because of it.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_linked_contact_follows_the_client():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import crm
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    ids = {k: uuid.uuid4() for k in ("owner", "a", "b", "gone")}
    agent = {k: f"{k}-{tag}@follow.test" for k in ids}
    client, contact = str(uuid.uuid4()), str(uuid.uuid4())
    drift_client, drift_contact = str(uuid.uuid4()), str(uuid.uuid4())

    def ctx(who):
        return TenantContext(agent_id=agent[who], tenant_id=tenant,
                             role=Role.BROKER_OWNER if who == "owner" else Role.AGENT)

    async def contact_owner(cid):
        return await admin.fetchval("SELECT assigned_agent_id FROM agent_contacts WHERE id=$1", uuid.UUID(cid))

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Follow test')", tenant, f"follow-{tag}")
        for who, uid in ids.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5)",
                uid, tenant, agent[who], "broker_owner" if who == "owner" else "agent", who != "gone")
        # A client and its contact, linked both ways, both with agent A.
        for cl, ct, assignee in ((client, contact, agent["a"]), (drift_client, drift_contact, None)):
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Linked',$3)",
                                cl, tenant, assignee)
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, legacy_client_id, data_state) "
                "VALUES ($1,$2,$3,$4,'pending_encryption')", ct, tenant, assignee, cl)
            await admin.execute("UPDATE clients SET contact_id=$2 WHERE id=$1", uuid.UUID(cl), uuid.UUID(ct))

        # The owner moves the client: the contact goes with it.
        await crm.update_client(client, crm.ClientPatch(assignee_id=agent["b"]), ctx=ctx("owner"))
        assert await contact_owner(contact) == agent["b"]
        # B hands it back to the pool: so does the contact.
        await crm.update_client(client, crm.ClientPatch(assignee_id=None), ctx=ctx("b"))
        assert await contact_owner(contact) is None
        # A takes it from the pool: the contact comes too.
        await crm.update_client(client, crm.ClientPatch(assignee_id=agent["a"]), ctx=ctx("a"))
        assert await contact_owner(contact) == agent["a"]

        # Drift: the pool client's contact already belongs to B. A may take the
        # client but not B's contact — the client moves, the contact stays, no error.
        await admin.execute("UPDATE agent_contacts SET assigned_agent_id=$2 WHERE id=$1",
                            uuid.UUID(drift_contact), agent["b"])
        taken = await crm.update_client(drift_client, crm.ClientPatch(assignee_id=agent["a"]), ctx=ctx("a"))
        assert taken["client"]["assignee_id"] == agent["a"]
        assert await contact_owner(drift_contact) == agent["b"]

        # An inactive new assignee (written directly; the API refuses one) moves
        # the client and leaves the contact, instead of failing the update.
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", uuid.UUID(client), agent["gone"])
        assert await contact_owner(contact) == agent["a"]
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
