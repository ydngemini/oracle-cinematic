"""The Python visibility mirrors in tenancy.py agree with the live policies
(T03.15, AC03.02, L03-12), against real PostgreSQL with FORCE RLS as the
application login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

tenancy.py says its predicates must stay in lockstep with the CREATE POLICY
statements. client_access / contact_access are already held to the database
by the access-matrix tests; this one holds the rest — can_read_private (the
tenant wall), lead_access (leads_linked_*, 0143), can_read_listing
(listings_read) and can_write (listings_write) — by asking, for every actor
and every row, what the mirror says and what the database actually lets the
same identity read and update.

Actors: agents A and C, the broker owner, an agent of another brokerage, a
platform admin and a brokerage-scope pipeline. C holds a VIEW share of B's
client. Rows: B's client and A's client with a lead each, a harvested lead
with no client, another brokerage's lead, and four listings (own, another
brokerage's private, its shared-pool and one it granted to this brokerage).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_python_mirrors_match_the_policies():
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import tenancy
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@mirror.test" for k in users}

    def ctx(who: str) -> TenantContext:
        return TenantContext(agent_id=agent[who], tenant_id=other if who == "d" else tenant,
                             role=Role.BROKER_OWNER if who == "owner" else Role.AGENT)

    actors = {w: ctx(w) for w in ("a", "c", "owner", "d")}
    actors["admin"] = TenantContext(agent_id=f"admin-{tag}", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    actors["pipeline"] = brokerage_scope(TenantContext(agent_id=f"pipeline:{tag}", tenant_id=tenant, role=Role.AGENT))
    actor_user = {w: (str(users[w]) if w in users else None) for w in actors}

    clients = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    leads = {"a": uuid.uuid4(), "b": uuid.uuid4(), "harvest": uuid.uuid4(), "other": uuid.uuid4()}
    listings = {k: uuid.uuid4() for k in ("own", "private", "pool", "granted")}

    async def seen(c: TenantContext, table: str, ids) -> set:
        async with connection.tenant_tx(c) as conn:
            return {r["id"] for r in await conn.fetch(f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", list(ids))}

    async def updatable(c: TenantContext, table: str, rid) -> bool:
        async with connection.tenant_tx(c) as conn:
            done = await conn.execute(f"UPDATE {table} SET updated_at = updated_at WHERE id = $1", rid)
        return done.endswith(" 1")

    try:
        for t, slug in ((tenant, "mirror"), (other, "mirror-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Mirror')", uuid.UUID(t), f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, uuid.UUID(other if who == "d" else tenant), agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for k, cid in clients.items():
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                                cid, uuid.UUID(tenant), f"Client {k}", agent[k])
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, granted_by) "
            "VALUES ($1,'client',$2,$3,'view',$4)", uuid.UUID(tenant), clients["b"], users["c"], agent["b"])
        for k, lid in leads.items():
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id) "
                "VALUES ($1,$2,$3,'DE',0,$4)", lid, uuid.UUID(other if k == "other" else tenant),
                f"MIRROR-{tag}-{k}", clients.get(k))
        for k, lid in listings.items():
            await admin.execute(
                "INSERT INTO listings (id, tenant_id, address, is_shared_mls) VALUES ($1,$2,$3,$4)",
                lid, uuid.UUID(tenant if k == "own" else other), f"{tag} {k} St", k == "pool")
        await admin.execute(
            "INSERT INTO listing_grants (listing_id, grantor_tenant_id, grantee_tenant_id) VALUES ($1,$2,$3)",
            listings["granted"], uuid.UUID(other), uuid.UUID(tenant))

        client_rows = {r["id"]: dict(r) for r in await admin.fetch(
            "SELECT id, tenant_id::text, assignee_user_id FROM clients WHERE id = ANY($1::uuid[])", list(clients.values()))}
        lead_rows = {r["id"]: dict(r) for r in await admin.fetch(
            "SELECT id, tenant_id::text, seller_client_id FROM leads WHERE id = ANY($1::uuid[])", list(leads.values()))}
        listing_rows = {r["id"]: dict(r) for r in await admin.fetch(
            "SELECT id, tenant_id::text, is_shared_mls FROM listings WHERE id = ANY($1::uuid[])", list(listings.values()))}
        shares = {str(users["c"]): {str(clients["b"]): "view"}}

        mismatches = []
        for who, c in actors.items():
            uid = actor_user[who]

            # Tenant wall: nothing outside it is ever visible.
            for table, rows in (("clients", client_rows), ("leads", lead_rows)):
                visible = await seen(c, table, rows)
                for rid, row in rows.items():
                    if rid in visible and not tenancy.can_read_private(c, row):
                        mismatches.append(("can_read_private", who, table, rid))

            # Leads follow their seller client (0143).
            visible = await seen(c, "leads", lead_rows)
            for rid, row in lead_rows.items():
                seller = client_rows.get(row["seller_client_id"])
                seller_access = (tenancy.client_access(c, {**seller, "id": str(seller["id"])}, uid, shares.get(uid))
                                 if seller else None)
                mirror = tenancy.lead_access(c, row, seller_access)
                if (mirror is not None) != (rid in visible):
                    mismatches.append(("lead_access read", who, rid, mirror, rid in visible))
                if (mirror == "edit") != await updatable(c, "leads", rid):
                    mismatches.append(("lead_access write", who, rid, mirror))

            # Listings: read (listings_read) and write (listings_write).
            granted = frozenset(r["listing_id"] for r in await admin.fetch(
                "SELECT listing_id FROM listing_grants WHERE grantee_tenant_id = $1::uuid", c.tenant_id))
            visible = await seen(c, "listings", listing_rows)
            for rid, row in listing_rows.items():
                if tenancy.can_read_listing(c, row, granted) != (rid in visible):
                    mismatches.append(("can_read_listing", who, rid))
                if tenancy.can_write(c, row) != await updatable(c, "listings", rid):
                    mismatches.append(("can_write", who, rid))
        assert mismatches == [], mismatches
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for t in (tenant, other):
                for table in ("listing_grants", "listings", "leads", "record_shares", "clients", "users"):
                    col = "grantee_tenant_id" if table == "listing_grants" else "tenant_id"
                    await admin.execute(f"DELETE FROM {table} WHERE {col} = $1::uuid", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
