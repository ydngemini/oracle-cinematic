"""A lead that is a client's own house follows that client; harvested
prospects stay the brokerage pool (0143). Against real PostgreSQL with FORCE
RLS, as the application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03, owner decision D-2026-10-13-01, ledger L03-07. The sibling of
test_communications_access_matrix_live.py (0140).

Actors: agent A, agent B, agent C (B's client and contact shared with C to
VIEW), the broker owner, a platform admin, an agent of another brokerage, and a
brokerage-scope pipeline.

Leads: one per client (A's, B's, the pool's — leads.seller_client_id) and one
harvested prospect with no client. Each has an interaction-log row and a photo
on it; a listing-only photo of B's listing is unchanged (listings stay
brokerage-wide). lead_intake_events follow their contact.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

# table -> (permissive tenant-wall policy, restrictive 0143 prefix)
SCOPED = {
    "leads": ("leads_tenant_isolation", "leads_linked"),
    "lead_intake_events": ("lead_intake_events_tenant_isolation", "lead_intake_events_linked"),
    "property_media": ("property_media_tenant_isolation", "property_media_linked"),
}
KINDS = ("a", "b", "pool", "harvest")  # harvest = no client


def test_leads_follow_their_client(monkeypatch):
    frames: list[dict] = []

    async def capture(tenant_id, payload, **audience):
        frames.append(payload)
        return 0

    import ws_hub

    monkeypatch.setattr(ws_hub, "broadcast", capture)
    # Lead frames follow the lead since 0151 (test_lead_children_access_live
    # proves who the audience is); what the frame says is checked here.
    monkeypatch.setattr(ws_hub, "broadcast_record", capture)
    asyncio.run(_scenario(frames))


def test_lead_policy_composition_cannot_widen():
    asyncio.run(_policy_composition())


async def _policy_composition():
    """Each table keeps exactly one permissive tenant wall plus four
    restrictive 0143 policies (a new permissive policy would OR around them,
    F03.01). interaction_logs keeps 0131's client policies and adds the lead
    ones."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", [*SCOPED, "interaction_logs"])
    finally:
        await admin.close()
    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r["tablename"], []).append(r)

    def four(prefix: str) -> set[tuple[str, str]]:
        return {(f"{prefix}_read", "SELECT"), (f"{prefix}_insert", "INSERT"),
                (f"{prefix}_update", "UPDATE"), (f"{prefix}_delete", "DELETE")}

    for t, (wall, prefix) in SCOPED.items():
        permissive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "PERMISSIVE"}
        assert permissive == {(wall, "ALL")}, (t, permissive)
        restrictive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "RESTRICTIVE"}
        assert restrictive == four(prefix), (t, restrictive)
    restrictive = {(r["policyname"], r["cmd"]) for r in by_table["interaction_logs"]
                   if r["permissive"] == "RESTRICTIVE"}
    assert restrictive == four("interaction_logs_client") | four("interaction_logs_lead"), restrictive


async def _scenario(frames: list[dict]):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import lead_dossier
    import media_api
    import property_view_api
    import record_access
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@leads.test" for k in users}
    people = {k: {"client": uuid.uuid4(), "contact": uuid.uuid4()} for k in ("a", "b", "pool")}
    assignee = {"a": agent["a"], "b": agent["b"], "pool": None}
    address_b = f"{tag} Private Lane, Dover, DE"
    pv_parcel_b = f"pv:{hashlib.sha256(address_b.lower().encode()).hexdigest()[:32]}"

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="leads-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"pipeline:{tag}", tenant_id=tenant, role=Role.AGENT))
    actors = {"a": ctx("a"), "b": ctx("b"), "c": ctx("c"), "owner": ctx("owner"), "d": ctx("d"),
              "admin": admin_ctx, "pipeline": pipeline}

    lead = {k: uuid.uuid4() for k in KINDS}
    log = {k: uuid.uuid4() for k in KINDS}
    media = {k: uuid.uuid4() for k in KINDS}
    intake = {k: uuid.uuid4() for k in ("a", "b", "pool", "none")}
    listing_b, listing_media_b = uuid.uuid4(), uuid.uuid4()

    async def visible(c: TenantContext, table: str, ids) -> set:
        async with connection.tenant_tx(c) as conn:
            found = await conn.fetch(f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", list(ids))
        return {r["id"] for r in found}

    try:
        for t, slug in ((tenant, "leads"), (other_tenant, "leads-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Lead matrix')",
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
                p["client"], tenant, f"Lead {k}", assignee[k], p["contact"])
        for kind in ("client", "contact"):
            await admin.execute(
                "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
                "granted_by) VALUES ($1,$2,$3,$4,'view',$5)", tenant, kind, people["b"][kind], users["c"], agent["b"])
        for k in KINDS:
            seller = people[k]["client"] if k in people else None
            parcel = pv_parcel_b if k == "b" else f"MATRIX-{tag}-{k}"
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5)", lead[k], tenant, parcel, seller,
                address_b if k == "b" else f"{tag} {k} Street, Dover, DE")
            await admin.execute(
                "INSERT INTO interaction_logs (id, tenant_id, lead_id, actor_role, interaction_type, payload) "
                "VALUES ($1,$2,$3,'agent','status_change','{}')", log[k], tenant, lead[k])
            await admin.execute(
                "INSERT INTO property_media (id, tenant_id, lead_id, kind, url) VALUES ($1,$2,$3,'photo',$4)",
                media[k], tenant, lead[k], f"/api/media/{media[k]}")
        await admin.execute(
            "INSERT INTO listings (id, tenant_id, address, seller_client_id) VALUES ($1,$2,$3,$4)",
            listing_b, tenant, address_b, people["b"]["client"])
        await admin.execute(
            "INSERT INTO property_media (id, tenant_id, listing_id, kind, url) VALUES ($1,$2,$3,'photo','/x')",
            listing_media_b, tenant, listing_b)
        # Intake events: the connector foreign key is not what this test is
        # about (replication-role replica skips it for these rows only).
        await admin.execute("SET session_replication_role = replica")
        for k, i in intake.items():
            await admin.execute(
                "INSERT INTO lead_intake_events (id, tenant_id, connector_id, external_event_id, payload_ciphertext, "
                "payload_digest, source_key, intent, contact_id) VALUES ($1,$2,$3,$4,$5,$6,'matrix','buyer',$7)",
                i, tenant, uuid.uuid4(), f"ev-{tag}-{k}", b"\x00", "0" * 64,
                people[k]["contact"] if k in people else None)
        await admin.execute("SET session_replication_role = origin")

        # ── read: each actor sees the leads of the clients they can see ─────
        def of(ids: dict, *ks: str) -> set:
            return {ids[k] for k in ks}

        for table, ids, extra in (("leads", lead, set()), ("interaction_logs", log, set()),
                                  ("property_media", media, {listing_media_b})):
            everything = of(ids, *KINDS) | extra
            expected = {
                "a": of(ids, "a", "pool", "harvest") | extra,
                "b": of(ids, "b", "pool", "harvest") | extra,
                "c": of(ids, "b", "pool", "harvest") | extra,  # B's client, shared view-only
                "owner": everything, "admin": everything, "pipeline": everything,
                "d": set(),  # another brokerage
            }
            for who, want in expected.items():
                got = await visible(actors[who], table, everything)
                assert got == want, (table, who, sorted(map(str, got)), sorted(map(str, want)))
        intake_all = set(intake.values())
        for who, ks in (("a", ("a", "pool", "none")), ("b", ("b", "pool", "none")), ("c", ("b", "pool", "none")),
                        ("owner", tuple(intake)), ("admin", tuple(intake)), ("pipeline", tuple(intake)), ("d", ())):
            got = await visible(actors[who], "lead_intake_events", intake_all)
            assert got == of(intake, *ks), ("lead_intake_events", who, got)

        # The pipeline page's own predicate (server.push_deal_pipeline) still
        # lists the pool and the harvested prospect, never a colleague's house.
        async with connection.tenant_tx(ctx("a")) as conn:
            page = {r["id"] for r in await conn.fetch(
                "SELECT id FROM leads WHERE tenant_id = ANY (app_visible_tenants()) "
                "ORDER BY motivation_score DESC, updated_at DESC, id DESC LIMIT 50")}
        assert page == of(lead, "a", "pool", "harvest"), page

        # The policy and app_linked_access() (what the API reads back) agree.
        for who, c in actors.items():
            if who == "d":
                continue  # the tenant wall, not this function, keeps another brokerage out
            seen = await visible(c, "leads", lead.values())
            async with connection.tenant_tx(c) as conn:
                for k in KINDS:
                    seller = people[k]["client"] if k in people else None
                    view = await conn.fetchval("SELECT app_linked_access($1, NULL, false)", seller)
                    edit = await conn.fetchval("SELECT app_linked_access($1, NULL, true)", seller)
                    assert view == (lead[k] in seen), (who, k, view)
                    assert edit == (view and not (who == "c" and k == "b")), (who, k, edit)
                    level = await record_access.linked_access_level(conn, "leads", str(lead[k]))
                    assert level == (None if not view else "edit" if edit else "view"), (who, k, level)
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await record_access.linked_access_level(conn, "property_media", str(media["b"])) == "view"
            assert await record_access.linked_access_level(conn, "property_media", str(media["a"])) is None
            assert await record_access.linked_access_level(conn, "lead_intake_events", str(intake["b"])) == "view"

        # ── write ───────────────────────────────────────────────────────────
        async with connection.tenant_tx(ctx("c")) as conn:  # a view share cannot change the house
            assert await conn.execute("UPDATE leads SET dossier_status='dead' WHERE id=$1", lead["b"]) == "UPDATE 0"
            assert await conn.execute("DELETE FROM property_media WHERE id=$1", media["b"]) == "DELETE 0"
        for sql, args in (
            ("INSERT INTO interaction_logs (tenant_id, lead_id, actor_role, interaction_type) "
             "VALUES ($1,$2,'agent','status_change')", (tenant, lead["b"])),
            ("INSERT INTO property_media (tenant_id, lead_id, kind, url) VALUES ($1,$2,'photo','/y')",
             (tenant, lead["b"])),
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.tenant_tx(ctx("c")) as conn:
                    await conn.execute(sql, *args)
        # An agent cannot attach a house to a colleague's client, new or harvested.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute(
                    "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score, seller_client_id) "
                    "VALUES ($1,$2,'DE',0,$3)", tenant, f"MATRIX-{tag}-sneak", people["b"]["client"])
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute("UPDATE leads SET seller_client_id=$1 WHERE id=$2",
                                   people["b"]["client"], lead["harvest"])
        async with connection.tenant_tx(ctx("a")) as conn:  # the pool and own houses: as before
            assert await conn.execute("UPDATE leads SET dossier_status='draft' WHERE id = ANY($1::uuid[])",
                                      [lead["harvest"], lead["pool"], lead["a"]]) == "UPDATE 3"
            await conn.execute("INSERT INTO interaction_logs (tenant_id, lead_id, actor_role, interaction_type) "
                               "VALUES ($1,$2,'agent','status_change')", tenant, lead["harvest"])
        async with connection.tenant_tx(ctx("b")) as conn:  # B on B's own house
            await conn.execute("INSERT INTO property_media (tenant_id, lead_id, kind, url) VALUES ($1,$2,'photo','/z')",
                               tenant, lead["b"])

        # Harvest (platform admin) still upserts every parcel, a client's house
        # included, and keeps its link: the exact statement persist_leads runs.
        async with connection.tenant_tx(admin_ctx) as conn:
            await conn.execute(
                "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score, underwriting, payload) "
                "VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb) "
                "ON CONFLICT (tenant_id, parcel_id) DO UPDATE SET "
                "motivation_score = EXCLUDED.motivation_score, underwriting = EXCLUDED.underwriting, "
                "payload = EXCLUDED.payload, updated_at = now()",
                tenant, pv_parcel_b, "DE", 77, "{}", "{}")
        row = await admin.fetchrow("SELECT motivation_score, seller_client_id FROM leads WHERE id=$1", lead["b"])
        assert row["motivation_score"] == 77 and row["seller_client_id"] == people["b"]["client"], dict(row)

        # ── the API: 404 when invisible, 403 when view-only ────────────────
        move = lead_dossier.DossierMove(status="dead")
        for who, code in (("a", 404), ("c", 403)):
            with pytest.raises(HTTPException) as e:
                await lead_dossier.move_dossier(str(lead["b"]), move, ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
        frames.clear()
        await lead_dossier.move_dossier(str(lead["b"]), move, ctx=ctx("b"))
        await lead_dossier.move_dossier(str(lead["harvest"]), move, ctx=ctx("a"))
        # The brokerage-wide frame names no client's house; a prospect's it does.
        by_lead = {f["lead_id"]: f for f in frames if f.get("type") == "DOSSIER_MOVED"}
        assert by_lead[str(lead["b"])]["address"] is None and by_lead[str(lead["b"])]["parcel_id"] is None
        assert by_lead[str(lead["harvest"])]["address"]
        for who, code in (("a", 404), ("c", 403)):
            with pytest.raises(HTTPException) as e:
                await media_api.delete_media(media["b"], ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
            with pytest.raises(HTTPException) as e:
                await property_view_api.create_upload_link(
                    property_view_api.CreateUploadLink(), lead_id=lead["b"], listing_id=None, ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
        assert await admin.fetchval("SELECT count(*) FROM property_media WHERE id=$1", media["b"]) == 1
        # Typing the address of a colleague's client's house: a clear 409, not
        # a 500 from the hidden duplicate, and never the colleague's lead id.
        subject = property_view_api.CreateSubject(address=address_b, state="DE")
        with pytest.raises(HTTPException) as e:
            await property_view_api.create_subject(subject, ctx=ctx("a"))
        assert e.value.status_code == 409, e.value.detail
        assert await property_view_api.create_subject(subject, ctx=ctx("b")) == {
            "lead_id": str(lead["b"]), "created": False, "address": address_b,
            "identity": {"status": "exact_address", "basis": "address_exact", "verified": False}}
        # The same street text for a house in another state is that house's own
        # record, never lead b (F11.04).
        elsewhere = await property_view_api.create_subject(
            property_view_api.CreateSubject(address=address_b, state="PA"), ctx=ctx("b"))
        assert elsewhere["created"] is True and elsewhere["lead_id"] != str(lead["b"])
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
