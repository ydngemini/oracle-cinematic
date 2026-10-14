"""Each agent sees their own contacts, the brokerage pool, and what is shared
with them — against real PostgreSQL with FORCE RLS, as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03 AC03.01/AC03.02 for agent_contacts and its intake tables (0134),
owner decision D-2026-10-13-01: unassigned = brokerage pool; an agent-created
contact is not auto-assigned; shares are view or edit, chosen per share. The
sibling of test_brokerage_access_matrix_live.py (clients, 0131).

Actors: agent A (assignee), agent B (not assigned), agent C (shared), the
broker owner, a platform admin, an agent of another brokerage, and a
brokerage-scope pipeline (inbound SMS/voice matching and intake).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

CHILD_TABLES = (
    "contact_property_relationships", "contact_intake_sessions",
    "intake_handoff_tasks", "contact_nurture_jobs",
)
MASTER = "e" * 64  # throwaway contact-encryption key for this test only


def test_agents_see_their_own_pool_and_shared_contacts_only(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


def test_contact_policy_composition_cannot_widen():
    asyncio.run(_policy_composition())


async def _policy_composition():
    """AC03.02: agent_contacts has exactly one permissive policy (the tenant
    wall) and four restrictive agent-scope ones; every intake table carries its
    tenant wall plus four restrictive parent policies. A new permissive policy
    would OR around them (F03.01)."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", ["agent_contacts", *CHILD_TABLES])
        kinds = await admin.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='record_shares_resource_kind_check'")
    finally:
        await admin.close()
    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r["tablename"], []).append(r)
    contacts = {(r["policyname"], r["permissive"], r["cmd"]) for r in by_table["agent_contacts"]}
    assert contacts == {
        ("agent_contacts_tenant_isolation", "PERMISSIVE", "ALL"),
        ("agent_contacts_agent_read", "RESTRICTIVE", "SELECT"),
        ("agent_contacts_agent_insert", "RESTRICTIVE", "INSERT"),
        ("agent_contacts_agent_update", "RESTRICTIVE", "UPDATE"),
        ("agent_contacts_agent_delete", "RESTRICTIVE", "DELETE"),
    }, contacts
    for t in CHILD_TABLES:
        permissive = {r["policyname"] for r in by_table[t] if r["permissive"] == "PERMISSIVE"}
        assert permissive == {f"{t}_tenant_isolation"}, (t, permissive)
        restrictive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "RESTRICTIVE"}
        assert restrictive == {(f"{t}_contact_read", "SELECT"), (f"{t}_contact_insert", "INSERT"),
                               (f"{t}_contact_update", "UPDATE"), (f"{t}_contact_delete", "DELETE")}, (t, restrictive)
    assert "'client'" in kinds and "'contact'" in kinds, kinds


async def _scenario():
    import asyncpg
    from fastapi import HTTPException

    import contacts_api
    import lead_routing_api
    import record_access
    import tenancy
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    ids = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d", "gone")}
    agent = {k: f"{k}-{tag}@contacts.test" for k in ids}
    k_a, k_b, k_pool, k_exp = (str(uuid.uuid4()) for _ in range(4))
    client_a = str(uuid.uuid4())
    mine = {k_a, k_b, k_pool, k_exp}

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who].upper() if who == "a" else agent[who],  # case must not matter
                             tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="contacts-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"inbound:{tag}", tenant_id=tenant, role=Role.AGENT))

    async def visible(c: TenantContext, table: str = "agent_contacts", col: str = "id") -> set[str]:
        async with connection.tenant_tx(c) as conn:
            rows = await conn.fetch(f"SELECT {col} AS id FROM {table} WHERE tenant_id = ANY($1::uuid[])",
                                    [tenant, other_tenant])
        return {str(r["id"]) for r in rows} & mine

    async def level(c: TenantContext, contact: str):
        async with connection.tenant_tx(c) as conn:
            return await record_access.contact_access_level(conn, contact)

    async def insert_relationship(c: TenantContext, contact: str):
        async with connection.tenant_tx(c) as conn:
            await conn.execute(
                "INSERT INTO contact_property_relationships (tenant_id, contact_id, property_ref_kind, "
                "property_label_ciphertext, relationship_type) VALUES ($1,$2,'manual','\\x00'::bytea,'owner')",
                tenant, uuid.UUID(contact))

    try:
        for t, slug in ((tenant, "contacts"), (other_tenant, "contacts-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Contact matrix')",
                                t, f"{slug}-{tag}")
        for who, uid in ids.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,$6)",
                uid, other_tenant if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}", who != "gone")
        # The trigger resolves the text assignee (any case) to the user.
        for cid, assignee in ((k_a, agent["a"].upper()), (k_b, agent["b"]), (k_pool, None), (k_exp, agent["a"])):
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'matrix','pending_encryption')", cid, tenant, assignee)
        row = await admin.fetchrow("SELECT assigned_agent_id, assigned_user_id FROM agent_contacts WHERE id=$1",
                                   uuid.UUID(k_a))
        assert row["assigned_agent_id"] == agent["a"] and row["assigned_user_id"] == ids["a"], "not canonicalized"
        for nobody in ("nobody@x.test", agent["d"], agent["gone"]):  # unknown, other brokerage, inactive
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await admin.execute("UPDATE agent_contacts SET assigned_agent_id=$2 WHERE id=$1",
                                    uuid.UUID(k_b), nobody)
        # The derived column cannot be written around the trigger.
        await admin.execute("UPDATE agent_contacts SET assigned_user_id=$2 WHERE id=$1", uuid.UUID(k_b), ids["c"])
        assert await admin.fetchval("SELECT assigned_user_id FROM agent_contacts WHERE id=$1",
                                    uuid.UUID(k_b)) == ids["b"]

        await admin.execute(
            "INSERT INTO contact_intake_sessions (tenant_id, contact_id, persona, raw_answers_ciphertext, "
            "normalized_fields_ciphertext, transcript_ciphertext, created_by) "
            "VALUES ($1,$2,'buyer','\\x00','\\x00','\\x00',$3)", tenant, uuid.UUID(k_a), agent["a"])
        await admin.execute(
            "INSERT INTO contact_nurture_jobs (tenant_id, contact_id, event_type, channel, calendar_year, "
            "idempotency_key, scheduled_for) VALUES ($1,$2,'birthday','email',2026,$3,now())",
            tenant, uuid.UUID(k_a), f"matrix-{tag}")
        # An expired share grants nothing.
        await admin.execute(
            """INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission,
                                          granted_by, created_at, expires_at)
               VALUES ($1,'contact',$2,$3,'edit','seed', now()-interval '2 days', now()-interval '1 day')""",
            tenant, k_exp, ids["b"])
        # A CLIENT share never opens a contact with the same id space.
        await admin.execute(
            """INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission,
                                          granted_by) VALUES ($1,'client',$2,$3,'edit','seed')""",
            tenant, k_b, ids["c"])

        # ── the matrix before any share ──────────────────────────────────────
        assert await visible(ctx("a")) == {k_a, k_pool, k_exp}
        assert await visible(ctx("b")) == {k_b, k_pool}, "expired share or colleague's contact leaked"
        assert await visible(ctx("c")) == {k_pool}, "a client share opened a contact"
        assert await visible(ctx("owner")) == mine
        assert await visible(admin_ctx) == mine
        assert await visible(pipeline) == mine
        assert await visible(ctx("d")) == set(), "another brokerage saw these contacts"
        assert await level(ctx("b"), k_a) is None and await level(ctx("b"), k_pool) == "edit"

        # Intake tables inherit: B reads none of A's contact's intake or nurture
        # rows, and cannot write one; A and the pipeline see them.
        for t in ("contact_intake_sessions", "contact_nurture_jobs"):
            assert await visible(ctx("b"), t, "contact_id") == set(), t
            assert await visible(ctx("a"), t, "contact_id") == {k_a}, t
            assert await visible(pipeline, t, "contact_id") == {k_a}, t
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await insert_relationship(ctx("b"), k_a)
        async with connection.tenant_tx(ctx("b")) as conn:  # a silent UPDATE by B touches nothing
            assert await conn.execute("UPDATE agent_contacts SET timezone='UTC' WHERE id=$1",
                                      uuid.UUID(k_a)) == "UPDATE 0"
        with pytest.raises(HTTPException) as e:  # through the route: 404, existence not revealed
            await contacts_api.get_contact(k_a, ctx=ctx("b"))
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            await contacts_api.update_contact(k_a, contacts_api.ContactPatch(timezone="UTC"), ctx=ctx("b"))
        assert e.value.status_code == 404
        listed = await contacts_api.list_contacts(q=None, limit=200, cursor=None, ctx=ctx("b"))
        assert {c["id"] for c in listed["contacts"]} & mine == {k_b, k_pool}

        # The pipeline (inbound SMS/voice intake) writes for the brokerage.
        await insert_relationship(pipeline, k_b)

        # ── sharing ───────────────────────────────────────────────────────────
        share = record_access.ShareCreate
        with pytest.raises(HTTPException) as e:  # B cannot even see it
            await record_access.share_contact(k_a, share(grantee=agent["c"], permission="view"), ctx=ctx("b"))
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:  # the pool is already everyone's
            await record_access.share_contact(k_pool, share(grantee=agent["c"], permission="view"), ctx=ctx("owner"))
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:  # another brokerage's agent is not a member
            await record_access.share_contact(k_a, share(grantee=agent["d"], permission="view"), ctx=ctx("a"))
        assert e.value.status_code == 422

        granted = await record_access.share_contact(k_a, share(grantee=agent["c"], permission="view"), ctx=ctx("a"))
        assert granted["permission"] == "view" and granted["grantee_agent_id"] == agent["c"]
        assert await admin.fetchval("SELECT resource_kind FROM record_shares WHERE id=$1",
                                    uuid.UUID(granted["id"])) == "contact"
        assert await visible(ctx("c")) == {k_a, k_pool}
        assert await level(ctx("c"), k_a) == "view"
        assert await visible(ctx("c"), "contact_intake_sessions", "contact_id") == {k_a}
        got = await contacts_api.get_contact(k_a, ctx=ctx("c"))
        assert got["contact"]["access"] == "view" and got["contact"]["pool"] is False
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # view means view, at the database too
            await insert_relationship(ctx("c"), k_a)
        for call in (  # and through every write route: 403 with the reason, never a silent no-op
            contacts_api.update_contact(k_a, contacts_api.ContactPatch(timezone="UTC"), ctx=ctx("c")),
            contacts_api.create_intake(
                k_a, contacts_api.IntakeSubmission(persona="seller",
                                                   answers=["1 Main St, Dover, DE", "Within 60 days", "A clean sale"]),
                ctx=ctx("c")),
            contacts_api.reserve_nurture_job(
                k_a, contacts_api.NurtureJobRequest(event_type="birthday", channel="email"), ctx=ctx("c")),
            contacts_api.create_property_relationship(
                k_a, contacts_api.PropertyRelationshipCreate(property_ref_kind="manual", property_label="Home",
                                                             relationship_type="owner"), ctx=ctx("c")),
        ):
            with pytest.raises(HTTPException) as e:
                await call
            assert e.value.status_code == 403 and "view only" in e.value.detail, e.value.detail

        # Upgrade to edit: a NEW grant row, the old one revoked (history kept).
        await record_access.share_contact(k_a, share(grantee=agent["c"], permission="edit"), ctx=ctx("a"))
        assert await admin.fetchval(
            "SELECT count(*) FROM record_shares WHERE resource_id=$1 AND grantee_user_id=$2", uuid.UUID(k_a), ids["c"]
        ) == 2
        assert await level(ctx("c"), k_a) == "edit"
        await insert_relationship(ctx("c"), k_a)
        patched = await contacts_api.update_contact(k_a, contacts_api.ContactPatch(timezone="America/New_York"),
                                                    ctx=ctx("c"))
        assert patched["contact"]["timezone"] == "America/New_York" and patched["contact"]["access"] == "edit"
        with pytest.raises(HTTPException) as e:  # an edit share does not let C re-share
            await record_access.share_contact(k_a, share(grantee=agent["b"], permission="view"), ctx=ctx("c"))
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e:  # nor take the contact over
            await contacts_api.update_contact(k_a, contacts_api.ContactPatch(assigned_agent_id=agent["c"]),
                                              ctx=ctx("c"))
        assert e.value.status_code == 403

        # ── the Python mirror agrees with the database, actor by actor ────────
        shares_by_user: dict[str, dict[str, str]] = {}
        for r in await admin.fetch("SELECT grantee_user_id, resource_id, permission FROM record_shares "
                                   "WHERE tenant_id=$1 AND resource_kind='contact' AND revoked_at IS NULL "
                                   "AND (expires_at IS NULL OR expires_at > now())", uuid.UUID(tenant)):
            shares_by_user.setdefault(str(r["grantee_user_id"]), {})[str(r["resource_id"])] = r["permission"]
        rows = {str(r["id"]): dict(r) for r in await admin.fetch(
            "SELECT id, tenant_id, assigned_user_id FROM agent_contacts WHERE id = ANY($1::uuid[])",
            [uuid.UUID(x) for x in mine])}
        for who in ("a", "b", "c", "owner", "d"):
            c = ctx(who)
            # The inline policy (what a query sees) and app_contact_access()
            # (what the API asks) must give the same answer.
            seen = await visible(c)
            for cid in rows:
                assert (cid in seen) == (await level(c, cid) is not None), (who, cid)
            for cid, r in rows.items():
                mirror = tenancy.contact_access(c, {**r, "tenant_id": str(r["tenant_id"])}, str(ids[who]),
                                                shares_by_user.get(str(ids[who]), {}))
                assert mirror == await level(c, cid), (who, cid, mirror)

        # ── revoke ────────────────────────────────────────────────────────────
        access = await record_access.get_contact_access(k_a, ctx=ctx("a"))
        assert access["can_manage_shares"] and [s["permission"] for s in access["shares"]] == ["edit"]
        assert access["contact_id"] == k_a and access["assignee_id"] == agent["a"]
        await record_access.revoke_contact_share(k_a, access["shares"][0]["id"], ctx=ctx("a"))
        assert await visible(ctx("c")) == {k_pool}

        # ── creating and assigning ────────────────────────────────────────────
        # An agent-created contact is not auto-assigned: it lands in the pool.
        made = await contacts_api.create_contact(contacts_api.ContactCreate(full_name="Dana Pool"), ctx=ctx("b"))
        mine.add(made["contact"]["id"])
        assert made["contact"]["assigned_agent_id"] is None and made["contact"]["pool"] is True
        assert made["contact"]["id"] in await visible(ctx("c"))
        # ...but a contact for an assigned client stays with that client's agent.
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Ana',$3)",
                            client_a, tenant, agent["a"])
        linked = await contacts_api.create_contact(
            contacts_api.ContactCreate(full_name="Ana Linked", client_id=client_a), ctx=ctx("a"))
        mine.add(linked["contact"]["id"])
        assert linked["contact"]["assigned_agent_id"] == agent["a"]
        assert linked["contact"]["id"] not in await visible(ctx("b"))
        with pytest.raises(HTTPException) as e:  # an agent cannot create one for a colleague
            await contacts_api.create_contact(
                contacts_api.ContactCreate(full_name="Gift", assigned_agent_id=agent["b"]), ctx=ctx("a"))
        assert e.value.status_code == 403
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # and the database agrees
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute("INSERT INTO agent_contacts (tenant_id, assigned_agent_id, data_state) "
                                   "VALUES ($1,$2,'pending_encryption')", tenant, agent["b"])
        with pytest.raises(HTTPException) as e:  # nor link one to a client they cannot see
            await contacts_api.create_contact(
                contacts_api.ContactCreate(full_name="Peek", client_id=client_a), ctx=ctx("b"))
        assert e.value.status_code == 404

        with pytest.raises(HTTPException) as e:  # an agent cannot hand a contact to a colleague
            await contacts_api.update_contact(k_a, contacts_api.ContactPatch(assigned_agent_id=agent["b"]),
                                              ctx=ctx("a"))
        assert e.value.status_code == 403
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # and the database agrees
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute("UPDATE agent_contacts SET assigned_agent_id=$2 WHERE id=$1",
                                   uuid.UUID(k_a), agent["b"])
        with pytest.raises(HTTPException) as e:
            await contacts_api.update_contact(k_a, contacts_api.ContactPatch(assigned_agent_id="nobody@x.test"),
                                              ctx=ctx("owner"))
        assert e.value.status_code == 422
        taken = await contacts_api.update_contact(
            k_pool, contacts_api.ContactPatch(assigned_agent_id=agent["b"].upper()), ctx=ctx("b"))
        assert taken["contact"]["assigned_agent_id"] == agent["b"], taken  # B takes a pool contact, canonically
        assert k_pool not in await visible(ctx("a")), "a taken contact stayed in A's view"
        await contacts_api.update_contact(k_pool, contacts_api.ContactPatch(assigned_agent_id=None), ctx=ctx("b"))
        assert k_pool in await visible(ctx("a"))
        moved = await contacts_api.update_contact(k_a, contacts_api.ContactPatch(assigned_agent_id=agent["b"]),
                                                  ctx=ctx("owner"))
        assert moved["contact"]["assigned_agent_id"] == agent["b"]
        assert k_a not in await visible(ctx("a")) and k_a in await visible(ctx("b"))
        assert await visible(ctx("a"), "contact_intake_sessions", "contact_id") == set(), "intake stayed behind"

        # ── brokerage-wide reads that are not "seeing the contact" ────────────
        # Lead-routing workload counts every assignee's contacts, whoever asks.
        async with connection.tenant_tx(ctx("c")) as conn:
            counts = {str(r["assigned_user_id"]): r["open_contacts"]
                      for r in await conn.fetch("SELECT * FROM app_open_contact_counts()")}
            # ...and the function's brokerage scope does not leak into the caller's.
            assert await conn.fetchval("SELECT count(*) FROM agent_contacts WHERE id=$1", uuid.UUID(k_a)) == 0
        assert counts.get(str(ids["b"])) == 2 and counts.get(str(ids["a"])) == 2, counts  # k_b+k_a / k_exp+linked
        routing = await lead_routing_api.list_routing_agents(ctx=ctx("c"))
        by_agent = {r["agent_id"]: r["assigned_open_contacts"] for r in routing["agents"]}
        assert by_agent[agent["b"]] == 2, by_agent
        # Quiet-hours inputs for a client the agent can see, from a contact they cannot.
        await admin.execute("UPDATE agent_contacts SET state_code='DE', timezone='America/New_York' WHERE id=$1",
                            uuid.UUID(linked["contact"]["id"]))
        await admin.execute("UPDATE clients SET assignee_id=NULL WHERE id=$1", uuid.UUID(client_a))  # to the pool
        # 0137 carried the contact to the pool with its client; put it back with
        # A so the client is visible to C while its contact is not (the drift
        # app_client_contact_place exists for).
        await admin.execute("UPDATE agent_contacts SET assigned_agent_id=$2 WHERE id=$1",
                            uuid.UUID(linked["contact"]["id"]), agent["a"])
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM agent_contacts WHERE id=$1",
                                       uuid.UUID(linked["contact"]["id"])) == 0
            place = await conn.fetchrow("SELECT state_code, timezone FROM app_client_contact_place($1::uuid)",
                                        client_a)
        assert place["state_code"] == "DE" and place["timezone"] == "America/New_York"

        # A user session never gets brokerage scope, even if asked.
        sneaky = brokerage_scope(TenantContext(agent_id=agent["c"], tenant_id=tenant, role=Role.AGENT,
                                               verify_session=True, user_id=str(ids["c"]), session_epoch=0))
        assert await visible(sneaky) == {k_pool, made["contact"]["id"]}
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
