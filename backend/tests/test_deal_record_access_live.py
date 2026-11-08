"""A deal with no client follows its lead, and a deal's offers, milestones,
parties and closing checklist follow the deal (0155). Against real
PostgreSQL with FORCE RLS, as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03, owner decision D-2026-10-13-01, ledger L03-10. The sibling of
test_lead_children_access_live.py (0151): the lead rule here is 0143's.

Actors: agent A, agent B, agent C (B's client shared with C to VIEW), the
broker owner, a platform admin, an agent of another brokerage, and a
brokerage-scope pipeline. Deals, each with one offer, milestone, party and
checklist item:
  lead_<k>   no client, on the house of A's client / B's / the pool client's
             / a harvested prospect (no client), opened by the owner;
  client_b   B's client's deal (0131 already decides the deal; 0155 makes its
             children follow it);
  bare       no client and no lead: the brokerage pool, as before;
  a_on_b     no client, on B's client's house, opened by A (0131's
             "the agent who opened it" arm, kept).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

CHILDREN = ("transaction_offers", "transaction_milestones", "compliance_checklist_items",
            "transaction_parties")
LEADS = ("a", "b", "pool", "harvest")  # harvest = no client
DEALS = tuple(f"lead_{k}" for k in LEADS) + ("client_b", "bare", "a_on_b")


def test_deals_follow_their_lead_and_children_follow_the_deal():
    asyncio.run(_scenario())


def test_deal_policy_composition_cannot_widen():
    asyncio.run(_policy_composition())


async def _policy_composition():
    """One permissive tenant wall per table plus the restrictive 0131/0155
    policies (a new permissive policy would OR around them, F03.01)."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", ["transactions", *CHILDREN])
    finally:
        await admin.close()
    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r["tablename"], []).append(r)

    def four(prefix: str) -> set[tuple[str, str]]:
        return {(f"{prefix}_read", "SELECT"), (f"{prefix}_insert", "INSERT"),
                (f"{prefix}_update", "UPDATE"), (f"{prefix}_delete", "DELETE")}

    want = {
        "transactions": four("transactions_client") | four("transactions_lead"),
        "transaction_parties": four("transaction_parties_client") | four("transaction_parties_deal"),
        "transaction_offers": four("transaction_offers_deal"),
        "transaction_milestones": four("transaction_milestones_deal"),
        "compliance_checklist_items": four("compliance_checklist_items_deal"),
    }
    for t, expected in want.items():
        permissive = [r for r in by_table[t] if r["permissive"] == "PERMISSIVE"]
        assert len(permissive) == 1 and permissive[0]["cmd"] == "ALL", (t, by_table[t])
        restrictive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "RESTRICTIVE"}
        assert restrictive == expected, (t, restrictive ^ expected)


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import portfolio_api
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@deals.test" for k in users}
    people = {k: {"client": uuid.uuid4(), "contact": uuid.uuid4()} for k in ("a", "b", "pool")}
    assignee = {"a": agent["a"], "b": agent["b"], "pool": None}

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="deals-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"pipeline:{tag}", tenant_id=tenant, role=Role.AGENT))
    actors = {"a": ctx("a"), "b": ctx("b"), "c": ctx("c"), "owner": ctx("owner"), "d": ctx("d"),
              "admin": admin_ctx, "pipeline": pipeline}

    lead = {k: uuid.uuid4() for k in LEADS}
    deal = {k: uuid.uuid4() for k in DEALS}
    child = {t: {k: uuid.uuid4() for k in DEALS} for t in CHILDREN}

    async def visible(c: TenantContext, table: str, wanted) -> set:
        async with connection.tenant_tx(c) as conn:
            found = await conn.fetch(f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", list(wanted))
        return {r["id"] for r in found}

    try:
        # ── fixtures (superuser, outside RLS) ──────────────────────────────
        for t, slug in ((tenant, "deals"), (other_tenant, "deals-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Deal access')",
                                t, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other_tenant if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for k, p in people.items():
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'matrix','pending_encryption')", p["contact"], tenant, assignee[k])
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, contact_id) VALUES ($1,$2,$3,$4,$5)",
                p["client"], tenant, f"Deals {k}", assignee[k], p["contact"])
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'view',$4)", tenant, people["b"]["client"], users["c"], agent["b"])
        for k in LEADS:
            seller = people[k]["client"] if k in people else None
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5)", lead[k], tenant, f"DEAL-{tag}-{k}", seller,
                f"{tag} {k} Street, Dover, DE")
        shape = {  # deal -> (client, client_party_role, lead, created_by)
            **{f"lead_{k}": (None, None, lead[k], agent["owner"]) for k in LEADS},
            "client_b": (people["b"]["client"], "seller", None, agent["b"]),
            "bare": (None, None, None, agent["owner"]),
            "a_on_b": (None, None, lead["b"], agent["a"].upper()),  # creator matched case-insensitively
        }
        for k, (client, role, lead_id, creator) in shape.items():
            await admin.execute(
                "INSERT INTO transactions (id, tenant_id, state_code, client_id, client_party_role, lead_id, "
                "created_by, purchase_price) VALUES ($1,$2,'DE',$3,$4,$5,$6,400000)",
                deal[k], tenant, client, role, lead_id, creator)
            await admin.execute(
                "INSERT INTO transaction_offers (id, tenant_id, transaction_id, amount, created_by, updated_by) "
                "VALUES ($1,$2,$3,410000,'x','x')", child["transaction_offers"][k], tenant, deal[k])
            await admin.execute(
                "INSERT INTO transaction_milestones (id, tenant_id, transaction_id, milestone_type, title) "
                "VALUES ($1,$2,$3,'inspection','Inspection')", child["transaction_milestones"][k], tenant, deal[k])
            await admin.execute(
                "INSERT INTO compliance_checklist_items (id, tenant_id, transaction_id, disclosure_id, form_name) "
                "VALUES ($1,$2,$3,'DE-SPDS','Seller disclosure')",
                child["compliance_checklist_items"][k], tenant, deal[k])
            await admin.execute(
                "INSERT INTO transaction_parties (id, tenant_id, transaction_id, party_role, display_name, "
                "contact_ciphertext) VALUES ($1,$2,$3,'attorney','Closing attorney','\\x00'::bytea)",
                child["transaction_parties"][k], tenant, deal[k])

        # ── read: each actor sees exactly the deals they may, and their rows ─
        mine = {
            "a": {"lead_a", "lead_pool", "lead_harvest", "bare", "a_on_b"},
            "b": {"lead_b", "lead_pool", "lead_harvest", "bare", "client_b", "a_on_b"},
            "c": {"lead_b", "lead_pool", "lead_harvest", "bare", "client_b", "a_on_b"},  # B's, shared to view
            "owner": set(DEALS), "admin": set(DEALS), "pipeline": set(DEALS), "d": set(),
        }
        for who, keys in mine.items():
            got = await visible(actors[who], "transactions", deal.values())
            want = {deal[k] for k in keys}
            assert got == want, ("transactions", who, got ^ want)
            for table in CHILDREN:
                got = await visible(actors[who], table, child[table].values())
                want = {child[table][k] for k in keys}
                assert got == want, (table, who, got ^ want)

        # The routes answer 404 where the row is hidden, and the rows where it is not.
        for route in (portfolio_api.transaction_detail, portfolio_api.list_offers):
            with pytest.raises(HTTPException) as e:
                await route(deal["lead_b"], ctx("a"))
            assert e.value.status_code == 404
        detail = await portfolio_api.transaction_detail(deal["lead_b"], ctx("c"))
        assert len(detail["parties"]) == 1 and len(detail["milestones"]) == 1
        offers = await portfolio_api.list_offers(deal["client_b"], ctx("b"))
        assert offers["total"] == 1

        # ── write: a view share changes nothing; own, pool and opened deals as before ─
        async with connection.tenant_tx(ctx("c")) as conn:
            for k in ("lead_b", "client_b"):
                assert await conn.execute("UPDATE transactions SET notes = 'c' WHERE id = $1",
                                          deal[k]) == "UPDATE 0", k
                for table in CHILDREN:
                    assert await conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE id = $1",
                                              child[table][k]) == "UPDATE 0", (table, k)
                    assert await conn.execute(f"DELETE FROM {table} WHERE id = $1",
                                              child[table][k]) == "DELETE 0", (table, k)
        for who, keys in (("a", ("lead_a", "lead_pool", "lead_harvest", "bare", "a_on_b")),
                          ("b", ("lead_b", "client_b"))):
            async with connection.tenant_tx(ctx(who)) as conn:
                ids = [deal[k] for k in keys]
                assert await conn.execute("UPDATE transactions SET notes = $2 WHERE id = ANY($1::uuid[])",
                                          ids, who) == f"UPDATE {len(ids)}", who
                for table in CHILDREN:
                    rows = [child[table][k] for k in keys]
                    assert await conn.execute(
                        f"UPDATE {table} SET tenant_id = tenant_id WHERE id = ANY($1::uuid[])",
                        rows) == f"UPDATE {len(rows)}", (table, who)

        # An insert on a deal the caller cannot edit is refused by the policy.
        inserts = {
            "transaction_offers": "INSERT INTO transaction_offers (tenant_id, transaction_id, amount, created_by, "
                                  "updated_by) VALUES ($1,$2,1,'x','x')",
            "transaction_milestones": "INSERT INTO transaction_milestones (tenant_id, transaction_id, "
                                      "milestone_type, title) VALUES ($1,$2,'t','t')",
            "compliance_checklist_items": "INSERT INTO compliance_checklist_items (tenant_id, transaction_id, "
                                          "disclosure_id, form_name) VALUES ($1,$2,'d','f')",
            "transaction_parties": "INSERT INTO transaction_parties (tenant_id, transaction_id, party_role, "
                                   "display_name) VALUES ($1,$2,'title','t')",
        }
        for table, sql in inserts.items():
            for who, k in (("a", "lead_b"), ("c", "lead_b"), ("c", "client_b")):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with connection.tenant_tx(ctx(who)) as conn:
                        await conn.execute(sql, tenant, deal[k])
            async with connection.tenant_tx(ctx("b")) as conn:
                await conn.execute(sql, tenant, deal["lead_b"])
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute(sql, tenant, deal["a_on_b"])  # A opened it
        # Opening a deal on a house the caller cannot edit is refused too.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("c")) as conn:
                await conn.execute(
                    "INSERT INTO transactions (tenant_id, state_code, lead_id, created_by) VALUES ($1,'DE',$2,$3)",
                    tenant, lead["b"], agent["owner"])
        # The API route reports it as the server maps 42501 (403), never as a success.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await portfolio_api.add_milestone(
                deal["lead_b"], portfolio_api.MilestoneCreate(milestone_type="appraisal", title="Appraisal"),
                ctx("c"))
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
