"""Tenant-scoped Neoh Studio sites, previews, publishing, and attribution."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import site_domains
from approval_service import create_approval
from audit_ledger import AuditCategory, ledger
from automation_jobs import canonical_json, payload_hash
from db.connection import tenant_tx
from public_sites_api import public_site_path
from platform_policy import ActionRisk, enforce_public_property_data
from site_domains import DnsResolver, DnsUnavailable, get_dns_resolver
from tenancy import Role, TenantContext, require_context, require_role

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sites", tags=["studio-sites"])

SiteTemplate = Literal["editorial", "neighborhood", "listing_focus"]
SiteStatus = Literal["draft", "preview", "published", "archived"]
SiteScope = Literal["personal", "team"]
AttributionEvent = Literal[
    "visit", "lead_capture", "intake_complete", "appointment", "contract", "closing"
]

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_SAFE_FONT_PAIRS = {"editorial_sans", "narrow_editorial", "neutral_sans"}


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, date):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif key in {
            "brand_theme",
            "content",
            "authorized_idx_sources",
            "source_manifest",
            "metadata",
            "last_check",
        }:
            result[key] = _json(value)
    return result


def _safe_asset_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    # Browsers read "/\\host" as protocol-relative, the same as "//host" (INFO-2).
    if cleaned.startswith("/") and not cleaned.startswith(("//", "/\\")) and "\\" not in cleaned:
        return cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("media URLs must be local paths or absolute HTTPS URLs")
    return cleaned


def _normalized_hostname(value: str) -> str:
    candidate = value.strip().rstrip(".").lower()
    if "://" in candidate or "/" in candidate or "@" in candidate:
        raise ValueError("hostname must not contain a scheme, path, or user information")
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("hostname is not valid IDNA") from exc
    if len(candidate) > 253 or "." not in candidate:
        raise ValueError("hostname must be a fully qualified domain name")
    labels = candidate.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise ValueError("hostname contains an invalid label")
    if candidate == "localhost" or candidate.endswith(".localhost"):
        raise ValueError("local hostnames cannot be published")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return candidate
    raise ValueError("IP addresses cannot be used as tenant domains")


def _platform_hostnames() -> set[str]:
    """The platform's own addresses: the configured public origins and the
    custom-domain serving target. A brokerage never claims one of these."""
    hosts: set[str] = set()
    for name in ("ORACLE_DOMAIN", "ORACLE_PUBLIC_BASE_URL", "ORACLE_BASE_URL"):
        value = (os.getenv(name) or "").strip()
        if not value:
            continue
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.hostname:
            hosts.add(parsed.hostname.rstrip(".").lower())
    target = site_domains.serving_target()
    if target:
        hosts.add(target)
    return hosts


def _claimable_hostname(value: str) -> str:
    """A hostname a publish may claim (T14.04): a valid public FQDN whose DNS
    challenge name still fits in DNS, and never the platform's own address or
    a name under it."""
    candidate = _normalized_hostname(value)
    if len(site_domains.challenge_name(candidate)) > 253:
        raise ValueError("hostname is too long to verify with a DNS record")
    for platform in _platform_hostnames():
        if candidate == platform or candidate.endswith("." + platform):
            raise ValueError("the platform's own hostnames cannot be claimed by a site")
    return candidate


class BrandTheme(BaseModel):
    """Constrained theme tokens; arbitrary tenant CSS is deliberately excluded."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    background: str = Field(default="#171612", pattern=_HEX_COLOR_RE.pattern)
    surface: str = Field(default="#2B2922", pattern=_HEX_COLOR_RE.pattern)
    text: str = Field(default="#F2F2F2", pattern=_HEX_COLOR_RE.pattern)
    muted: str = Field(default="#ABABAB", pattern=_HEX_COLOR_RE.pattern)
    accent: str = Field(default="#FFBC1F", pattern=_HEX_COLOR_RE.pattern)
    border: str = Field(default="#8A7550", pattern=_HEX_COLOR_RE.pattern)
    glass_opacity: float = Field(default=0.2, ge=0, le=0.2)
    font_pair: str = Field(default="narrow_editorial")

    @field_validator("font_pair")
    @classmethod
    def validate_font_pair(cls, value: str) -> str:
        if value not in _SAFE_FONT_PAIRS:
            raise ValueError("font_pair must be one of the approved Studio font pairs")
        return value


class SourceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_name: str = Field(min_length=2, max_length=160)
    source_url: str = Field(min_length=8, max_length=2048)
    observed_at: date
    license: str = Field(default="public-or-licensed", min_length=2, max_length=120)

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source_url must be an absolute HTTPS URL")
        return value


class SiteArea(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    state_code: str = Field(pattern=r"^[A-Za-z]{2}$")
    slug: str = Field(pattern=_SLUG_RE.pattern)
    summary: str = Field(min_length=20, max_length=1200)
    citations: list[SourceCitation] = Field(min_length=1, max_length=12)

    @field_validator("state_code")
    @classmethod
    def normalize_state(cls, value: str) -> str:
        return value.upper()


class AuthorizedIdxSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(min_length=2, max_length=120)
    feed_id: str = Field(min_length=2, max_length=160)
    authorization_ref: str = Field(min_length=4, max_length=240)
    last_synced_at: Optional[datetime] = None
    listing_count: Optional[int] = Field(default=None, ge=0)


class SiteContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    eyebrow: str = Field(default="LOCAL REAL ESTATE", min_length=2, max_length=80)
    headline: str = Field(min_length=4, max_length=160)
    description: str = Field(min_length=20, max_length=1200)
    hero_media_url: Optional[str] = Field(default=None, max_length=2048)
    public_brand_name: Optional[str] = Field(default=None, max_length=160)
    agent_name: Optional[str] = Field(default=None, max_length=160)
    license_number: Optional[str] = Field(default=None, max_length=120)
    agent_bio: Optional[str] = Field(default=None, max_length=2000)
    requested_service_areas: list[str] = Field(default_factory=list, max_length=40)
    idx_requested: bool = False
    requested_domain: Optional[str] = Field(default=None, max_length=253)
    areas: list[SiteArea] = Field(default_factory=list, max_length=40)
    seo_title: str = Field(min_length=4, max_length=70)
    seo_description: str = Field(min_length=20, max_length=170)
    website_chat_intake: Literal["buyer_three_question", "seller_three_question"] = (
        "buyer_three_question"
    )

    @field_validator("hero_media_url")
    @classmethod
    def validate_media(cls, value: Optional[str]) -> Optional[str]:
        return _safe_asset_url(value)

    @field_validator("requested_service_areas")
    @classmethod
    def validate_requested_areas(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(" ".join(value.split()) for value in values if value.strip()))
        if any(len(value) > 120 for value in cleaned):
            raise ValueError("requested service areas must be 120 characters or fewer")
        return cleaned

    @field_validator("requested_domain")
    @classmethod
    def validate_requested_domain(cls, value: Optional[str]) -> Optional[str]:
        return _normalized_hostname(value) if value else None

    @model_validator(mode="after")
    def validate_public_content(self) -> "SiteContent":
        enforce_public_property_data(self.model_dump(mode="json"))
        if self.idx_requested:
            _refuse_idx()
        return self


IDX_NOT_OFFERED = "MLS/IDX listings are not offered on sites (D-2026-10-12-01)."


def _refuse_idx() -> None:
    """New site content may not ask for MLS/IDX while the legacy MLS gate is
    off (T14.02, L14-04). Stored revisions keep whatever they hold."""
    from mls_switch import mls_enabled

    if not mls_enabled():
        raise ValueError(IDX_NOT_OFFERED)


class _NoNewIdxSources(BaseModel):
    @model_validator(mode="after")
    def refuse_idx_sources(self):
        if getattr(self, "authorized_idx_sources", None):
            _refuse_idx()
        return self


class SiteCreate(_NoNewIdxSources):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(pattern=_SLUG_RE.pattern)
    template_key: SiteTemplate = "editorial"
    scope: SiteScope = "personal"
    brand_theme: BrandTheme = Field(default_factory=BrandTheme)
    headline: Optional[str] = Field(default=None, min_length=4, max_length=160)
    content: Optional[SiteContent] = None
    authorized_idx_sources: list[AuthorizedIdxSource] = Field(default_factory=list, max_length=20)


class SiteRevisionCreate(_NoNewIdxSources):
    model_config = ConfigDict(extra="forbid")
    brand_theme: BrandTheme
    content: SiteContent
    authorized_idx_sources: list[AuthorizedIdxSource] = Field(default_factory=list, max_length=20)


class SitePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: uuid.UUID


class SitePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision_id: uuid.UUID
    hostname: Optional[str] = Field(default=None, max_length=253)

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, value: Optional[str]) -> Optional[str]:
        return _claimable_hostname(value) if value else None


class SitePublishFinalize(SitePublishRequest):
    approval_id: uuid.UUID


class SiteRollbackRequest(BaseModel):
    """Re-point the live revision to one that was live before (T14.04).

    ``expected_live_revision_id`` is the live revision the caller saw: a
    rollback against a page someone else has since changed is refused rather
    than undoing their change. Without ``to_revision_id`` the target is the
    revision that was live just before the current one."""

    model_config = ConfigDict(extra="forbid")
    expected_live_revision_id: uuid.UUID
    to_revision_id: Optional[uuid.UUID] = None


class SiteCollaboratorUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: str = Field(min_length=1, max_length=128)
    can_edit: bool = True
    can_publish: bool = False

    @model_validator(mode="after")
    def validate_capability(self) -> "SiteCollaboratorUpsert":
        if not (self.can_edit or self.can_publish):
            raise ValueError("A collaborator must be allowed to edit or publish.")
        return self


class AttributionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_type: AttributionEvent
    subject_kind: Literal["session", "contact", "client"] = "session"
    subject_id: Optional[str] = Field(default=None, max_length=240)
    session_hash: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: Optional[str] = Field(default=None, max_length=160)
    medium: Optional[str] = Field(default=None, max_length=160)
    campaign: Optional[str] = Field(default=None, max_length=160)
    content: Optional[str] = Field(default=None, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_subject_and_metadata(self) -> "AttributionCreate":
        if self.subject_kind != "session" and not self.subject_id:
            raise ValueError("subject_id is required for contact and client attribution")
        enforce_public_property_data(self.metadata)
        if len(canonical_json(self.metadata).encode("utf-8")) > 32_000:
            raise ValueError("metadata is too large")
        return self


def _default_content(name: str, headline: Optional[str]) -> SiteContent:
    title = headline or f"{name} real estate, handled personally."
    return SiteContent(
        headline=title,
        description=(
            "A focused local home-search and seller resource. Add authorized IDX coverage "
            "and source-backed area guides before publishing."
        ),
        seo_title=title[:70],
        seo_description=(
            "Local property guidance, verified listing access, and a direct path to your agent."
        ),
    )


def _revision_payload(
    brand_theme: BrandTheme,
    content: SiteContent,
    authorized_idx_sources: list[AuthorizedIdxSource],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    brand = brand_theme.model_dump(mode="json")
    content_value = content.model_dump(mode="json")
    idx_sources = [item.model_dump(mode="json") for item in authorized_idx_sources]
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for area in content.areas:
        for citation in area.citations:
            item = citation.model_dump(mode="json")
            key = (item["source_name"], item["source_url"], item["observed_at"])
            if key not in seen:
                citations.append(item)
                seen.add(key)
    digest = payload_hash(
        {
            "brand_theme": brand,
            "content": content_value,
            "authorized_idx_sources": idx_sources,
            "source_manifest": citations,
        }
    )
    return brand, content_value, idx_sources, citations, digest


async def _fetch_site_revision(
    conn: Any,
    ctx: TenantContext,
    site_id: uuid.UUID,
    revision_id: uuid.UUID,
    capability: Literal["read", "edit", "publish"] = "read",
) -> Any:
    await _site_for_access(conn, ctx, site_id, capability=capability)
    row = await conn.fetchrow(
        """
        SELECT r.*
          FROM hyperlocal_site_revisions r
          JOIN hyperlocal_sites s
            ON s.tenant_id=r.tenant_id AND s.id=r.site_id
         WHERE r.tenant_id=$1::uuid AND r.site_id=$2::uuid AND r.id=$3::uuid
        """,
        ctx.tenant_id,
        site_id,
        revision_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Site revision not found.")
    return row


async def _site_for_access(
    conn: Any,
    ctx: TenantContext,
    site_id: uuid.UUID,
    *,
    capability: Literal["read", "edit", "publish"] = "read",
    for_update: bool = False,
) -> Any:
    row = await conn.fetchrow(
        """
        SELECT s.*,COALESCE(c.can_edit,false) AS collaborator_can_edit,
               COALESCE(c.can_publish,false) AS collaborator_can_publish
          FROM hyperlocal_sites s
          LEFT JOIN hyperlocal_site_collaborators c
            ON c.tenant_id=s.tenant_id AND c.site_id=s.id AND c.agent_id=$3
         WHERE s.tenant_id=$1::uuid AND s.id=$2::uuid
        """ + (" FOR UPDATE OF s" if for_update else ""),
        ctx.tenant_id,
        site_id,
        ctx.agent_id,
    )
    if row is None or not _allowed(row, ctx, capability):
        raise HTTPException(status_code=404, detail="Site not found.")
    return row


def _allowed(row: Any, ctx: TenantContext, capability: Literal["read", "edit", "publish"]) -> bool:
    privileged = ctx.is_platform_admin or ctx.is_broker_owner
    owner = row["owner_agent_id"] == ctx.agent_id
    if capability == "read":
        return bool(privileged or owner or row["scope"] == "team" or row["collaborator_can_edit"]
                    or row["collaborator_can_publish"])
    if capability == "edit":
        return bool(privileged or owner or row["collaborator_can_edit"])
    return bool(privileged or owner or row["collaborator_can_publish"])


@router.get("/templates")
async def site_templates(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    del ctx
    return {
        "templates": [
            {"key": "editorial", "label": "Editorial", "best_for": "agent brand and areas"},
            {"key": "neighborhood", "label": "Neighborhood", "best_for": "source-backed local guides"},
            {"key": "listing_focus", "label": "Listing focus", "best_for": "authorized IDX inventory"},
        ],
        "wizard": ["template", "brand", "hero", "areas_idx", "trust", "domain_seo", "preview"],
        "publishing": {
            "preview_is_reversible": True,
            "production_requires_approval": True,
            "arbitrary_css_allowed": False,
            "fabricated_local_claims_allowed": False,
        },
    }


@router.get("")
async def list_sites(
    limit: int = Query(default=50, ge=1, le=100),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT s.*, r.revision, r.content_sha256, r.brand_theme, r.content,
                   r.authorized_idx_sources, r.source_manifest
              FROM hyperlocal_sites s
              LEFT JOIN hyperlocal_site_revisions r
                ON r.tenant_id=s.tenant_id
               AND r.site_id=s.id
               AND r.id=COALESCE(s.preview_revision_id,s.published_revision_id)
             WHERE s.tenant_id=$1::uuid AND s.status <> 'archived'
               AND ($3::boolean OR s.owner_agent_id=$4 OR s.scope='team' OR EXISTS (
                    SELECT 1 FROM hyperlocal_site_collaborators access
                     WHERE access.tenant_id=s.tenant_id AND access.site_id=s.id
                       AND access.agent_id=$4
               ))
             ORDER BY s.updated_at DESC
             LIMIT $2
            """,
            ctx.tenant_id,
            limit,
            ctx.is_platform_admin or ctx.is_broker_owner,
            ctx.agent_id,
        )
    return {
        "sites": [_row(row) for row in rows],
        "freshness": {"retrieved_at": datetime.now(timezone.utc).isoformat()},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_site(
    body: SiteCreate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    if body.scope == "team":
        require_role(ctx, Role.BROKER_OWNER)
    content = body.content or _default_content(body.name, body.headline)
    brand, content_value, idx_sources, citations, digest = _revision_payload(
        body.brand_theme, content, body.authorized_idx_sources
    )
    async with tenant_tx(ctx) as conn:
        site = await conn.fetchrow(
            """
            INSERT INTO hyperlocal_sites (
                tenant_id,owner_agent_id,name,slug,template_key,scope
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6)
            ON CONFLICT (tenant_id,slug) DO NOTHING
            RETURNING *
            """,
            ctx.tenant_id,
            ctx.agent_id,
            body.name,
            body.slug,
            body.template_key,
            body.scope,
        )
        if site is None:
            raise HTTPException(status_code=409, detail="A site already uses this slug.")
        revision = await conn.fetchrow(
            """
            INSERT INTO hyperlocal_site_revisions (
                tenant_id,site_id,revision,brand_theme,content,
                authorized_idx_sources,source_manifest,content_sha256,created_by
            ) VALUES ($1::uuid,$2::uuid,1,$3::jsonb,$4::jsonb,$5::jsonb,$6::jsonb,$7,$8)
            RETURNING *
            """,
            ctx.tenant_id,
            site["id"],
            canonical_json(brand),
            canonical_json(content_value),
            canonical_json(idx_sources),
            canonical_json(citations),
            digest,
            ctx.agent_id,
        )
        site = await conn.fetchrow(
            """
            UPDATE hyperlocal_sites
               SET preview_revision_id=$3::uuid,status='draft'
             WHERE tenant_id=$1::uuid AND id=$2::uuid
            RETURNING *
            """,
            ctx.tenant_id,
            site["id"],
            revision["id"],
        )
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="studio_site_created",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(site["id"]),
        metadata={"slug": body.slug, "template_key": body.template_key, "content_sha256": digest},
    )
    return {"site": _row(site), "revision": _row(revision)}


@router.get("/{site_id}")
async def get_site(
    site_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        site = await _site_for_access(conn, ctx, site_id)
        revisions = await conn.fetch(
            """
            SELECT * FROM hyperlocal_site_revisions
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid
             ORDER BY revision DESC LIMIT 25
            """,
            ctx.tenant_id,
            site_id,
        )
        can_publish = _allowed(site, ctx, "publish")
        live = site["published_revision_id"] if site["status"] == "published" else None
        rollback_to = await _previous_live_revision(conn, ctx, site_id, live) if live and can_publish else None
    return {
        "site": _row(site),
        "revisions": [_row(row) for row in revisions],
        # What the panel offers; the routes still decide (404 to anyone else).
        "can_publish": can_publish,
        "publication": {
            "status": site["status"],
            "live_revision_id": str(live) if live else None,
            "rollback_to_revision_id": str(rollback_to) if rollback_to else None,
        },
    }


async def _previous_live_revision(conn: Any, ctx: TenantContext, site_id: uuid.UUID, live: Any) -> Any:
    """The revision that was live just before ``live`` went live, from the
    site's own trail (0173): the latest move onto ``live`` names where it came
    from. None when the trail holds no earlier live revision (a first publish,
    or a site live since before 0173, whose backfill row states only its
    state at migration time)."""
    return await conn.fetchval(
        """
        SELECT e.from_revision_id
          FROM hyperlocal_site_events e
         WHERE e.tenant_id=$1::uuid AND e.site_id=$2::uuid AND e.subject='site'
           AND e.to_status='published' AND e.to_revision_id=$3::uuid
           AND e.from_revision_id IS NOT NULL AND e.from_revision_id <> $3::uuid
           AND EXISTS (
               SELECT 1 FROM hyperlocal_site_events w
                WHERE w.tenant_id=e.tenant_id AND w.site_id=e.site_id AND w.subject='site'
                  AND w.to_status='published' AND w.to_revision_id=e.from_revision_id
           )
         ORDER BY e.occurred_at DESC, e.id DESC
         LIMIT 1
        """,
        ctx.tenant_id,
        site_id,
        live,
    )


async def _was_live(conn: Any, ctx: TenantContext, site_id: uuid.UUID, revision_id: uuid.UUID) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM hyperlocal_site_events
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid AND subject='site'
               AND to_status='published' AND to_revision_id=$3::uuid
        )
        """,
        ctx.tenant_id,
        site_id,
        revision_id,
    ))


@router.post("/{site_id}/revisions", status_code=status.HTTP_201_CREATED)
async def create_site_revision(
    site_id: uuid.UUID,
    body: SiteRevisionCreate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    brand, content, idx_sources, citations, digest = _revision_payload(
        body.brand_theme, body.content, body.authorized_idx_sources
    )
    async with tenant_tx(ctx) as conn:
        site = await _site_for_access(conn, ctx, site_id, capability="edit", for_update=True)
        if site["status"] == "archived":
            raise HTTPException(status_code=404, detail="Site not found.")
        next_revision = await conn.fetchval(
            """
            SELECT COALESCE(MAX(revision),0)+1
              FROM hyperlocal_site_revisions
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid
            """,
            ctx.tenant_id,
            site_id,
        )
        revision = await conn.fetchrow(
            """
            INSERT INTO hyperlocal_site_revisions (
                tenant_id,site_id,revision,brand_theme,content,
                authorized_idx_sources,source_manifest,content_sha256,created_by
            ) VALUES ($1::uuid,$2::uuid,$3,$4::jsonb,$5::jsonb,$6::jsonb,$7::jsonb,$8,$9)
            RETURNING *
            """,
            ctx.tenant_id,
            site_id,
            next_revision,
            canonical_json(brand),
            canonical_json(content),
            canonical_json(idx_sources),
            canonical_json(citations),
            digest,
            ctx.agent_id,
        )
        # A new revision is a draft beside the live page, never instead of it:
        # an edit (which an edit-only collaborator may make) must not take a
        # published site offline. Only publish and rollback move what is live.
        await conn.execute(
            """
            UPDATE hyperlocal_sites
               SET preview_revision_id=$3::uuid,
                   status=CASE WHEN status='published' THEN 'published' ELSE 'draft' END
             WHERE tenant_id=$1::uuid AND id=$2::uuid
            """,
            ctx.tenant_id,
            site_id,
            revision["id"],
        )
    return {"revision": _row(revision), "source_count": len(citations)}


@router.post("/{site_id}/preview")
async def preview_site(
    site_id: uuid.UUID,
    body: SitePreviewRequest,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        revision = await _fetch_site_revision(conn, ctx, site_id, body.revision_id, "edit")
        site = await conn.fetchrow(
            """
            UPDATE hyperlocal_sites
               SET preview_revision_id=$3::uuid,
                   status=CASE WHEN status='published' THEN 'published' ELSE 'preview' END
             WHERE tenant_id=$1::uuid AND id=$2::uuid AND status <> 'archived'
            RETURNING *
            """,
            ctx.tenant_id,
            site_id,
            body.revision_id,
        )
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return {
        "site": _row(site),
        "revision": _row(revision),
        "preview_path": f"/site-preview/{site['slug']}?revision={body.revision_id}",
        "reversible": True,
    }


def _publish_payload(
    site_id: uuid.UUID,
    revision: Any,
    hostname: Optional[str],
) -> dict[str, Any]:
    return {
        "site_id": str(site_id),
        "revision_id": str(revision["id"]),
        "content_sha256": str(revision["content_sha256"]),
        "hostname": hostname,
    }


@router.post("/{site_id}/publish-approval", status_code=status.HTTP_202_ACCEPTED)
async def request_site_publish(
    site_id: uuid.UUID,
    body: SitePublishRequest,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        revision = await _fetch_site_revision(conn, ctx, site_id, body.revision_id, "publish")
    payload = _publish_payload(site_id, revision, body.hostname)
    approval = await create_approval(
        ctx,
        action_type="studio.site.publish",
        risk=ActionRisk.OUTREACH,
        target_type="hyperlocal_site",
        target_id=str(site_id),
        draft_payload=payload,
    )
    return {"status": "awaiting_approval", "approval": approval, "publish_payload": payload}


@router.post("/{site_id}/publish")
async def publish_site(
    site_id: uuid.UUID,
    body: SitePublishFinalize,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    try:
        async with tenant_tx(ctx) as conn:
            revision = await _fetch_site_revision(conn, ctx, site_id, body.revision_id, "publish")
            payload = _publish_payload(site_id, revision, body.hostname)
            approval = await conn.fetchrow(
                """
                SELECT * FROM action_approvals
                 WHERE tenant_id=$1::uuid AND id=$2::uuid
                   AND action_type='studio.site.publish'
                   AND target_type='hyperlocal_site' AND target_id=$3
                """,
                ctx.tenant_id,
                body.approval_id,
                str(site_id),
            )
            if approval is None:
                raise HTTPException(status_code=404, detail="Publish approval not found.")
            if approval["status"] != "approved" or approval["expires_at"] <= datetime.now(timezone.utc):
                raise HTTPException(status_code=409, detail="Publish approval is not active and approved.")
            if str(approval["payload_hash"]) != payload_hash(payload):
                raise HTTPException(status_code=409, detail="Publish payload changed after approval.")

            # The site trail (0173) records this move with who made it; this
            # names the approval and its reviewer beside it.
            await _site_event_evidence(conn, {
                "approval_id": str(body.approval_id),
                "approved_by": approval.get("decided_by"),
            })
            site = await conn.fetchrow(
                """
                UPDATE hyperlocal_sites
                   SET published_revision_id=$3::uuid,preview_revision_id=$3::uuid,
                       status='published',published_at=now()
                 WHERE tenant_id=$1::uuid AND id=$2::uuid AND status <> 'archived'
                RETURNING *
                """,
                ctx.tenant_id,
                site_id,
                body.revision_id,
            )
            if site is None:
                raise HTTPException(status_code=404, detail="Site not found.")
            domain = None
            if body.hostname:
                # A claim's state moves only on DNS evidence (verify route); a
                # republish never resets a verified or failed claim.
                domain = await conn.fetchrow(
                    """
                    INSERT INTO hyperlocal_site_domains (tenant_id,site_id,hostname,status)
                    VALUES ($1::uuid,$2::uuid,$3,'pending')
                    ON CONFLICT (tenant_id,site_id,hostname) DO UPDATE
                        SET updated_at=now()
                    RETURNING *
                    """,
                    ctx.tenant_id,
                    site_id,
                    body.hostname,
                )
            tenant_slug = await conn.fetchval("SELECT slug FROM tenants WHERE id=$1::uuid", ctx.tenant_id)
    except asyncpg.UniqueViolationError as exc:
        # UNIQUE (hostname) across every brokerage: the name is claimed by
        # another site. The hostname is part of the approved payload, so the
        # whole publish rolls back. Who holds it is not said.
        if getattr(exc, "constraint_name", None) == "hyperlocal_site_domains_hostname_key":
            raise HTTPException(status_code=409, detail="This hostname is not available.") from None
        raise
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="studio_site_published",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(site_id),
        metadata={
            "revision_id": str(body.revision_id),
            "content_sha256": str(revision["content_sha256"]),
            "approval_id": str(body.approval_id),
            # The claim's own state: a republish no longer resets it to pending.
            "custom_domain_status": domain["status"] if domain else None,
        },
    )
    return {
        "site": _row(site),
        "domain": _row(domain) if domain else None,
        # Served by public_sites_api; site slugs are unique only within a
        # brokerage, so the brokerage's (global) slug is part of the address.
        "platform_path": public_site_path(str(tenant_slug or ""), site["slug"]),
        # "pending_verification" kept for existing readers; otherwise the
        # claim's own state, which only DNS evidence moves.
        "custom_domain_status": (
            "not_requested" if not domain
            else "pending_verification" if domain["status"] == "pending"
            else domain["status"]
        ),
        "dns_challenge": (
            site_domains.challenge(domain["hostname"], domain["challenge_token"]) if domain else None
        ),
    }


async def _site_event_evidence(conn: Any, evidence: dict[str, Any]) -> None:
    """What the 0173 site trigger stores as this transaction's evidence beside
    the actor it reads from the session. Transaction-local (SET LOCAL)."""
    await conn.execute("SELECT set_config('app.site_event_evidence', $1, true)", canonical_json(evidence))


# ── Custom domains: DNS evidence only (T14.04) ───────────────────────────────

# A check costs up to two bounded DNS lookups; one per claim per this many
# seconds is plenty for a person pressing "Check DNS".
_RECHECK_SECONDS = 10


def _domain_view(row: Any) -> dict[str, Any]:
    last_check = _json(row["last_check"]) if row["last_check"] is not None else None
    return {
        "id": str(row["id"]),
        "site_id": str(row["site_id"]),
        "hostname": row["hostname"],
        "status": row["status"],
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
        "last_checked_at": row["last_checked_at"].isoformat() if row["last_checked_at"] else None,
        "last_outcome": last_check.get("outcome") if isinstance(last_check, dict) else None,
        "challenge": site_domains.challenge(row["hostname"], row["challenge_token"]),
    }


@router.get("/{site_id}/domains")
async def list_site_domains(
    site_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        await _site_for_access(conn, ctx, site_id, capability="publish")
        rows = await conn.fetch(
            """
            SELECT * FROM hyperlocal_site_domains
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid
             ORDER BY created_at, hostname
            """,
            ctx.tenant_id,
            site_id,
        )
    target = site_domains.serving_target()
    return {
        "domains": [_domain_view(row) for row in rows],
        # Whether "active" is reachable at all on this platform: only when the
        # operator has declared where custom hostnames are served.
        "serving": {"available": bool(target), "target": target},
    }


@router.post("/{site_id}/domains/{domain_id}/verify")
async def verify_site_domain(
    site_id: uuid.UUID,
    domain_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
    resolver: DnsResolver = Depends(get_dns_resolver),
) -> dict[str, Any]:
    """Look the claim's challenge up in DNS and move its state on what DNS
    answered. Always 200 for an answer about DNS: in production the edge turns
    a backend 503 into an HTML 504, so "DNS unreachable, retry" travels in the
    body (retryable, retry_after_seconds) and writes nothing at all."""
    domain_sql = """
        SELECT d.*, d.last_checked_at > now() - make_interval(secs => $4) AS too_soon
          FROM hyperlocal_site_domains d
         WHERE d.tenant_id=$1::uuid AND d.site_id=$2::uuid AND d.id=$3::uuid
    """
    async with tenant_tx(ctx) as conn:
        site = await _site_for_access(conn, ctx, site_id, capability="publish")
        if site["status"] == "archived":
            raise HTTPException(status_code=404, detail="Site not found.")
        seen = await conn.fetchrow(domain_sql, ctx.tenant_id, site_id, domain_id, float(_RECHECK_SECONDS))
    if seen is None:
        raise HTTPException(status_code=404, detail="Domain not found.")
    if seen["status"] == "retired":
        raise HTTPException(status_code=409, detail="This domain was retired and is no longer checked.")
    if seen["too_soon"]:
        raise HTTPException(status_code=429, detail="This domain was checked moments ago. Try again in a few seconds.")

    # DNS runs outside any transaction: no pooled connection or row lock is
    # held across network I/O.
    target = site_domains.serving_target()
    try:
        observed = await site_domains.check_domain(
            resolver, hostname=seen["hostname"], token=seen["challenge_token"], target=target)
    except DnsUnavailable:
        return {
            "domain": _domain_view(seen),
            "outcome": "dns_unavailable",
            "retryable": True,
            "retry_after_seconds": 30,
            "message": site_domains.outcome_message(
                "dns_unavailable", status=seen["status"], previous=seen["status"], target=target,
                site_published=site["status"] == "published"),
        }

    current = step = None
    try:
        async with tenant_tx(ctx) as conn:
            # Re-checked at write time (C3.4): the right to publish, and the site.
            site = await _site_for_access(conn, ctx, site_id, capability="publish", for_update=True)
            if site["status"] == "archived":
                raise HTTPException(status_code=404, detail="Site not found.")
            published = site["status"] == "published"
            next_status, outcome = site_domains.decide(
                seen["status"], txt=observed.txt, cname=observed.cname, site_published=published,
                target_configured=bool(target))
            checked_at = datetime.now(timezone.utc).isoformat()
            current = seen["status"]
            # 0173 allows active only from verified: a pending or failed claim
            # whose TXT and CNAME both matched is written verified, then active,
            # each move compare-and-set and in the trail with this check's evidence.
            for step in site_domains.steps(seen["status"], next_status):
                evidence = {**observed.evidence, "outcome": outcome if step == next_status else step,
                            "checked_at": checked_at}
                row = await conn.fetchrow(
                    """
                    UPDATE hyperlocal_site_domains
                       SET status=$4,
                           verified_at=CASE
                               WHEN $4 IN ('verified','active')
                               THEN CASE WHEN status IN ('verified','active') THEN verified_at ELSE now() END
                           END,
                           verification_hash=CASE WHEN $4 IN ('verified','active') THEN $5 END,
                           last_checked_at=now(),
                           last_check=$6::jsonb
                     WHERE tenant_id=$1::uuid AND site_id=$2::uuid AND id=$3::uuid
                       AND status=$7 AND challenge_token=$8
                    RETURNING *
                    """,
                    ctx.tenant_id,
                    site_id,
                    domain_id,
                    step,
                    observed.matched_sha256,
                    canonical_json(evidence),
                    current,
                    seen["challenge_token"],
                )
                if row is None:
                    raise HTTPException(status_code=409,
                                        detail="This domain changed while it was being checked. Check again.")
                current = step
    except asyncpg.CheckViolationError as exc:
        # The 0173 trigger refused the move; the transaction rolled back, so
        # nothing was written. A conflict to report, never a 500, but also a
        # defect to find (steps() should only make legal moves): logged with
        # ids and statuses, never the hostname.
        log.error("Domain %s: the database refused %s -> %s (SQLSTATE %s); nothing changed.",
                  domain_id, current, step, exc.sqlstate)
        raise HTTPException(status_code=409,
                            detail="This domain could not be updated from this check. Nothing changed.") from None
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="studio_site_domain_checked",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(site_id),
        metadata={"domain_id": str(domain_id), "outcome": outcome,
                  "from_status": seen["status"], "to_status": next_status},
    )
    return {
        "domain": _domain_view(row),
        "outcome": outcome,
        "retryable": False,
        "message": site_domains.outcome_message(
            outcome, status=next_status, previous=seen["status"], target=target, site_published=published),
    }


# ── Rollback: re-point the live revision (T14.04) ────────────────────────────

@router.post("/{site_id}/rollback")
async def rollback_site(
    site_id: uuid.UUID,
    body: SiteRollbackRequest,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Make a revision that was live before live again. Revisions are
    immutable: this re-points ``published_revision_id`` and edits nothing,
    and the draft in progress (``preview_revision_id``) is left alone.

    No new approval is raised: the restored content was approved and live
    before, and its content hash cannot change (revisions are immutable,
    0173). The approvals it was published under are named in the trail and
    the audit record. Whether a rollback should need a fresh approval is an
    owner question (L14)."""
    async with tenant_tx(ctx) as conn:
        site = await _site_for_access(conn, ctx, site_id, capability="publish", for_update=True)
        if site["status"] == "archived":
            raise HTTPException(status_code=404, detail="Site not found.")
        live = site["published_revision_id"]
        if site["status"] != "published" or live is None:
            raise HTTPException(status_code=409, detail="This site is not published, so there is nothing to roll back.")
        if body.to_revision_id is not None and live == body.to_revision_id and live != body.expected_live_revision_id:
            # A replay of a rollback that already happened: no second effect.
            return {"site": _row(site), "live_revision_id": str(live), "previous_live_revision_id": None,
                    "already_live": True}
        if live != body.expected_live_revision_id:
            raise HTTPException(status_code=409, detail="The live version changed. Reload and try again.")
        target = body.to_revision_id or await _previous_live_revision(conn, ctx, site_id, live)
        if target is None:
            raise HTTPException(status_code=409, detail="There is no earlier published version to restore.")
        if target == live:
            raise HTTPException(status_code=409, detail="That version is already live.")
        if not await _was_live(conn, ctx, site_id, target):
            raise HTTPException(
                status_code=409, detail="That version was never published, so it cannot be restored.")
        revision = await conn.fetchrow(
            """
            SELECT id, revision, content_sha256 FROM hyperlocal_site_revisions
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid AND id=$3::uuid
            """,
            ctx.tenant_id,
            site_id,
            target,
        )
        if revision is None:
            raise HTTPException(status_code=404, detail="Site revision not found.")
        approvals = await conn.fetch(
            """
            SELECT id, decided_by FROM action_approvals
             WHERE tenant_id=$1::uuid AND action_type='studio.site.publish'
               AND target_type='hyperlocal_site' AND target_id=$2
               AND status='approved' AND draft_payload->>'revision_id'=$3
             ORDER BY decided_at
            """,
            ctx.tenant_id,
            str(site_id),
            str(target),
        )
        approval_ids = [str(row["id"]) for row in approvals]
        await _site_event_evidence(conn, {
            "rollback": True,
            "restored_content_sha256": str(revision["content_sha256"]),
            "original_approval_ids": approval_ids,
        })
        updated = await conn.fetchrow(
            """
            UPDATE hyperlocal_sites
               SET published_revision_id=$3::uuid,published_at=now()
             WHERE tenant_id=$1::uuid AND id=$2::uuid
               AND status='published' AND published_revision_id=$4::uuid
            RETURNING *
            """,
            ctx.tenant_id,
            site_id,
            target,
            live,
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="The live version changed. Reload and try again.")
        tenant_slug = await conn.fetchval("SELECT slug FROM tenants WHERE id=$1::uuid", ctx.tenant_id)
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="studio_site_rolled_back",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(site_id),
        metadata={
            "from_revision_id": str(live),
            "to_revision_id": str(target),
            "to_content_sha256": str(revision["content_sha256"]),
            "original_approval_ids": approval_ids,
        },
    )
    return {
        "site": _row(updated),
        "live_revision_id": str(target),
        "live_revision": revision["revision"],
        "previous_live_revision_id": str(live),
        "already_live": False,
        "platform_path": public_site_path(str(tenant_slug or ""), updated["slug"]),
    }


@router.post("/{site_id}/attribution", status_code=status.HTTP_201_CREATED)
async def record_attribution(
    site_id: uuid.UUID,
    body: AttributionCreate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        await _site_for_access(conn, ctx, site_id)
        row = await conn.fetchrow(
            """
            INSERT INTO hyperlocal_site_attribution_events (
                tenant_id,site_id,event_type,subject_kind,subject_id,session_hash,
                source,medium,campaign,content,metadata
            )
            SELECT $1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb
             WHERE EXISTS (
                 SELECT 1 FROM hyperlocal_sites
                  WHERE tenant_id=$1::uuid AND id=$2::uuid AND status <> 'archived'
             )
            RETURNING *
            """,
            ctx.tenant_id,
            site_id,
            body.event_type,
            body.subject_kind,
            body.subject_id,
            body.session_hash,
            body.source,
            body.medium,
            body.campaign,
            body.content,
            canonical_json(body.metadata),
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    return {"event": _row(row)}


@router.get("/{site_id}/funnel")
async def site_funnel(
    site_id: uuid.UUID,
    days: int = Query(default=90, ge=1, le=730),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        await _site_for_access(conn, ctx, site_id)
        rows = await conn.fetch(
            """
            SELECT event_type,source,medium,COUNT(*)::int AS event_count
              FROM hyperlocal_site_attribution_events
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid
               AND occurred_at >= now()-make_interval(days => $3)
             GROUP BY event_type,source,medium
             ORDER BY event_type, event_count DESC
            """,
            ctx.tenant_id,
            site_id,
            days,
        )
    return {
        "site_id": str(site_id),
        "window_days": days,
        "breakdown": [_row(row) for row in rows],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "note": "Attribution is directional; closing credit follows recorded source events.",
    }


@router.get("/{site_id}/collaborators")
async def list_site_collaborators(
    site_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        await _site_for_access(conn, ctx, site_id, capability="edit")
        rows = await conn.fetch(
            """
            SELECT agent_id,can_edit,can_publish,created_at,updated_at
              FROM hyperlocal_site_collaborators
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid
             ORDER BY agent_id
            """,
            ctx.tenant_id,
            site_id,
        )
    return {"collaborators": [_row(row) for row in rows]}


@router.put("/{site_id}/collaborators/{agent_id}")
async def upsert_site_collaborator(
    site_id: uuid.UUID,
    agent_id: str,
    body: SiteCollaboratorUpsert,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    if body.agent_id.casefold() != agent_id.casefold():
        raise HTTPException(status_code=422, detail="Path and body agent_id must match.")
    async with tenant_tx(ctx) as conn:
        site = await _site_for_access(conn, ctx, site_id, capability="edit")
        if not (ctx.is_platform_admin or ctx.is_broker_owner or site["owner_agent_id"] == ctx.agent_id):
            raise HTTPException(status_code=403, detail="Only the site owner can manage collaborators.")
        canonical_agent = await conn.fetchval(
            """
            SELECT agent_id FROM users
             WHERE tenant_id=$1::uuid AND lower(agent_id)=lower($2) AND is_active=true
            """,
            ctx.tenant_id,
            body.agent_id,
        )
        if canonical_agent is None:
            raise HTTPException(status_code=422, detail="Collaborator must be an active brokerage user.")
        if canonical_agent == site["owner_agent_id"]:
            raise HTTPException(status_code=409, detail="The site owner already has full access.")
        row = await conn.fetchrow(
            """
            INSERT INTO hyperlocal_site_collaborators (
                tenant_id,site_id,agent_id,can_edit,can_publish,created_by
            ) VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6)
            ON CONFLICT (tenant_id,site_id,agent_id) DO UPDATE
                SET can_edit=EXCLUDED.can_edit,can_publish=EXCLUDED.can_publish,
                    updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            site_id,
            canonical_agent,
            body.can_edit,
            body.can_publish,
            ctx.agent_id,
        )
    return {"collaborator": _row(row)}


@router.delete("/{site_id}/collaborators/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_site_collaborator(
    site_id: uuid.UUID,
    agent_id: str,
    ctx: TenantContext = Depends(require_context),
) -> None:
    async with tenant_tx(ctx) as conn:
        site = await _site_for_access(conn, ctx, site_id, capability="edit")
        if not (ctx.is_platform_admin or ctx.is_broker_owner or site["owner_agent_id"] == ctx.agent_id):
            raise HTTPException(status_code=403, detail="Only the site owner can manage collaborators.")
        result = await conn.execute(
            """
            DELETE FROM hyperlocal_site_collaborators
             WHERE tenant_id=$1::uuid AND site_id=$2::uuid AND lower(agent_id)=lower($3)
            """,
            ctx.tenant_id,
            site_id,
            agent_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Collaborator not found.")
