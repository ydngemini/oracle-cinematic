"""The rows hanging off a lead follow the lead (0151): client-portal links,
contract documents, floor plans and their revisions, 360 scenes, upload links,
3D builds and capture sessions, tour variants, voice walkthroughs, video jobs
and lead-only calls. Against real PostgreSQL with FORCE RLS, as the
application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03, owner decision D-2026-10-13-01, ledger L03-09. The sibling of
test_lead_record_access_live.py (0143), whose lead rule these tables follow.

Actors: agent A, agent B, agent C (B's client shared with C to VIEW), the
broker owner, a platform admin, an agent of another brokerage, and a
brokerage-scope pipeline. Leads: A's client's house, B's, the pool client's,
and a harvested prospect (no client). Each lead has one row in every child
table; listing-only rows (and a video job whose source names no valid lead)
stay brokerage-wide.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

CHILDREN = ("client_portals", "contract_documents", "property_floorplans", "property_floorplan_revisions",
            "property_pano_scenes", "property_view_upload_links", "reconstruction_jobs",
            "spatial_tour_variants", "capture_sessions", "voice_walkthrough_jobs", "video_studio_jobs",
            "live_call_sessions")
KINDS = ("a", "b", "pool", "harvest")  # harvest = no client


def test_lead_children_follow_the_lead(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


def test_lead_child_policy_composition_cannot_widen():
    asyncio.run(_policy_composition())


async def _policy_composition():
    """One permissive tenant wall plus four restrictive 0151 policies per table
    (a new permissive policy would OR around them, F03.01). live_call_sessions
    keeps 0140's client/contact policies besides."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", list(CHILDREN))
    finally:
        await admin.close()
    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r["tablename"], []).append(r)

    def four(prefix: str) -> set[tuple[str, str]]:
        return {(f"{prefix}_read", "SELECT"), (f"{prefix}_insert", "INSERT"),
                (f"{prefix}_update", "UPDATE"), (f"{prefix}_delete", "DELETE")}

    for t in CHILDREN:
        permissive = {r["cmd"] for r in by_table[t] if r["permissive"] == "PERMISSIVE"}
        assert permissive == {"ALL"} and len([r for r in by_table[t] if r["permissive"] == "PERMISSIVE"]) == 1, \
            (t, by_table[t])
        restrictive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "RESTRICTIVE"}
        want = four(f"{t}_lead") | (four("live_call_sessions_linked") if t == "live_call_sessions" else set())
        assert restrictive == want, (t, restrictive)


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    import client_portal
    import contracts_api
    import property_view_api
    import record_access
    import space_assets
    import tour_api
    import video_studio
    import video_studio_api
    import voice_intel
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@children.test" for k in users}
    people = {k: {"client": uuid.uuid4(), "contact": uuid.uuid4()} for k in ("a", "b", "pool")}
    assignee = {"a": agent["a"], "b": agent["b"], "pool": None}

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="children-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"pipeline:{tag}", tenant_id=tenant, role=Role.AGENT))
    actors = {"a": ctx("a"), "b": ctx("b"), "c": ctx("c"), "owner": ctx("owner"), "d": ctx("d"),
              "admin": admin_ctx, "pipeline": pipeline}

    lead = {k: uuid.uuid4() for k in KINDS}
    ids: dict[str, dict[str, uuid.UUID]] = {t: {k: uuid.uuid4() for k in KINDS} for t in CHILDREN}
    media = {k: uuid.uuid4() for k in KINDS}
    splat_b = uuid.uuid4()
    token = {k: secrets.token_urlsafe(32) for k in KINDS}
    listing = uuid.uuid4()
    listing_media = uuid.uuid4()
    extra = {  # rows with no lead: brokerage-wide
        "property_floorplans": uuid.uuid4(), "property_pano_scenes": uuid.uuid4(),
        "property_view_upload_links": uuid.uuid4(), "reconstruction_jobs": uuid.uuid4(),
        "video_studio_jobs": uuid.uuid4(), "spatial_tour_variants": uuid.uuid4(),
        "capture_sessions": uuid.uuid4(),
    }
    malformed_video = uuid.uuid4()
    call_on_pool_client = uuid.uuid4()  # a pool client's call that mentions B's house
    queued_b = uuid.uuid4()

    async def visible(c: TenantContext, table: str, wanted) -> set:
        async with connection.tenant_tx(c) as conn:
            found = await conn.fetch(f"SELECT id FROM {table} WHERE id = ANY($1::uuid[])", list(wanted))
        return {r["id"] for r in found}

    async def refused(coro, code: int):
        with pytest.raises(HTTPException) as e:
            await coro
        assert e.value.status_code == code, e.value.detail

    try:
        # ── fixtures (superuser, outside RLS) ──────────────────────────────
        for t, slug in ((tenant, "children"), (other_tenant, "children-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Lead children')",
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
                p["client"], tenant, f"Children {k}", assignee[k], p["contact"])
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'view',$4)", tenant, people["b"]["client"], users["c"], agent["b"])
        await admin.execute("INSERT INTO listings (id, tenant_id, address) VALUES ($1,$2,$3)",
                            listing, tenant, f"{tag} Listing Road, Dover, DE")
        await admin.execute(
            "INSERT INTO property_media (id, tenant_id, listing_id, kind, url) VALUES ($1,$2,$3,'photo','/l')",
            listing_media, tenant, listing)
        for k in KINDS:
            seller = people[k]["client"] if k in people else None
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5)", lead[k], tenant, f"CHILD-{tag}-{k}", seller,
                f"{tag} {k} Street, Dover, DE")
            await _insert_children(admin, tenant, k, lead[k], ids, media[k],
                                   hashlib.sha256(token[k].encode()).hexdigest())
        await admin.execute(
            "INSERT INTO property_media (id, tenant_id, lead_id, kind, url, s3_key) "
            "VALUES ($1,$2,$3,'splat','/s','splats/b.sog')", splat_b, tenant, lead["b"])
        # No lead: a listing's plan, scene, upload link, build, variant, capture and video.
        await admin.execute("INSERT INTO property_floorplans (id, tenant_id, listing_id, document) "
                            "VALUES ($1,$2,$3,'{}')", extra["property_floorplans"], tenant, listing)
        await admin.execute("INSERT INTO property_pano_scenes (id, tenant_id, listing_id, media_id) "
                            "VALUES ($1,$2,$3,$4)", extra["property_pano_scenes"], tenant, listing, listing_media)
        await admin.execute(
            "INSERT INTO property_view_upload_links (id, tenant_id, listing_id, token_hash, expires_at) "
            "VALUES ($1,$2,$3,$4, now() + interval '1 day')",
            extra["property_view_upload_links"], tenant, listing, secrets.token_bytes(32))
        await admin.execute("INSERT INTO reconstruction_jobs (id, tenant_id, listing_id, status) "
                            "VALUES ($1,$2,$3,'failed')", extra["reconstruction_jobs"], tenant, listing)
        await admin.execute(
            "INSERT INTO spatial_tour_variants (id, tenant_id, listing_id, property_key, source_record_ids, "
            "source_media_ids, variant_name, style, rehab_scope, model_version, disclosure, created_by) "
            "VALUES ($1,$2,$3,'k',ARRAY[gen_random_uuid()],ARRAY[gen_random_uuid()],'v','modern','{}','m','d','x')",
            extra["spatial_tour_variants"], tenant, listing)
        await admin.execute("INSERT INTO capture_sessions (id, tenant_id, listing_id, status) "
                            "VALUES ($1,$2,$3,'succeeded')", extra["capture_sessions"], tenant, listing)
        for vid, source in ((extra["video_studio_jobs"], {"listing_id": str(listing)}),
                            (malformed_video, {"lead_id": "not-a-uuid"})):
            await admin.execute("INSERT INTO video_studio_jobs (id, tenant_id, kind, source, size, status) "
                                "VALUES ($1,$2,'text',$3::jsonb,'720x1280','failed')", vid, tenant, json.dumps(source))
        await admin.execute(
            "INSERT INTO live_call_sessions (id, tenant_id, client_id, lead_id, created_by) VALUES ($1,$2,$3,$4,'x')",
            call_on_pool_client, tenant, people["pool"]["client"], lead["b"])

        # ── read: each actor sees exactly the children of the leads they see ─
        def of(table: str, *ks: str) -> set:
            return {ids[table][k] for k in ks}

        for table in CHILDREN:
            also = {extra[table]} if table in extra else set()
            if table == "video_studio_jobs":
                also |= {malformed_video}
            if table == "live_call_sessions":
                also |= {call_on_pool_client}  # follows its client (0140), not the house it mentions
            everything = of(table, *KINDS) | also
            expected = {
                "a": of(table, "a", "pool", "harvest") | also,
                "b": of(table, "b", "pool", "harvest") | also,
                "c": of(table, "b", "pool", "harvest") | also,  # B's client, shared view-only
                "owner": everything, "admin": everything, "pipeline": everything,
                "d": set(),
            }
            for who, want in expected.items():
                got = await visible(actors[who], table, everything)
                assert got == want, (table, who, sorted(map(str, got - want)), sorted(map(str, want - got)))

        # The tour's own reads (tour_api.fetch_tour_rows) for B's house, as A: nothing.
        async with connection.tenant_tx(ctx("a")) as conn:
            rows, scenes, plan = await tour_api.fetch_tour_rows(conn, lead["b"], None)
        assert rows == [] and scenes == [] and plan is None
        async with connection.tenant_tx(ctx("c")) as conn:
            rows, scenes, plan = await tour_api.fetch_tour_rows(conn, lead["b"], None)
        assert len(scenes) == 1 and plan is not None

        # The read-back the API uses agrees with the policy.
        for table in CHILDREN:
            async with connection.tenant_tx(ctx("c")) as conn:
                if table != "property_floorplan_revisions":
                    assert await record_access.linked_access_level(conn, table, str(ids[table]["b"])) == "view", table
                    assert await record_access.linked_access_level(conn, table, str(ids[table]["a"])) is None, table
                    assert await record_access.linked_access_level(conn, table, str(ids[table]["pool"])) == "edit", table
            async with connection.tenant_tx(ctx("b")) as conn:
                if table != "property_floorplan_revisions":
                    assert await record_access.linked_access_level(conn, table, str(ids[table]["b"])) == "edit", table
        async with connection.tenant_tx(ctx("a")) as conn:
            assert await record_access.linked_access_level(conn, "live_call_sessions", str(call_on_pool_client)) == "edit"

        # ── write: a view share cannot change any of them ───────────────────
        for table in CHILDREN:
            async with connection.tenant_tx(ctx("c")) as conn:
                assert await conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE id = $1",
                                          ids[table]["b"]) == "UPDATE 0", table
                assert await conn.execute(f"DELETE FROM {table} WHERE id = $1", ids[table]["b"]) == "DELETE 0", table
            async with connection.tenant_tx(ctx("a")) as conn:  # the pool's and a prospect's: as before
                assert await conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE id = ANY($1::uuid[])",
                                          [ids[table]["pool"], ids[table]["harvest"]]) == "UPDATE 2", table
        for sql, args in (
            ("INSERT INTO client_portals (tenant_id, lead_id, token_hash, access_expires_at) "
             "VALUES ($1,$2,$3, now() + interval '1 day')", (tenant, lead["b"], "f" * 64)),
            ("INSERT INTO property_floorplan_revisions (tenant_id, floorplan_id, revision, document) "
             "VALUES ($1,$2,2,'{}')", (tenant, ids["property_floorplans"]["b"])),
            ("INSERT INTO reconstruction_jobs (tenant_id, lead_id, status) VALUES ($1,$2,'failed')",
             (tenant, lead["b"])),
            ("INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, model_version, created_by) "
             "VALUES ($1,$2,'consent','m','x')", (tenant, ids["live_call_sessions"]["b"])),
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.tenant_tx(ctx("c")) as conn:
                    await conn.execute(sql, *args)
        async with connection.tenant_tx(ctx("b")) as conn:  # B on B's own house
            await conn.execute("INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, "
                               "model_version, created_by) VALUES ($1,$2,'consent','m','x')",
                               tenant, ids["live_call_sessions"]["b"])

        # ── the API: 404 when invisible, 403 when view-only, nothing changed ─
        portals_before = await admin.fetchval("SELECT count(*) FROM client_portals WHERE tenant_id=$1", tenant)
        monkeypatch.setattr(contracts_api, "require_feature", lambda feature: None)  # not the subject

        class _Provider:
            def available(self):
                return True, ""

        monkeypatch.setattr(tour_api, "get_provider", lambda: _Provider())
        for who, code in (("a", 404), ("c", 403)):
            await refused(client_portal.create_portal_link(
                client_portal.PortalLinkRequest(lead_id=lead["b"]), ctx=ctx(who)), code)
            await refused(client_portal.revoke_portal_link(ids["client_portals"]["b"], ctx=ctx(who)), code)
            await refused(contracts_api.edit_document(
                ids["contract_documents"]["b"],
                contracts_api.DocumentEdit(revised_text="x" * 30, attorney_reviewer="Counsel"), ctx=ctx(who)), code)
            await refused(property_view_api.revoke_upload_link(ids["property_view_upload_links"]["b"],
                                                               ctx=ctx(who)), code)
            await refused(tour_api.enqueue_reconstruction(lead_id=lead["b"], listing_id=None, confirm_rebuild=True,
                                                          idempotency_key=None, ctx=ctx(who)), code)
            await refused(tour_api.retry_reconstruction(ids["reconstruction_jobs"]["b"], ctx=ctx(who)), code)
            await refused(video_studio_api.cancel_video_job(ids["video_studio_jobs"]["b"], ctx=ctx(who)), code)
            await refused(video_studio_api.delete_video_job(ids["video_studio_jobs"]["b"], ctx=ctx(who)), code)
            await refused(voice_intel.log_walkthrough(
                str(lead["b"]), UploadFile(file=io.BytesIO(b"\x00" * 16), filename="walk.m4a"), ctx=ctx(who)), code)
        assert await admin.fetchval("SELECT count(*) FROM client_portals WHERE tenant_id=$1", tenant) == portals_before
        assert await admin.fetchval("SELECT revoked_at FROM client_portals WHERE id=$1",
                                    ids["client_portals"]["b"]) is None
        assert await admin.fetchval("SELECT revoked_at FROM property_view_upload_links WHERE id=$1",
                                    ids["property_view_upload_links"]["b"]) is None
        assert await admin.fetchval("SELECT count(*) FROM reconstruction_jobs WHERE lead_id=$1", lead["b"]) == 1
        assert await admin.fetchval("SELECT count(*) FROM video_studio_jobs WHERE id=$1",
                                    ids["video_studio_jobs"]["b"]) == 1
        assert await admin.fetchval("SELECT count(*) FROM voice_walkthrough_jobs WHERE lead_id=$1", lead["b"]) == 1
        # ── capability URLs and workers still reach a client's house ────────
        session = await client_portal.open_portal_session(token["b"])  # SECURITY DEFINER resolver
        assert session.lead_id == str(lead["b"])
        monkeypatch.setattr(space_assets, "verify", lambda *a: True)
        monkeypatch.setattr(space_assets, "read_range", lambda key, rng: (b"SPLAT", 5))

        class _Req:
            headers: dict = {}

        resp = await tour_api.signed_space_asset(splat_b, _Req(), t=tenant, exp=int(time.time()) + 60, sig="x")
        assert resp.body == b"SPLAT", "the signed stream of a client's house's space"
        # B, who may edit the house, gets past the check (a revoked link is the proof).
        await client_portal.revoke_portal_link(ids["client_portals"]["b"], ctx=ctx("b"))
        assert await admin.fetchval("SELECT revoked_at FROM client_portals WHERE id=$1",
                                    ids["client_portals"]["b"]) is not None
        worker = video_studio._job_ctx({"tenant_id": tenant})
        assert ids["video_studio_jobs"]["b"] in await visible(worker, "video_studio_jobs",
                                                              [ids["video_studio_jobs"]["b"]])
        assert media["b"] in await visible(worker, "property_media", [media["b"]])

        # ── cost guards count the whole brokerage ───────────────────────────
        await admin.execute("INSERT INTO reconstruction_jobs (id, tenant_id, lead_id, status) "
                            "VALUES ($1,$2,$3,'queued')", queued_b, tenant, lead["b"])
        sql = "SELECT count(*) FROM reconstruction_jobs WHERE status = 'queued'"
        async with connection.tenant_tx(ctx("a")) as conn:
            assert await conn.fetchval(sql) == 0, "A cannot read B's build"
        assert await record_access.pipeline_count(tenant, sql) == 1, "but the backlog counts it"

        # ── live frames about a lead follow the lead's client ───────────────
        audience = await record_access.linked_audience(tenant, None, None, lead_id=lead["b"])
        assert {x.lower() for x in audience} == {agent["owner"], agent["b"], agent["c"]}, audience
        for k in ("pool", "harvest"):
            assert await record_access.linked_audience(tenant, None, None, lead_id=lead[k]) is None, k
        assert await record_access.linked_audience(tenant, None, None, lead_id=uuid.uuid4()) is None
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


async def _insert_children(admin, tenant: str, k: str, lead_id, ids: dict, media_id, token_hash: str) -> None:
    """One row per child table on lead `lead_id` (superuser, outside RLS)."""
    def i(table: str):
        return ids[table][k]

    await admin.execute("INSERT INTO property_media (id, tenant_id, lead_id, kind, url) VALUES ($1,$2,$3,'photo','/p')",
                        media_id, tenant, lead_id)
    await admin.execute(
        "INSERT INTO client_portals (id, tenant_id, lead_id, token_hash, access_expires_at) "
        "VALUES ($1,$2,$3,$4, now() + interval '1 day')", i("client_portals"), tenant, lead_id, token_hash)
    await admin.execute(
        "INSERT INTO contract_documents (id, tenant_id, lead_id, document_type, template_key, template_version, "
        "input_hash, content_ciphertext, created_by, status) VALUES ($1,$2,$3,'assignment','t','1',$4,$5,'x','draft')",
        i("contract_documents"), tenant, lead_id, "0" * 64, b"\x00")
    await admin.execute("INSERT INTO property_floorplans (id, tenant_id, lead_id, document) VALUES ($1,$2,$3,'{}')",
                        i("property_floorplans"), tenant, lead_id)
    await admin.execute(
        "INSERT INTO property_floorplan_revisions (id, tenant_id, floorplan_id, revision, document) "
        "VALUES ($1,$2,$3,1,'{}')", i("property_floorplan_revisions"), tenant, i("property_floorplans"))
    await admin.execute("INSERT INTO property_pano_scenes (id, tenant_id, lead_id, media_id) VALUES ($1,$2,$3,$4)",
                        i("property_pano_scenes"), tenant, lead_id, media_id)
    await admin.execute(
        "INSERT INTO property_view_upload_links (id, tenant_id, lead_id, token_hash, expires_at) "
        "VALUES ($1,$2,$3,$4, now() + interval '1 day')",
        i("property_view_upload_links"), tenant, lead_id, secrets.token_bytes(32))
    await admin.execute(
        "INSERT INTO reconstruction_jobs (id, tenant_id, lead_id, status, raw_output_key) "
        "VALUES ($1,$2,$3,'failed','raw/x')", i("reconstruction_jobs"), tenant, lead_id)
    await admin.execute(
        "INSERT INTO spatial_tour_variants (id, tenant_id, lead_id, property_key, source_record_ids, "
        "source_media_ids, variant_name, style, rehab_scope, model_version, disclosure, created_by) "
        "VALUES ($1,$2,$3,'k',ARRAY[gen_random_uuid()],ARRAY[$4::uuid],'v','modern','{}','m','d','x')",
        i("spatial_tour_variants"), tenant, lead_id, media_id)
    await admin.execute(
        "INSERT INTO capture_sessions (id, tenant_id, lead_id, reconstruction_job_id, status) "
        "VALUES ($1,$2,$3,$4,'succeeded')", i("capture_sessions"), tenant, lead_id, i("reconstruction_jobs"))
    await admin.execute(
        "INSERT INTO voice_walkthrough_jobs (id, tenant_id, lead_id, created_by, original_filename, suffix, "
        "audio_bytes, status) VALUES ($1,$2,$3,'x','w.m4a','.m4a',0,'succeeded')",
        i("voice_walkthrough_jobs"), tenant, lead_id)
    await admin.execute(
        "INSERT INTO video_studio_jobs (id, tenant_id, kind, source, size, status) "
        "VALUES ($1,$2,'text',$3::jsonb,'720x1280','queued')",
        i("video_studio_jobs"), tenant, json.dumps({"lead_id": str(lead_id), "images": []}))
    await admin.execute(
        "INSERT INTO live_call_sessions (id, tenant_id, lead_id, created_by) VALUES ($1,$2,$3,'x')",
        i("live_call_sessions"), tenant, lead_id)
