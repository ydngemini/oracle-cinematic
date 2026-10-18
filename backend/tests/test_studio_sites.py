"""Studio sites remain tenant-scoped, source-backed, and approval-bound."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import sites_api
from sites_api import (
    AttributionCreate,
    BrandTheme,
    SiteArea,
    SiteContent,
    SiteCreate,
    SitePublishFinalize,
    SourceCitation,
    create_site,
    publish_site,
)
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
MIGRATION = (
    Path(__file__).parents[1]
    / "db"
    / "migrations"
    / "0056_studio_sites_and_contract_types.sql"
)


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(received_ctx):
        assert received_ctx == CTX
        yield conn

    return tx


async def _ignore_audit(**_kwargs):
    return None


class _CreateConn:
    def __init__(self):
        self.site_id = uuid.uuid4()
        self.revision_id = uuid.uuid4()
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        now = datetime.now(timezone.utc)
        if "INSERT INTO hyperlocal_sites" in query:
            return {
                "id": self.site_id,
                "tenant_id": args[0],
                "owner_agent_id": args[1],
                "name": args[2],
                "slug": args[3],
                "template_key": args[4],
                "status": "draft",
                "created_at": now,
                "updated_at": now,
            }
        if "INSERT INTO hyperlocal_site_revisions" in query:
            return {
                "id": self.revision_id,
                "tenant_id": args[0],
                "site_id": args[1],
                "revision": 1,
                "brand_theme": args[2],
                "content": args[3],
                "authorized_idx_sources": args[4],
                "source_manifest": args[5],
                "content_sha256": args[6],
                "created_by": args[7],
                "created_at": now,
            }
        if "UPDATE hyperlocal_sites" in query:
            return {
                "id": self.site_id,
                "tenant_id": args[0],
                "owner_agent_id": CTX.agent_id,
                "name": "Dover Homes",
                "slug": "dover-homes",
                "template_key": "editorial",
                "status": "draft",
                "preview_revision_id": args[2],
                "created_at": now,
                "updated_at": now,
            }
        raise AssertionError(query)


def test_site_create_writes_tenant_and_constrained_theme(monkeypatch):
    conn = _CreateConn()
    monkeypatch.setattr(sites_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(sites_api.ledger, "record", _ignore_audit)

    result = asyncio.run(
        create_site(
            SiteCreate(name="Dover Homes", slug="dover-homes", template_key="editorial"),
            CTX,
        )
    )

    assert result["site"]["tenant_id"] == TENANT_ID
    assert result["site"]["owner_agent_id"] == CTX.agent_id
    assert result["revision"]["content_sha256"]
    assert result["revision"]["brand_theme"]["glass_opacity"] == 0.2
    for query, args in conn.calls:
        assert args[0] == TENANT_ID
        assert "$1::uuid" in query


def test_theme_rejects_arbitrary_css_and_glass_above_twenty_percent():
    with pytest.raises(ValidationError):
        BrandTheme(glass_opacity=0.21)
    with pytest.raises(ValidationError):
        BrandTheme.model_validate({**BrandTheme().model_dump(), "custom_css": "body{display:none}"})


def test_area_content_requires_source_and_rejects_protected_claim_fields():
    citation = SourceCitation(
        source_name="City open data",
        source_url="https://data.example.gov/area",
        observed_at=date(2026, 8, 1),
    )
    area = SiteArea(
        name="Downtown",
        state_code="de",
        slug="downtown",
        summary="A source-backed description of current public services and housing inventory.",
        citations=[citation],
    )
    content = SiteContent(
        headline="Dover homes with local context",
        description="Browse authorized listings and source-backed area information with a local agent.",
        areas=[area],
        seo_title="Dover homes and local real estate",
        seo_description="Authorized property listings and source-backed local information for Dover home buyers and sellers.",
    )
    assert content.areas[0].state_code == "DE"

    with pytest.raises(ValidationError):
        SiteContent(
            headline="Dover homes with local context",
            description="Browse authorized listings and source-backed area information with a local agent.",
            seo_title="Dover homes and local real estate",
            seo_description="Authorized property listings and source-backed local information for Dover home buyers and sellers.",
            **{"race": "inferred"},
        )


def test_hostname_and_attribution_validation_are_fail_closed():
    with pytest.raises(ValidationError):
        SitePublishFinalize(
            revision_id=uuid.uuid4(), approval_id=uuid.uuid4(), hostname="http://localhost/admin"
        )
    with pytest.raises(ValidationError):
        AttributionCreate(event_type="closing", subject_kind="contact", metadata={})
    with pytest.raises(HTTPException):
        AttributionCreate(
            event_type="visit",
            metadata={"familial_status": "inferred"},
        )


class _PublishConn:
    def __init__(self, approved_hash: str):
        self.site_id = uuid.uuid4()
        self.revision_id = uuid.uuid4()
        self.approved_hash = approved_hash

    async def fetchrow(self, query, *args):
        now = datetime.now(timezone.utc)
        if "FROM hyperlocal_sites s" in query:
            return {
                "id": self.site_id,
                "owner_agent_id": CTX.agent_id,
                "scope": "personal",
                "collaborator_can_edit": False,
                "collaborator_can_publish": False,
            }
        if "FROM hyperlocal_site_revisions" in query:
            return {
                "id": self.revision_id,
                "tenant_id": TENANT_ID,
                "site_id": self.site_id,
                "content_sha256": "a" * 64,
            }
        if "FROM action_approvals" in query:
            return {
                "id": args[1],
                "status": "approved",
                "expires_at": now + timedelta(hours=1),
                "payload_hash": self.approved_hash,
            }
        raise AssertionError(query)


def test_publish_rejects_payload_changed_after_approval(monkeypatch):
    conn = _PublishConn(approved_hash="0" * 64)
    monkeypatch.setattr(sites_api, "tenant_tx", _fake_tenant_tx(conn))
    body = SitePublishFinalize(
        revision_id=conn.revision_id,
        approval_id=uuid.uuid4(),
        hostname="homes.example.com",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(publish_site(conn.site_id, body, CTX))
    assert exc.value.status_code == 409
    assert exc.value.detail == "Publish payload changed after approval."


def test_migration_enforces_rls_budgets_and_all_contract_workflows():
    sql = MIGRATION.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())
    for document_type in (
        "buyer_representation",
        "buyer_offer",
        "inspection_repair_request",
        "financing_contingency_addendum",
        "listing_agreement",
        "seller_disclosure",
        "counteroffer_addendum",
        "termination_release",
    ):
        assert f"'{document_type}'" in sql
    for table in (
        "hyperlocal_sites",
        "hyperlocal_site_revisions",
        "hyperlocal_site_domains",
        "hyperlocal_site_attribution_events",
        "studio_campaigns",
    ):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    assert "spent_amount >= 0 AND spent_amount <= hard_budget" in normalized
    assert "FOREIGN KEY (tenant_id, approval_id)" in normalized
    assert "REFERENCES action_approvals(tenant_id, id)" in normalized
    assert "REVOKE DELETE, TRUNCATE" in sql


# ── T14.02 / L14-04: no new MLS/IDX on sites while the MLS gate is off ───────

_CONTENT = dict(
    headline="Dover homes with local context",
    description="Source-backed area information with a local agent for Dover buyers and sellers.",
    seo_title="Dover homes and local real estate",
    seo_description="Source-backed local information for Dover home buyers and sellers from a local agent.",
)


def _idx_source():
    import sites_api
    return sites_api.AuthorizedIdxSource(provider="Bright MLS", feed_id="feed-1", authorization_ref="contract-123")


def test_a_new_site_or_revision_cannot_add_idx_sources(monkeypatch):
    import pydantic
    import sites_api

    monkeypatch.delenv("ORACLE_MLS_ENABLED", raising=False)
    with pytest.raises(pydantic.ValidationError, match="not offered"):
        sites_api.SiteCreate(name="Main St Homes", slug="main-st-homes", authorized_idx_sources=[_idx_source()])
    with pytest.raises(pydantic.ValidationError, match="not offered"):
        sites_api.SiteRevisionCreate(brand_theme=sites_api.BrandTheme(), content=sites_api.SiteContent(**_CONTENT),
                                     authorized_idx_sources=[_idx_source()])
    with pytest.raises(pydantic.ValidationError, match="not offered"):
        sites_api.SiteContent(**_CONTENT, idx_requested=True)
    # Without IDX, nothing changes.
    assert sites_api.SiteCreate(name="Main St Homes", slug="main-st-homes").authorized_idx_sources == []
    assert sites_api.SiteContent(**_CONTENT).idx_requested is False


def test_the_legacy_gate_still_decides_when_mls_is_switched_on(monkeypatch):
    import sites_api

    monkeypatch.setenv("ORACLE_MLS_ENABLED", "1")
    assert sites_api.SiteContent(**_CONTENT, idx_requested=True).idx_requested is True
    assert len(sites_api.SiteRevisionCreate(brand_theme=sites_api.BrandTheme(), content=sites_api.SiteContent(**_CONTENT),
                                            authorized_idx_sources=[_idx_source()]).authorized_idx_sources) == 1


# ── T14.04: a hostname belongs to one claim ──────────────────────────────────

class _TakenHostnameConn(_PublishConn):
    """Everything about the publish is in order, but another site (of this or
    another brokerage) already claims the hostname: the global UNIQUE (hostname)."""

    def __init__(self):
        super().__init__(approved_hash="")

    async def execute(self, query, *args):
        return "SELECT 1"

    async def fetchval(self, query, *args):
        return "northstar"

    async def fetchrow(self, query, *args):
        if "UPDATE hyperlocal_sites" in query:
            return {"id": self.site_id, "slug": "dover-homes", "status": "published"}
        if "INSERT INTO hyperlocal_site_domains" in query:
            import asyncpg

            exc = asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
            exc.constraint_name = "hyperlocal_site_domains_hostname_key"
            raise exc
        return await super().fetchrow(query, *args)


def test_a_hostname_claimed_elsewhere_is_409_and_never_says_by_whom(monkeypatch):
    from automation_jobs import payload_hash

    conn = _TakenHostnameConn()
    monkeypatch.setattr(sites_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(sites_api.ledger, "record", _ignore_audit)
    body = SitePublishFinalize(revision_id=conn.revision_id, approval_id=uuid.uuid4(), hostname="homes.example.com")
    conn.approved_hash = payload_hash(
        sites_api._publish_payload(conn.site_id, {"id": conn.revision_id, "content_sha256": "a" * 64},
                                   "homes.example.com"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(publish_site(conn.site_id, body, CTX))
    assert exc.value.status_code == 409
    assert exc.value.detail == "This hostname is not available."


class _RepublishConn(_TakenHostnameConn):
    """A (re)publish whose hostname claim already has a state of its own: the
    upsert leaves that state alone (0173), so the claim may be anything."""

    def __init__(self, claim_status):
        super().__init__()
        self.claim_status = claim_status

    async def fetchrow(self, query, *args):
        if "INSERT INTO hyperlocal_site_domains" in query:
            return {"id": uuid.uuid4(), "tenant_id": TENANT_ID, "site_id": self.site_id, "hostname": args[2],
                    "status": self.claim_status, "challenge_token": "ab" * 32}
        return await super().fetchrow(query, *args)


@pytest.mark.parametrize("hostname, claim_status", [
    ("homes.example.com", "pending"), ("homes.example.com", "verified"), ("homes.example.com", "active"),
    ("homes.example.com", "failed"), (None, None),
])
def test_the_publish_audit_entry_names_the_claims_real_state(monkeypatch, hostname, claim_status):
    from automation_jobs import payload_hash

    conn = _RepublishConn(claim_status)
    audited: list[dict] = []

    async def record(**kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(sites_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(sites_api.ledger, "record", record)
    body = SitePublishFinalize(revision_id=conn.revision_id, approval_id=uuid.uuid4(), hostname=hostname)
    conn.approved_hash = payload_hash(
        sites_api._publish_payload(conn.site_id, {"id": conn.revision_id, "content_sha256": "a" * 64}, hostname))
    result = asyncio.run(publish_site(conn.site_id, body, CTX))
    (entry,) = [a for a in audited if a["action"] == "studio_site_published"]
    assert entry["metadata"]["custom_domain_status"] == claim_status
    assert "custom_domain_pending" not in entry["metadata"]       # it said pending for a verified claim
    assert "homes.example.com" not in repr(entry)
    assert result["custom_domain_status"] == (
        "not_requested" if hostname is None else "pending_verification" if claim_status == "pending"
        else claim_status)
