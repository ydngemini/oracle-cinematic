"""The title-finding reviewer action against real PostgreSQL (T11.03, F11.04):
FORCE RLS, the application login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

A broker owner confirms one finding and dismisses another in their tenant;
another tenant's finding is a 404 and stays untouched; a second decision is a
409. Then the homeowner's portal read: the dismissed finding is gone and the
confirmed one carries review_status 'confirmed', the one condition under which
SecureDossierPage drops its "Not verified" label. The audit rows name the
finding, the decision and the actor, and never the reason.

The reason is at rest only sealed, under the finding's own tenant key (0167,
CONTRACT C5.1). A platform admin may decide another tenant's finding (RLS lets
them); that decision is sealed with, and audited in, the finding's tenant.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

REASON = "Recorded lien matches this parcel id and the deed of record"
DISMISS_REASON = "Judgment names a different owner at another address"
MASTER = "live-test-title-review-master-key-0123456789abcdef"


def test_an_owner_decides_their_tenants_title_findings_only(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg
    from fastapi import HTTPException

    import client_portal
    import intelligence_api
    import sealed_text
    from db import connection
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other = uuid.uuid4(), uuid.uuid4()
    tag = str(tenant)[:8]
    parcel = f"TITLE-{tag}"
    owner = TenantContext(agent_id=f"owner-{tag}@title.test", tenant_id=str(tenant), role=Role.BROKER_OWNER)
    # An operator whose context tenant is `tenant`, deciding `other`'s finding.
    operator = TenantContext(agent_id=f"ops-{tag}@title.test", tenant_id=str(tenant), role=Role.PLATFORM_ADMIN)
    lead, portal = uuid.uuid4(), uuid.uuid4()

    async def opened(finding_id, tid):
        """The ciphertext opened with tenant tid's key."""
        return await admin.fetchval(
            "SELECT pgp_sym_decrypt(review_reason_ciphertext, $2) FROM title_findings WHERE id=$1",
            finding_id, sealed_text.key_for(tid))

    async def finding(tid, match_status, kind):
        license_id = await admin.fetchval(
            "INSERT INTO source_licenses (tenant_id, source_key, source_name) VALUES ($1,$2,'County recorder') "
            "ON CONFLICT (tenant_id, source_key) DO UPDATE SET source_name=EXCLUDED.source_name RETURNING id",
            tid, f"recorder-{tag}")
        record_id = await admin.fetchval(
            "INSERT INTO source_records (tenant_id, source_license_id, source_key, record_key, property_key, "
            "observed_at, request_hash, payload_hash, raw_payload) "
            "VALUES ($1,$2,$3,$4,$5, now(), $6, $6, '{}'::jsonb) RETURNING id",
            tid, license_id, f"recorder-{tag}", f"R-{uuid.uuid4().hex[:8]}", parcel, "a" * 64)
        return await admin.fetchval(
            "INSERT INTO title_findings (tenant_id, property_key, finding_type, match_status, source_record_id, notes) "
            "VALUES ($1,$2,$3,$4,$5,'Agent working note about a named third party') RETURNING id",
            tid, parcel, kind, match_status, record_id)

    def review(decision, reason):
        return intelligence_api.TitleFindingReview(decision=decision, reason=reason)

    try:
        for tid, name in ((tenant, "Title A"), (other, "Title B")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                tid, f"title-{str(tid)[:8]}", name)
        confirmed_id = await finding(tenant, "matched", "lien")
        dismissed_id = await finding(tenant, "possible_match", "judgment")
        foreign_id = await finding(other, "matched", "lien")

        # The application login is held by RLS: it is no superuser and cannot bypass it.
        async with connection.tenant_tx(owner) as conn:
            bypass = await conn.fetchval(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert bypass is False

        out = await intelligence_api.review_title_finding(
            finding_id=confirmed_id, body=review("confirmed", REASON), ctx=owner)
        assert (out["review_status"], out["reviewed_by"]) == ("confirmed", owner.agent_id)
        row = await admin.fetchrow(
            "SELECT review_status, reviewed_by, reviewed_at FROM title_findings WHERE id=$1",
            confirmed_id)
        assert (row["review_status"], row["reviewed_by"]) == ("confirmed", owner.agent_id)
        assert row["reviewed_at"] is not None
        # The reason is at rest only sealed, under this tenant's key.
        assert await opened(confirmed_id, tenant) == REASON
        # There is no plaintext column for it at all (0167).
        assert not await admin.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='title_findings' AND column_name='review_reason')")

        # Another tenant's finding does not exist for this caller, and is untouched.
        with pytest.raises(HTTPException) as unseen:
            await intelligence_api.review_title_finding(
                finding_id=foreign_id, body=review("dismissed", DISMISS_REASON), ctx=owner)
        assert unseen.value.status_code == 404
        foreign = await admin.fetchrow(
            "SELECT review_status, reviewed_by, reviewed_at FROM title_findings WHERE id=$1", foreign_id)
        assert tuple(foreign) == ("required", None, None)

        # A decided finding is not decided again.
        with pytest.raises(HTTPException) as again:
            await intelligence_api.review_title_finding(
                finding_id=confirmed_id, body=review("dismissed", DISMISS_REASON), ctx=owner)
        assert again.value.status_code == 409 and "confirmed" in again.value.detail
        assert await admin.fetchval(
            "SELECT review_status FROM title_findings WHERE id=$1", confirmed_id) == "confirmed"

        await intelligence_api.review_title_finding(
            finding_id=dismissed_id, body=review("dismissed", DISMISS_REASON), ctx=owner)

        # The homeowner's portal read, through the real route and query.
        await admin.execute(
            "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, address) "
            "VALUES ($1,$2,$3,'DE',50,'1 Title Way')", lead, tenant, parcel)
        await admin.execute(
            "INSERT INTO client_portals (id, tenant_id, lead_id, token_hash, access_expires_at, asset_scope) "
            "VALUES ($1,$2,$3,$4, now() + interval '1 day', $5::jsonb)",
            portal, tenant, lead, uuid.uuid4().hex, json.dumps({"summary": True, "title_summary": True}))

        async def no_activity(*_a, **_k):
            return None

        monkeypatch.setattr(client_portal, "_record_portal_activity", no_activity)
        monkeypatch.setattr(client_portal, "_portal_session_claims", lambda _h: {
            "portal_id": str(portal), "tenant_id": str(tenant), "lead_id": str(lead), "link_kind": "seller",
        })
        dossier = await client_portal.read_scoped_dossier(authorization="Bearer x")
        summary = dossier["assets"]["title_summary"]
        assert [(f["finding_type"], f["match_status"], f["review_status"]) for f in summary] == [
            ("lien", "matched", "confirmed"),
        ]

        # The audit rows name the finding, the decision and the actor; never the reason.
        audits = await admin.fetch(
            "SELECT action, user_id, target_id, metadata FROM audit_ledger "
            "WHERE tenant_id=$1 AND action LIKE 'title_finding_%' ORDER BY created_at", tenant)
        assert [(a["action"], a["user_id"], str(a["target_id"])) for a in audits] == [
            ("title_finding_confirmed", owner.agent_id, str(confirmed_id)),
            ("title_finding_dismissed", owner.agent_id, str(dismissed_id)),
        ]
        for audit in audits:
            metadata = json.loads(audit["metadata"]) if isinstance(audit["metadata"], str) else audit["metadata"]
            assert set(metadata) == {"finding_id", "decision"}
            assert REASON not in json.dumps(metadata) and DISMISS_REASON not in json.dumps(metadata)

        # A platform admin's decision on `other`'s finding is sealed with, and
        # audited in, `other`: the tenant whose client no longer sees it.
        out = await intelligence_api.review_title_finding(
            finding_id=foreign_id, body=review("dismissed", DISMISS_REASON), ctx=operator)
        assert (out["review_status"], out["reviewed_by"]) == ("dismissed", operator.agent_id)
        assert await opened(foreign_id, other) == DISMISS_REASON
        foreign_audits = await admin.fetch(
            "SELECT action, user_id, target_id FROM audit_ledger "
            "WHERE tenant_id=$1 AND action LIKE 'title_finding_%'", other)
        assert [(a["action"], a["user_id"], str(a["target_id"])) for a in foreign_audits] == [
            ("title_finding_dismissed", operator.agent_id, str(foreign_id)),
        ]
        assert await admin.fetchval(
            "SELECT count(*) FROM audit_ledger WHERE tenant_id=$1 AND action LIKE 'title_finding_%'",
            tenant) == 2
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for tid in (tenant, other):
                for table in ("client_portals", "leads", "title_findings", "source_records", "source_licenses"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tid)
                await admin.execute("DELETE FROM tenants WHERE id = $1", tid)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
