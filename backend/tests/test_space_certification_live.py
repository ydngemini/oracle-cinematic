"""A 3D space's certification against real PostgreSQL (T12.09, AC12.06):
FORCE RLS, the application login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The certification state is derived on every read from evidence already
stored — the build's diagnostics and the property's current capture media —
so a late upload invalidates an old proof without anything being rewritten.
This drives the one query that decides it (tour_api.fetch_tour_rows and the
Space summary) as an agent of the brokerage and as the portal's brokerage
scope:

* a build whose recorded input set covers every current photo/video is fresh;
  a row that is not capture input (no stored bytes, a generated listing
  video, or a client-link upload an agent has not approved) does not change
  that, and the build's own gather never reads it; approving the upload
  makes it capture input, and the space stale;
* capture the build could not read is counted, so it can never verify;
* a photo uploaded after the build makes it stale → pending, with the reason;
* a legacy build that never recorded its inputs is "unknown" until a photo
  newer than the build proves it stale — it is never assumed fresh;
* a space no build produced (a phone scan) is pending;
* nothing here is ever `verified`: no room-connectivity or device-run
  evidence exists, and the best stored numbers stop at `incomplete`;
* another brokerage reads nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

GOOD = {"held_out_psnr": 28.8, "held_out_ssim": 0.89, "held_out_lpips": 0.086,
        "registration_ratio": 0.95, "mean_reprojection_error_px": 0.5, "input_images": 150}


def test_freshness_and_state_are_derived_from_stored_evidence(monkeypatch, tmp_path):
    asyncio.run(_scenario(monkeypatch, tmp_path))


async def _scenario(monkeypatch, tmp_path):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import reconstruction_worker
    import tour_api
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other = uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:8]
    users = {"owner": uuid.uuid4(), "a": uuid.uuid4(), "d": uuid.uuid4()}
    agent = {k: f"{k}-{tag}@cert.test" for k in users}
    fresh_lead, legacy_lead, scan_lead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    skipped_lead = uuid.uuid4()
    p1, p2, p_blobless, p_late = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p_reel, p_pending, p_rejected, p_approved = (uuid.uuid4() for _ in range(4))
    legacy_old, legacy_late = uuid.uuid4(), uuid.uuid4()
    s_read, s_unread = uuid.uuid4(), uuid.uuid4()
    splat, legacy_splat, scan_splat = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    skipped_splat = uuid.uuid4()
    job, legacy_job, skipped_job = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=str(other if who == "d" else tenant), role=role)

    portal = brokerage_scope(TenantContext(agent_id=f"portal:{tag}", tenant_id=str(tenant), role=Role.AGENT))

    async def read(who, lead):
        c = portal if who == "portal" else ctx(who)
        async with connection.tenant_tx(c) as conn:
            rows, scenes, plan = await tour_api.fetch_tour_rows(conn, lead, None)
        return rows, tour_api.build_tour(rows, scenes, plan, lead_id=lead)

    def splat_row(rows):
        return next(r for r in rows if r["kind"] == "splat")

    try:
        # ── fixtures (superuser, outside RLS) ──────────────────────────────
        for t, slug in ((tenant, "cert"), (other, "cert-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Certification')",
                                t, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for lead in (fresh_lead, legacy_lead, scan_lead, skipped_lead):
            # No client: a harvested prospect, readable across the brokerage.
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, address) "
                "VALUES ($1,$2,$3,'DE',50,$4)", lead, tenant, f"CERT-{tag}-{lead.hex[:6]}",
                f"{tag} Certification Way, Dover, DE")

        async def media(mid, lead, kind, *, key="k", age="2 hours", provenance="captured",
                        uploaded_via="agent", review_status="approved"):
            await admin.execute(
                "INSERT INTO property_media (id, tenant_id, lead_id, kind, url, s3_key, provenance, "
                "uploaded_via, review_status, "
                f"created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, now() - interval '{age}')",
                mid, tenant, lead, kind, f"/api/media/{mid}", key, provenance,
                uploaded_via, review_status)

        async def build(jid, lead, media_id, diagnostics):
            await admin.execute(
                "INSERT INTO reconstruction_jobs (id, tenant_id, lead_id, status, stage, media_id, "
                "started_at, finished_at, diagnostics) VALUES ($1,$2,$3,'succeeded','ready',$4, "
                "now() - interval '90 minutes', now() - interval '60 minutes', $5::jsonb)",
                jid, tenant, lead, media_id, json.dumps(diagnostics))

        # A build that recorded what it was built from (p1, p2).
        await media(p1, fresh_lead, "photo")
        await media(p2, fresh_lead, "video")
        await media(splat, fresh_lead, "splat", key="splats/fresh.sog", age="1 hour")
        await build(job, fresh_lead, splat, {
            "limitations": [], "quality": GOOD,
            "capture": {"input_media_ids": sorted([str(p1), str(p2)])}})
        # A legacy build: no input set, only its start time.
        await media(legacy_old, legacy_lead, "photo", age="3 hours")
        await media(legacy_splat, legacy_lead, "splat", key="splats/legacy.sog", age="1 hour")
        await build(legacy_job, legacy_lead, legacy_splat, {"limitations": [], "quality": GOOD})
        # A phone scan: a captured splat with no build behind it.
        await media(scan_splat, scan_lead, "splat", key="splats/scan.sog", age="1 hour")
        # A build that could not read one of its two photos.
        await media(s_read, skipped_lead, "photo")
        await media(s_unread, skipped_lead, "photo")
        await media(skipped_splat, skipped_lead, "splat", key="splats/skipped.sog", age="1 hour")
        await build(skipped_job, skipped_lead, skipped_splat, {
            "limitations": [], "quality": GOOD,
            "capture": {"input_media_ids": sorted([str(s_read), str(s_unread)]),
                        "skipped_media_ids": [str(s_unread)]}})

        # ── 1. covered inputs: fresh, best stored evidence, not verified ───
        rows, tour = await read("a", fresh_lead)
        row = splat_row(rows)
        assert row["evidence_fresh"] is True
        assert row["evidence_job_status"] == "succeeded"
        assert tour["splat_quality_state"] == "incomplete"
        assert tour["badge"] == "3D Walkthrough · quality not verified"
        assert tour["whole_home_verified"] is False
        reasons = " ".join(tour["splat_certification"]["reasons"])
        assert "real phones" in reasons and "connects" in reasons

        # The portal reads under the brokerage scope and gets the same answer.
        rows, tour = await read("portal", fresh_lead)
        assert splat_row(rows)["evidence_fresh"] is True
        assert splat_row(rows)["evidence_unread"] == 0
        assert tour["splat_quality_state"] == "incomplete"

        # ── 1b. a generated listing video is not capture input ─────────────
        # video_studio stores it as kind='video' with an object key; it is
        # model output, not the house, so it neither stales the proof nor
        # reaches the next build.
        await media(p_reel, fresh_lead, "video", key="video-studio/reel.mp4", age="1 minute",
                    provenance="ai_generated")
        rows, tour = await read("a", fresh_lead)
        assert splat_row(rows)["evidence_fresh"] is True
        assert tour["splat_quality_state"] == "incomplete"

        # ── 1c. a client-link upload nobody approved is not capture input ──
        # property_view_api inserts it 'pending'; an agent may reject it. It
        # may not be this house at all (migration 0066: held for review before
        # it appears on anything public).
        await media(p_pending, fresh_lead, "photo", key="client/pending.jpg", age="1 minute",
                    uploaded_via="client_link", review_status="pending")
        await media(p_rejected, fresh_lead, "photo", key="client/rejected.jpg", age="1 minute",
                    uploaded_via="client_link", review_status="rejected")
        rows, tour = await read("a", fresh_lead)
        assert splat_row(rows)["evidence_fresh"] is True
        assert tour["splat_quality_state"] == "incomplete"
        assert not any("added after this space was built" in c for c in tour["splat_caveats"])

        async def _no_bytes(_row):
            return None

        monkeypatch.setattr(reconstruction_worker.media_storage, "load_media_bytes", _no_bytes)
        _, counts = await reconstruction_worker._gather_source_images(
            reconstruction_worker.ReconstructionJob(ctx=ctx("a"), job_id=str(uuid.uuid4()),
                                                    lead_id=str(fresh_lead), listing_id=None),
            tmp_path / "gather")
        # Neither the generated reel nor the unapproved client uploads.
        assert counts["input_media_ids"] == sorted([str(p1), str(p2)])

        # ── 2. a row with no stored bytes is not capture input ─────────────
        await media(p_blobless, fresh_lead, "photo", key=None, age="1 minute")
        rows, _ = await read("a", fresh_lead)
        assert splat_row(rows)["evidence_fresh"] is True

        # ── 3. a late upload makes the proof stale, without a write ────────
        await media(p_late, fresh_lead, "photo", age="1 minute")
        rows, tour = await read("a", fresh_lead)
        assert splat_row(rows)["evidence_fresh"] is False
        assert tour["splat_quality_state"] == "pending"
        assert any("added after this space was built" in c for c in tour["splat_caveats"])
        async with connection.tenant_tx(ctx("owner")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM property_media WHERE lead_id=$1", fresh_lead) == 8
        summary = await tour_api.space_summary(lead_id=fresh_lead, listing_id=None, ctx=ctx("a"))
        assert summary["published"]["certification"]["state"] == "pending"

        # ── 4. legacy build: unknown until a newer photo proves it stale ───
        rows, tour = await read("a", legacy_lead)
        assert splat_row(rows)["evidence_fresh"] is None
        assert tour["splat_quality_state"] == "incomplete"
        assert "evidence_age_unknown" in tour_api.space_certification.from_tour_row(splat_row(rows)).reasons
        await media(legacy_late, legacy_lead, "photo", age="1 minute")
        rows, tour = await read("a", legacy_lead)
        assert splat_row(rows)["evidence_fresh"] is False
        assert tour["splat_quality_state"] == "pending"

        # ── 5. a phone scan has no evidence at all ─────────────────────────
        rows, tour = await read("a", scan_lead)
        row = splat_row(rows)
        assert row["evidence_job_id"] is None and row["evidence_fresh"] is None
        assert tour["splat_quality_state"] == "pending"
        summary = await tour_api.space_summary(lead_id=scan_lead, listing_id=None, ctx=ctx("a"))
        assert summary["published"]["certification"]["state"] == "pending"

        # ── 5b. capture the build could not read is counted, not covered ───
        rows, tour = await read("a", skipped_lead)
        row = splat_row(rows)
        assert row["evidence_fresh"] is True and row["evidence_unread"] == 1
        assert tour["splat_quality_state"] == "incomplete"
        assert any("couldn't be read" in c for c in tour["splat_caveats"])

        # ── 5c. approving a client upload makes it capture input ───────────
        # Pending, it changes nothing; once the agent approves it (the review
        # endpoint's own write, as the app login), the space was not built
        # from it and is stale.
        await media(p_approved, skipped_lead, "photo", key="client/approved.jpg", age="1 minute",
                    uploaded_via="client_link", review_status="pending")
        rows, _ = await read("a", skipped_lead)
        assert splat_row(rows)["evidence_fresh"] is True
        import property_view_api

        await property_view_api.review_media(
            p_approved, property_view_api.ReviewMedia(decision="approved"), ctx=ctx("a"))
        rows, tour = await read("a", skipped_lead)
        assert splat_row(rows)["evidence_fresh"] is False
        assert tour["splat_quality_state"] == "pending"

        # ── 6. another brokerage reads nothing ─────────────────────────────
        rows, tour = await read("d", fresh_lead)
        assert rows == [] and tour["best_tier"] == 0

        # Never verified, whoever reads.
        for who, lead in (("a", fresh_lead), ("owner", legacy_lead), ("portal", scan_lead),
                          ("a", skipped_lead)):
            _, tour = await read(who, lead)
            assert tour["splat_quality_state"] != "verified" and tour["badge"] != "Full 3D Walkthrough"
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
                for t in (tenant, other):
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", str(t))
            for t in (tenant, other):
                await admin.execute("DELETE FROM tenants WHERE id=$1", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
