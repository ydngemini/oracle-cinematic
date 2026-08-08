"""Tenant-scoped Neoh Studio sites, previews, publishing, and attribution."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from approval_service import create_approval
from audit_ledger import AuditCategory, ledger
from automation_jobs import canonical_json, payload_hash
from db.connection import tenant_tx
from platform_policy import ActionRisk, enforce_public_property_data
from tenancy import Role, TenantContext, require_context, require_role

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
        }:
            result[key] = _json(value)
    return result


def _safe_asset_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned.startswith("/") and not cleaned.startswith("//"):
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
        return self


class SiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(pattern=_SLUG_RE.pattern)
    template_key: SiteTemplate = "editorial"
    scope: SiteScope = "personal"
    brand_theme: BrandTheme = Field(default_factory=BrandTheme)
    headline: Optional[str] = Field(default=None, min_length=4, max_length=160)
    content: Optional[SiteContent] = None
    authorized_idx_sources: list[AuthorizedIdxSource] = Field(default_factory=list, max_length=20)


class SiteRevisionCreate(BaseModel):
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
        return _normalized_hostname(value) if value else None


class SitePublishFinalize(SitePublishRequest):
    approval_id: uuid.UUID


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
    if row is None:
        raise HTTPException(status_code=404, detail="Site not found.")
    privileged = ctx.is_platform_admin or ctx.is_broker_owner
    owner = row["owner_agent_id"] == ctx.agent_id
    if capability == "read":
        allowed = privileged or owner or row["scope"] == "team" or row["collaborator_can_edit"] or row["collaborator_can_publish"]
    elif capability == "edit":
        allowed = privileged or owner or row["collaborator_can_edit"]
    else:
        allowed = privileged or owner or row["collaborator_can_publish"]
    if not allowed:
        raise HTTPException(status_code=404, detail="Site not found.")
    return row


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
    return {"site": _row(site), "revisions": [_row(row) for row in revisions]}


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
        await conn.execute(
            """
            UPDATE hyperlocal_sites SET preview_revision_id=$3::uuid,status='draft'
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
               SET preview_revision_id=$3::uuid,status='preview'
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
            domain = await conn.fetchrow(
                """
                INSERT INTO hyperlocal_site_domains (tenant_id,site_id,hostname,status)
                VALUES ($1::uuid,$2::uuid,$3,'pending')
                ON CONFLICT (tenant_id,site_id,hostname) DO UPDATE
                    SET status=CASE
                        WHEN hyperlocal_site_domains.status='active' THEN 'active'
                        ELSE 'pending'
                    END,
                        updated_at=now()
                RETURNING *
                """,
                ctx.tenant_id,
                site_id,
                body.hostname,
            )
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
            "custom_domain_pending": bool(body.hostname),
        },
    )
    return {
        "site": _row(site),
        "domain": _row(domain) if domain else None,
        "platform_path": f"/sites/{site['slug']}",
        "custom_domain_status": "pending_verification" if domain else "not_requested",
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
