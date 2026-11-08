"""client_contact_index (0156, audit A01, L17-23) against real PostgreSQL with
FORCE RLS, as the application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Pins: every client gets a row (stale until filled); the filler writes the
contact_truth values and never moves clients.updated_at; a name/email/phone
change marks the row stale and anything else does not; a fill that races an
uncommitted edit skips that client instead of recording old values; agents
read only the rows of clients they can see and can never write one; the row
goes with its client.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_client_contact_index_is_kept_current(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "cd" * 32)
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import client_contact_index as cci
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    tenant = str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("a", "b")}
    agent = {k: f"{k}-{tag}@index.test" for k in users}
    client = {k: uuid.uuid4() for k in ("a", "b", "pool", "odd")}

    def ctx(who: str) -> TenantContext:
        return TenantContext(agent_id=agent[who], tenant_id=tenant, role=Role.AGENT)

    async def index_row(cid):
        return await admin.fetchrow("SELECT * FROM client_contact_index WHERE client_id = $1", cid)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Index')", tenant, f"index-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,'agent',$3,'scrypt$x$y',$4,true)", uid, tenant, agent[who], f"Agent {who}")
        rows = {
            "a": ("Sarah Johnson", "Sarah@Example.com", "302-555-0101", agent["a"]),
            "b": ("Luis Ortega", "luis@example.com", "+1 302 555 0102", agent["b"]),
            "pool": ("Ana Ruiz", None, None, None),
            "odd": ("  ", "not-an-email", "555-0103", None),
        }
        for k, (name, email, phone, assignee) in rows.items():
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, email, phone, assignee_id, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6, now() - interval '1 day')",
                client[k], tenant, name, email, phone, assignee)

        # ── every new client has a stale row, and the filler fills it ───────
        for k in client:
            row = await index_row(client[k])
            assert row is not None and row["stale"] and row["indexed_at"] is None, k
        before = {r["id"]: r["updated_at"] for r in await admin.fetch(
            "SELECT id, updated_at FROM clients WHERE tenant_id = $1::uuid", tenant)}
        assert await cci.remaining() >= 4
        result = await cci.fill(batch_size=2)
        assert result["indexed"] >= 4
        for k, (name, email, phone, _assignee) in rows.items():
            row = await index_row(client[k])
            want = cci.index_values(tenant, name, email, phone)
            assert not row["stale"] and row["indexed_at"] is not None, k
            assert row["email_lookup_hash"] == want["email_hash"], k
            assert row["phone_lookup_hash"] == want["phone_hash"], k
            assert list(row["name_search_tokens"]) == want["tokens"], k
            assert (row["email_unusable"], row["phone_unusable"]) == (want["email_unusable"],
                                                                      want["phone_unusable"]), k
        odd = await index_row(client["odd"])
        assert odd["email_unusable"] and odd["phone_unusable"] and list(odd["name_search_tokens"]) == []
        after = {r["id"]: r["updated_at"] for r in await admin.fetch(
            "SELECT id, updated_at FROM clients WHERE tenant_id = $1::uuid", tenant)}
        assert after == before, "filling the index must never move clients.updated_at"
        again = await cci.fill(batch_size=10)  # idempotent: nothing of this tenant is pending
        assert all(not r["stale"] for r in await admin.fetch(
            "SELECT stale FROM client_contact_index WHERE tenant_id = $1::uuid", tenant)), again

        # ── an edit of name/email/phone marks stale; anything else does not ─
        async with connection.tenant_tx(ctx("a")) as conn:
            assert await conn.execute("UPDATE clients SET notes = 'x', stage = 'active' WHERE id = $1",
                                      client["a"]) == "UPDATE 1"
            assert await conn.execute("UPDATE clients SET email = email WHERE id = $1",
                                      client["a"]) == "UPDATE 1"  # same value: not a change
        assert not (await index_row(client["a"]))["stale"]
        async with connection.tenant_tx(ctx("a")) as conn:
            await conn.execute("UPDATE clients SET email = 'sarah.new@example.com' WHERE id = $1", client["a"])
        assert (await index_row(client["a"]))["stale"]
        await cci.fill(batch_size=10)
        row = await index_row(client["a"])
        assert not row["stale"] and row["email_lookup_hash"] == cci.index_values(
            tenant, "Sarah Johnson", "sarah.new@example.com", None)["email_hash"]

        # ── a fill racing an uncommitted edit skips that client ─────────────
        other = await asyncpg.connect(DSN)
        try:
            tx = other.transaction()
            await tx.start()
            await other.execute("UPDATE clients SET phone = '+13025550199' WHERE id = $1", client["b"])
            # The edit holds the client's row lock: the fill skips it (SKIP LOCKED) ...
            assert (await index_row(client["b"]))["stale"] is False  # trigger's mark not visible yet
            await cci.fill(batch_size=10)
            await tx.commit()
        finally:
            await other.close()
        # ... and the committed edit's mark stands, so the next fill uses the new phone.
        assert (await index_row(client["b"]))["stale"]
        await cci.fill(batch_size=10)
        row = await index_row(client["b"])
        assert row["phone_lookup_hash"] == cci.index_values(tenant, None, None, "+13025550199")["phone_hash"]

        # ── access: agents read what they can see, never write ──────────────
        async with connection.tenant_tx(ctx("a")) as conn:
            seen = {r["client_id"] for r in await conn.fetch(
                "SELECT client_id FROM client_contact_index WHERE tenant_id = $1::uuid", tenant)}
            assert seen == {client["a"], client["pool"], client["odd"]}, seen  # not B's
            assert await conn.execute("UPDATE client_contact_index SET stale = true WHERE client_id = $1",
                                      client["a"]) == "UPDATE 0"
            assert await conn.execute("DELETE FROM client_contact_index WHERE client_id = $1",
                                      client["pool"]) == "DELETE 0"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute("DELETE FROM client_contact_index WHERE client_id = $1", client["a"])
                await conn.execute(
                    "INSERT INTO client_contact_index (client_id, tenant_id, stale) VALUES ($1,$2,true)",
                    client["a"], tenant)
        # An agent's own client edit still marks the row (the trigger is SECURITY DEFINER).
        async with connection.tenant_tx(ctx("a")) as conn:
            await conn.execute("UPDATE clients SET full_name = 'Sarah J. Johnson' WHERE id = $1", client["a"])
        assert (await index_row(client["a"]))["stale"]

        # ── the row goes with its client ─────────────────────────────────────
        await admin.execute("DELETE FROM clients WHERE id = $1", client["pool"])
        assert await index_row(client["pool"]) is None
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("client_contact_index", "clients", "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
