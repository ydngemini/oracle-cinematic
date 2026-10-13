"""Each agent sees their own clients, the brokerage pool, and what is shared
with them — against real PostgreSQL with FORCE RLS, as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03 AC03.01 (clients slice) and AC03.02, owner decision D-2026-10-13-01:
unassigned = brokerage pool; agent-created clients are not auto-assigned;
shares are view or edit, chosen per share.

Actors: agent A (assignee), agent B (not assigned), agent C (shared), the
broker owner, a platform admin, an agent of another brokerage, and a
brokerage-scope pipeline (client portal / inbound matching).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

CHILD_TABLES = (
    "client_notes", "client_activities", "client_tasks", "client_tags", "buyer_profiles",
    "client_ai_state", "transactions", "transaction_parties", "showings", "interaction_logs",
)


def test_agents_see_their_own_pool_and_shared_clients_only():
    asyncio.run(_scenario())


def test_policy_composition_cannot_widen():
    asyncio.run(_policy_composition())


async def _policy_composition():
    """AC03.02: clients has exactly one permissive policy (the tenant wall) and
    the three restrictive agent-scope ones; every child table carries its four
    restrictive parent policies. A new permissive policy would OR around them."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", ["clients", *CHILD_TABLES])
    finally:
        await admin.close()
    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r["tablename"], []).append(r)
    clients = {(r["policyname"], r["permissive"], r["cmd"]) for r in by_table["clients"]}
    assert clients == {
        ("clients_tenant_isolation", "PERMISSIVE", "ALL"),
        ("clients_agent_read", "RESTRICTIVE", "SELECT"),
        ("clients_agent_update", "RESTRICTIVE", "UPDATE"),
        ("clients_agent_delete", "RESTRICTIVE", "DELETE"),
    }, clients
    for t in CHILD_TABLES:
        restrictive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "RESTRICTIVE"}
        assert restrictive == {(f"{t}_client_read", "SELECT"), (f"{t}_client_insert", "INSERT"),
                               (f"{t}_client_update", "UPDATE"), (f"{t}_client_delete", "DELETE")}, (t, restrictive)


async def _scenario():
    import asyncpg
    from fastapi import HTTPException

    import crm
    import record_access
    import tenancy
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    ids = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@matrix.test" for k in ids}
    c_a, c_b, c_pool, c_exp = (str(uuid.uuid4()) for _ in range(4))

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who].upper() if who == "a" else agent[who],  # case must not matter
                             tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="matrix-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"portal:{tag}", tenant_id=tenant, role=Role.AGENT))

    async def visible(c: TenantContext) -> set[str]:
        async with connection.tenant_tx(c) as conn:
            rows = await conn.fetch("SELECT id FROM clients WHERE tenant_id = ANY($1::uuid[])",
                                    [tenant, other_tenant])
        return {str(r["id"]) for r in rows} & {c_a, c_b, c_pool, c_exp}

    async def level(c: TenantContext, client: str):
        async with connection.tenant_tx(c) as conn:
            return await record_access.client_access_level(conn, client)

    try:
        for t, slug in ((tenant, "matrix"), (other_tenant, "matrix-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Access matrix')",
                                t, f"{slug}-{tag}")
        for who, uid in ids.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other_tenant if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        # The trigger resolves the text assignee (any case) to the user.
        for cid, name, assignee in ((c_a, "Sarah", agent["a"].upper()), (c_b, "Marcus", agent["b"]),
                                    (c_pool, "Priya", None), (c_exp, "Elena", agent["a"])):
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                cid, tenant, name, assignee)
        row = await admin.fetchrow("SELECT assignee_id, assignee_user_id FROM clients WHERE id=$1", uuid.UUID(c_a))
        assert row["assignee_id"] == agent["a"] and row["assignee_user_id"] == ids["a"], "not canonicalized"
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await admin.execute("UPDATE clients SET assignee_id='nobody@x.test' WHERE id=$1", uuid.UUID(c_b))
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # another brokerage's agent is nobody here
            await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", uuid.UUID(c_b), agent["d"])

        await admin.execute(
            "INSERT INTO client_notes (tenant_id, client_id, body, author_id) VALUES ($1,$2,'Prefers mornings',$3)",
            tenant, c_a, agent["a"])
        # An expired share grants nothing.
        await admin.execute(
            """INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission,
                                          granted_by, created_at, expires_at)
               VALUES ($1,'client',$2,$3,'edit','seed', now()-interval '2 days', now()-interval '1 day')""",
            tenant, c_exp, ids["b"])

        # ── the matrix before any share ──────────────────────────────────────
        assert await visible(ctx("a")) == {c_a, c_pool, c_exp}
        assert await visible(ctx("b")) == {c_b, c_pool}, "expired share or colleague's client leaked"
        assert await visible(ctx("c")) == {c_pool}
        assert await visible(ctx("owner")) == {c_a, c_b, c_pool, c_exp}
        assert await visible(admin_ctx) == {c_a, c_b, c_pool, c_exp}
        assert await visible(pipeline) == {c_a, c_b, c_pool, c_exp}
        assert await visible(ctx("d")) == set(), "another brokerage saw these clients"
        assert await level(ctx("b"), c_a) is None and await level(ctx("b"), c_pool) == "edit"

        # Children inherit: B cannot read A's client's note, nor write one.
        async with connection.tenant_tx(ctx("b")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM client_notes WHERE client_id=$1", uuid.UUID(c_a)) == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("b")) as conn:
                await conn.execute("INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
                                   "VALUES ($1,$2,'sneaky',$3)", tenant, c_a, agent["b"])
        # A silent UPDATE by B touches nothing.
        async with connection.tenant_tx(ctx("b")) as conn:
            assert await conn.execute("UPDATE clients SET full_name='X' WHERE id=$1", uuid.UUID(c_a)) == "UPDATE 0"

        # ── sharing ───────────────────────────────────────────────────────────
        with pytest.raises(HTTPException) as e:  # B is not the assignee
            await record_access.share_client(c_a, record_access.ShareCreate(grantee=agent["c"], permission="view"),
                                             ctx=ctx("b"))
        assert e.value.status_code == 404  # B cannot even see it
        with pytest.raises(HTTPException) as e:  # the pool is already everyone's
            await record_access.share_client(c_pool, record_access.ShareCreate(grantee=agent["c"], permission="view"),
                                             ctx=ctx("owner"))
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:  # another brokerage's agent is not a member
            await record_access.share_client(c_a, record_access.ShareCreate(grantee=agent["d"], permission="view"),
                                             ctx=ctx("a"))
        assert e.value.status_code == 422

        share = await record_access.share_client(
            c_a, record_access.ShareCreate(grantee=agent["c"], permission="view"), ctx=ctx("a"))
        assert share["permission"] == "view" and share["grantee_agent_id"] == agent["c"]
        assert await visible(ctx("c")) == {c_a, c_pool}
        assert await level(ctx("c"), c_a) == "view"
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM client_notes WHERE client_id=$1", uuid.UUID(c_a)) == 1
            with pytest.raises(HTTPException) as e:
                await record_access.require_client_edit(conn, c_a)
            assert e.value.status_code == 403
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # view means view, at the database too
            async with connection.tenant_tx(ctx("c")) as conn:
                await conn.execute("INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
                                   "VALUES ($1,$2,'edit attempt',$3)", tenant, c_a, agent["c"])
        with pytest.raises(HTTPException) as e:  # through the real route: 403, not a silent no-op
            await crm.update_client(c_a, crm.ClientPatch(full_name="Renamed"), ctx=ctx("c"))
        assert e.value.status_code == 403

        # Upgrade to edit: a NEW grant row, the old one revoked (history kept).
        await record_access.share_client(c_a, record_access.ShareCreate(grantee=agent["c"], permission="edit"),
                                         ctx=ctx("a"))
        assert await admin.fetchval(
            "SELECT count(*) FROM record_shares WHERE resource_id=$1 AND grantee_user_id=$2", uuid.UUID(c_a), ids["c"]
        ) == 2
        assert await level(ctx("c"), c_a) == "edit"
        async with connection.tenant_tx(ctx("c")) as conn:
            await conn.execute("INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
                               "VALUES ($1,$2,'C was here',$3)", tenant, c_a, agent["c"])
        with pytest.raises(HTTPException) as e:  # an edit share does not let C re-share
            await record_access.share_client(c_a, record_access.ShareCreate(grantee=agent["b"], permission="view"),
                                             ctx=ctx("c"))
        assert e.value.status_code == 403
        # F03.02 (0134): an edit share changes the client, it never takes it —
        # not through the route, not to the pool, not at the database.
        for new_assignee in (agent["c"], None):
            with pytest.raises(HTTPException) as e:
                await crm.update_client(c_a, crm.ClientPatch(assignee_id=new_assignee), ctx=ctx("c"))
            assert e.value.status_code == 403
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.tenant_tx(ctx("c")) as conn:
                    await conn.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", uuid.UUID(c_a), new_assignee)

        # ── the Python mirror agrees with the database, actor by actor ────────
        shares_by_user: dict[str, dict[str, str]] = {}
        for r in await admin.fetch("SELECT grantee_user_id, resource_id, permission FROM record_shares "
                                   "WHERE tenant_id=$1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())",
                                   uuid.UUID(tenant)):
            shares_by_user.setdefault(str(r["grantee_user_id"]), {})[str(r["resource_id"])] = r["permission"]
        rows = {str(r["id"]): dict(r) for r in await admin.fetch(
            "SELECT id, tenant_id, assignee_user_id FROM clients WHERE id = ANY($1::uuid[])",
            [uuid.UUID(x) for x in (c_a, c_b, c_pool, c_exp)])}
        for who in ("a", "b", "c", "owner", "d"):
            c = ctx(who)
            # The inline policy (what a query sees) and app_client_access()
            # (what the API asks) must give the same answer.
            seen = await visible(c)
            for cid in rows:
                assert (cid in seen) == (await level(c, cid) is not None), (who, cid)
            for cid, r in rows.items():
                mirror = tenancy.client_access(c, {**r, "tenant_id": str(r["tenant_id"])}, str(ids[who]),
                                               shares_by_user.get(str(ids[who]), {}))
                assert mirror == await level(c, cid), (who, cid, mirror)

        # ── revoke ────────────────────────────────────────────────────────────
        access = await record_access.get_client_access(c_a, ctx=ctx("a"))
        assert access["can_manage_shares"] and [s["permission"] for s in access["shares"]] == ["edit"]
        await record_access.revoke_share(c_a, access["shares"][0]["id"], ctx=ctx("a"))
        assert await visible(ctx("c")) == {c_pool}

        # ── assignment rules ──────────────────────────────────────────────────
        with pytest.raises(HTTPException) as e:  # an agent cannot hand a client to a colleague
            await crm.update_client(c_a, crm.ClientPatch(assignee_id=agent["b"]), ctx=ctx("a"))
        assert e.value.status_code == 403
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # and the database agrees
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", uuid.UUID(c_a), agent["b"])
        with pytest.raises(HTTPException) as e:
            await crm.update_client(c_a, crm.ClientPatch(assignee_id="nobody@x.test"), ctx=ctx("owner"))
        assert e.value.status_code == 422
        taken = await crm.update_client(c_pool, crm.ClientPatch(assignee_id=agent["b"].upper()), ctx=ctx("b"))
        assert taken["client"]["assignee_id"] == agent["b"], taken  # B takes a pool client, canonically
        assert await visible(ctx("a")) == {c_a, c_exp}, "a taken client stayed in A's view"
        await crm.update_client(c_pool, crm.ClientPatch(assignee_id=None), ctx=ctx("b"))  # and hands it back
        assert c_pool in await visible(ctx("a"))
        moved = await crm.update_client(c_a, crm.ClientPatch(assignee_id=agent["b"]), ctx=ctx("owner"))
        assert moved["client"]["assignee_id"] == agent["b"]
        assert c_a not in await visible(ctx("a")) and c_a in await visible(ctx("b"))

        # A deal stays with the agent who opened it, even after the client moves.
        await admin.execute(
            "INSERT INTO transactions (tenant_id, client_id, client_party_role, created_by, state_code) "
            "VALUES ($1,$2,'buyer',$3,'DE')",
            tenant, c_a, agent["a"])
        async with connection.tenant_tx(ctx("a")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM transactions WHERE client_id=$1", uuid.UUID(c_a)) == 1
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM transactions WHERE client_id=$1", uuid.UUID(c_a)) == 0

        # A user session never gets brokerage scope, even if asked.
        sneaky = brokerage_scope(TenantContext(agent_id=agent["c"], tenant_id=tenant, role=Role.AGENT,
                                               verify_session=True, user_id=str(ids["c"]), session_epoch=0))
        assert await visible(sneaky) == {c_pool}
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
                for t in (tenant, other_tenant):
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", t)
            for t in (tenant, other_tenant):
                await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(t))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
