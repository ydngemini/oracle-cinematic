"""Property View — address-first property page with agent + client media capture.

Three capabilities:
  1. Enter an address → resolve it and pull what public/licensed sources
     actually say about it (geocode + enrichment already in server.py).
  2. Agents upload exterior/interior photos and video.
  3. Agents mint a revocable, expiring link so a CLIENT with no account can
     upload media for that one property. Client uploads land in `pending` and
     are invisible until an agent approves them.

Deliberate boundaries:
  * NO listing-site scraping. `spatial_agent._scrape_zillow_images` exists but
    is gated off (SPATIAL_ALLOW_WEB_SCRAPE) for ToS reasons and stays that way.
    "Found through internet listings" here means geocoding + licensed/public
    data providers + any RESO feed the tenant is actually authorised for.
  * Video is S3-only (migration 0066 enforces it). media_blobs is bytea, which
    is fine for photos and ruinous for video.
  * Client-supplied media is untrusted: quota-capped, size-capped, magic-byte
    sniffed, and held for review.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.connection import tenant_tx
from tenancy import Role, TenantContext, require_context

log = logging.getLogger("oracle.property_view")

router = APIRouter(prefix="/api", tags=["property-view"])

# Client links are short-lived by default; an upload link is a capability URL
# and every extra day is extra exposure.
DEFAULT_LINK_TTL_HOURS = 72
MAX_LINK_TTL_HOURS = 24 * 14

MAX_PHOTO_BYTES = 25 * 1024 * 1024      # matches MediaUploader's client guard
MAX_VIDEO_BYTES = 512 * 1024 * 1024

_VALID_SURFACES = {"exterior", "interior", "aerial", "street", "other"}

# Magic-byte prefixes. Never trust the client's Content-Type: it is attacker
# controlled and is what turns an "image" upload into stored HTML/SVG XSS.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_VIDEO_BRANDS = {
    b"mp4", b"isom", b"iso2", b"avc1", b"mmp4", b"M4V ", b"qt  ",
}


def _sniff_image(data: bytes) -> Optional[str]:
    for signature, content_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return content_type
    # WEBP is RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sniff_video(data: bytes) -> Optional[str]:
    """ISO-BMFF (mp4/mov) and WebM only. No transcoding, no container zoo."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _VIDEO_BRANDS:
            return "video/quicktime" if brand in (b"qt  ",) else "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _subject_or_422(lead_id: Optional[UUID], listing_id: Optional[UUID]) -> None:
    if (lead_id is None) == (listing_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide exactly one of lead_id or listing_id.",
        )


# ---------------------------------------------------------------------------
# Agent-facing: the property view
# ---------------------------------------------------------------------------

@router.get("/crm/property-view")
async def property_view(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Everything Property View renders for one property: media by surface,
    pending client uploads, and active upload links."""
    _subject_or_422(lead_id, listing_id)

    async with tenant_tx(ctx) as conn:
        media = await conn.fetch(
            """
            SELECT id, kind, surface, url, s3_key, uploaded_via, review_status,
                   duration_seconds, sort_order, created_at
              FROM property_media
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             ORDER BY sort_order ASC, created_at ASC
            """,
            lead_id, listing_id,
        )
        links = await conn.fetch(
            """
            SELECT id, label, recipient_hint, expires_at, revoked_at,
                   max_uploads, upload_count, created_at, last_used_at
              FROM property_view_upload_links
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             ORDER BY created_at DESC
             LIMIT 50
            """,
            lead_id, listing_id,
        )

    def media_row(row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "kind": row["kind"],
            "surface": row["surface"],
            "url": row["url"] or f"/api/media/{row['id']}",
            "uploaded_via": row["uploaded_via"],
            "review_status": row["review_status"],
            "duration_seconds": float(row["duration_seconds"]) if row["duration_seconds"] else None,
            "created_at": row["created_at"].isoformat(),
        }

    approved = [media_row(r) for r in media if r["review_status"] == "approved"]
    pending = [media_row(r) for r in media if r["review_status"] == "pending"]

    by_surface: dict[str, list[dict[str, Any]]] = {}
    for item in approved:
        by_surface.setdefault(item["surface"] or "other", []).append(item)

    now = datetime.now(timezone.utc)
    return {
        "media": approved,
        "by_surface": by_surface,
        "pending_review": pending,
        "counts": {
            "total": len(approved),
            "pending": len(pending),
            "exterior": len(by_surface.get("exterior", [])),
            "interior": len(by_surface.get("interior", [])),
        },
        "upload_links": [
            {
                "id": str(r["id"]),
                "label": r["label"],
                "recipient_hint": r["recipient_hint"],
                "expires_at": r["expires_at"].isoformat(),
                "revoked": r["revoked_at"] is not None,
                "expired": r["expires_at"] <= now,
                "max_uploads": r["max_uploads"],
                "upload_count": r["upload_count"],
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            }
            for r in links
        ],
    }


@router.get("/crm/property-view/resolve")
async def resolve_subject(
    address: str = Query(min_length=3, max_length=300),
    ctx: TenantContext = Depends(require_context),
):
    """Find existing leads/listings for an address so media attaches to the
    right record. Media is always tied to a CRM record, never to a bare string —
    otherwise photos orphan themselves the moment the deal progresses."""
    # Trigram-free prefix/substring match; the tenant's record count is small
    # enough that ILIKE is fine and avoids a pg_trgm dependency.
    pattern = f"%{address.strip()}%"

    async with tenant_tx(ctx) as conn:
        leads = await conn.fetch(
            """
            SELECT id, address, motivation_score, created_at
              FROM leads
             WHERE address ILIKE $1
             ORDER BY created_at DESC
             LIMIT 10
            """,
            pattern,
        )
        listings = await conn.fetch(
            """
            SELECT id, address, price, status, created_at
              FROM listings
             WHERE address ILIKE $1
             ORDER BY created_at DESC
             LIMIT 10
            """,
            pattern,
        )

    return {
        "leads": [
            {"id": str(r["id"]), "address": r["address"], "motivation_score": r["motivation_score"]}
            for r in leads
        ],
        "listings": [
            {
                "id": str(r["id"]),
                "address": r["address"],
                "price": float(r["price"]) if r["price"] is not None else None,
                "status": r["status"],
            }
            for r in listings
        ],
    }


class CreateSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=3, max_length=300)
    state: str = Field(min_length=2, max_length=2)

    @field_validator("state")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


@router.post("/crm/property-view/subject", status_code=status.HTTP_201_CREATED)
async def create_subject(
    body: CreateSubject,
    ctx: TenantContext = Depends(require_context),
):
    """Create a minimal lead so an address the tenant has never seen can still
    hold media. `parcel_id` is synthesised from the address because the real
    parcel is unknown until enrichment runs — the UNIQUE(tenant, parcel) upsert
    key from 0018 still needs a deterministic value."""
    address = body.address.strip()
    parcel_id = f"pv:{hashlib.sha256(address.lower().encode()).hexdigest()[:32]}"

    async with tenant_tx(ctx) as conn:
        existing = await conn.fetchval(
            "SELECT id FROM leads WHERE parcel_id = $1", parcel_id,
        )
        if existing is not None:
            return {"lead_id": str(existing), "created": False}

        lead_id = await conn.fetchval(
            """
            INSERT INTO leads (tenant_id, parcel_id, state, motivation_score, address)
            VALUES ($1, $2, $3, 0, $4)
            RETURNING id
            """,
            ctx.tenant_id, parcel_id, body.state, address,
        )
    return {"lead_id": str(lead_id), "created": True}


@router.post("/crm/property-view/media", status_code=status.HTTP_201_CREATED)
async def agent_upload_media(
    surface: str = Form(default="exterior"),
    files: list[UploadFile] = File(...),
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Agent-authenticated upload that accepts photos AND video with a surface
    tag.

    The pre-existing /crm/leads/{id}/media route is photo-only (its _persist
    hardcodes kind='photo' and sniffs images), so this is a separate route
    rather than a change to it — media_api's contract is relied on elsewhere.
    """
    _subject_or_422(lead_id, listing_id)
    if surface not in _VALID_SURFACES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown surface.")
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide at least one file.")
    if len(files) > 40:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Upload exceeds 40 files.")

    created: list[dict[str, Any]] = []

    async with tenant_tx(ctx) as conn:
        if lead_id is not None and not await conn.fetchval("SELECT 1 FROM leads WHERE id = $1", lead_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
        if listing_id is not None and not await conn.fetchval("SELECT 1 FROM listings WHERE id = $1", listing_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")

        # Serialize concurrent uploads to the SAME property so two requests
        # can't read the same MAX(sort_order) and collide (mirrors
        # media_api._persist). Releases when this tenant_tx commits.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            str(listing_id or lead_id or ""),
        )
        next_order = await conn.fetchval(
            """
            SELECT COALESCE(MAX(sort_order), -1) + 1
              FROM property_media
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
            """,
            lead_id, listing_id,
        ) or 0

        for upload in files:
            data = await upload.read(MAX_VIDEO_BYTES + 1)
            if not data:
                continue

            content_type = _sniff_image(data)
            kind = "photo"
            if content_type is None:
                content_type = _sniff_video(data)
                kind = "video"
            if content_type is None:
                raise HTTPException(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    f"{upload.filename or 'file'} is not an accepted image or video.",
                )

            limit = MAX_PHOTO_BYTES if kind == "photo" else MAX_VIDEO_BYTES
            if len(data) > limit:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"{upload.filename or kind} exceeds the {limit // (1024 * 1024)} MB limit.",
                )

            s3_key = None
            if kind == "video":
                import object_storage

                if not object_storage.is_configured():
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "Video uploads require object storage, which is not configured "
                        "on this deployment. Photos still work.",
                    )
                s3_key = _put_video_to_storage(data, content_type, str(ctx.tenant_id))

            # `url` is NOT NULL and embeds the row id, so generate the id here
            # and do it in one INSERT (same rationale as media_api._persist).
            media_id = uuid4()
            await conn.execute(
                """
                INSERT INTO property_media (
                    id, tenant_id, lead_id, listing_id, kind, surface, url,
                    s3_key, sort_order, uploaded_via, review_status
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'agent','approved')
                """,
                media_id, ctx.tenant_id, lead_id, listing_id, kind, surface,
                f"/api/media/{media_id}", s3_key, next_order,
            )
            next_order += 1

            if kind == "photo":
                await conn.execute(
                    """
                    INSERT INTO media_blobs (media_id, content_type, byte_size, bytes)
                    VALUES ($1,$2,$3,$4)
                    """,
                    media_id, content_type, len(data), data,
                )

            created.append({"id": str(media_id), "kind": kind, "surface": surface})

    return {"media": created}


class CreateUploadLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, max_length=120)
    recipient_hint: Optional[str] = Field(default=None, max_length=200)
    ttl_hours: int = Field(default=DEFAULT_LINK_TTL_HOURS, ge=1, le=MAX_LINK_TTL_HOURS)
    max_uploads: int = Field(default=40, ge=1, le=500)


@router.post("/crm/property-view/upload-links", status_code=status.HTTP_201_CREATED)
async def create_upload_link(
    body: CreateUploadLink,
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Mint a client upload link. The token is returned ONCE and never stored."""
    _subject_or_422(lead_id, listing_id)

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.ttl_hours)

    async with tenant_tx(ctx) as conn:
        if lead_id is not None and not await conn.fetchval("SELECT 1 FROM leads WHERE id = $1", lead_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
        if listing_id is not None and not await conn.fetchval("SELECT 1 FROM listings WHERE id = $1", listing_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")

        row = await conn.fetchrow(
            """
            INSERT INTO property_view_upload_links (
                tenant_id, lead_id, listing_id, token_hash, label,
                recipient_hint, expires_at, max_uploads, created_by
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
            """,
            ctx.tenant_id, lead_id, listing_id, _hash_token(token),
            body.label, body.recipient_hint, expires_at, body.max_uploads, ctx.agent_id,
        )

    base = os.getenv("ORACLE_PUBLIC_BASE_URL") or os.getenv("ORACLE_BASE_URL", "")
    return {
        "id": str(row["id"]),
        "token": token,  # shown once
        "share_url": f"{base.rstrip('/')}/property-upload/{token}" if base else None,
        "expires_at": expires_at.isoformat(),
        "max_uploads": body.max_uploads,
        "notice": (
            "This link is shown once. Anyone holding it can upload media for this "
            "property until it expires or is revoked. Uploads are held for your "
            "review before appearing anywhere."
        ),
    }


@router.delete("/crm/property-view/upload-links/{link_id}")
async def revoke_upload_link(
    link_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchval(
            """
            UPDATE property_view_upload_links
               SET revoked_at = now()
             WHERE id = $1 AND revoked_at IS NULL
            RETURNING id
            """,
            link_id,
        )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found or already revoked.")
    return {"id": str(link_id), "revoked": True}


class ReviewMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str

    @field_validator("decision")
    @classmethod
    def _check(cls, v: str) -> str:
        if v not in {"approved", "rejected"}:
            raise ValueError("decision must be 'approved' or 'rejected'")
        return v


@router.post("/crm/property-view/media/{media_id}/review")
async def review_media(
    media_id: UUID,
    body: ReviewMedia,
    ctx: TenantContext = Depends(require_context),
):
    """Approve or reject a client-submitted upload."""
    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchval(
            """
            UPDATE property_media
               SET review_status = $2
             WHERE id = $1 AND review_status = 'pending'
            RETURNING id
            """,
            media_id, body.decision,
        )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No pending media with that id.")
    return {"id": str(media_id), "review_status": body.decision}


# ---------------------------------------------------------------------------
# Client-facing: unauthenticated, token-scoped
# ---------------------------------------------------------------------------

async def _resolve_link(conn, token: str):
    """Look a link up by token hash and enforce every gate.

    Runs under a platform-admin system context (the caller has no tenant), so
    the explicit checks here are the only thing standing between a token and a
    write. Order matters: existence, then revocation, then expiry, then quota.
    """
    row = await conn.fetchrow(
        """
        SELECT id, tenant_id, lead_id, listing_id, expires_at, revoked_at,
               max_uploads, upload_count
          FROM property_view_upload_links
         WHERE token_hash = $1
         FOR UPDATE
        """,
        _hash_token(token),
    )
    # One generic message for every failure mode: distinguishing "expired" from
    # "no such token" tells an enumerator which guesses were real.
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This upload link is no longer valid.")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This upload link is no longer valid.")
    if row["upload_count"] >= row["max_uploads"]:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "This upload link has reached its limit. Ask your agent for a new one.",
        )
    return row


@router.get("/public/property-upload/{token}")
async def describe_upload_link(token: str):
    """What the client's upload page shows before they pick files.

    Returns no property detail beyond a display address — a capability URL
    should not become a property-data leak if forwarded.
    """
    system_ctx = TenantContext(
        agent_id="property-view-public",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(system_ctx) as conn:
        row = await _resolve_link(conn, token)
        if row["lead_id"] is not None:
            address = await conn.fetchval("SELECT address FROM leads WHERE id = $1", row["lead_id"])
        else:
            address = await conn.fetchval("SELECT address FROM listings WHERE id = $1", row["listing_id"])

    return {
        "valid": True,
        "address": address,
        "remaining_uploads": row["max_uploads"] - row["upload_count"],
        "expires_at": row["expires_at"].isoformat(),
        "accepted": {
            "photo": ["image/png", "image/jpeg", "image/gif", "image/webp"],
            "video": ["video/mp4", "video/quicktime", "video/webm"],
            "max_photo_mb": MAX_PHOTO_BYTES // (1024 * 1024),
            "max_video_mb": MAX_VIDEO_BYTES // (1024 * 1024),
        },
        "notice": "Your agent reviews everything you send before it is used anywhere.",
    }


@router.post("/public/property-upload/{token}", status_code=status.HTTP_201_CREATED)
async def client_upload(
    token: str,
    surface: str = Form(default="exterior"),
    file: UploadFile = File(...),
):
    """Accept one media file from an unauthenticated client link.

    NOTE: video persistence requires object storage (migration 0066 forbids video
    in media_blobs). Where no storage backend is configured this returns 503 for
    video rather than silently downgrading to a bytea write.
    """
    if surface not in _VALID_SURFACES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown surface.")

    data = await file.read(MAX_VIDEO_BYTES + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty file.")

    content_type = _sniff_image(data)
    kind = "photo"
    if content_type is None:
        content_type = _sniff_video(data)
        kind = "video"
    if content_type is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Only JPEG, PNG, GIF, WEBP images and MP4/MOV/WEBM video are accepted.",
        )

    limit = MAX_PHOTO_BYTES if kind == "photo" else MAX_VIDEO_BYTES
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"{kind.title()} exceeds the {limit // (1024 * 1024)} MB limit.",
        )

    import object_storage

    if kind == "video" and not object_storage.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Video uploads require object storage, which is not configured on this deployment.",
        )

    system_ctx = TenantContext(
        agent_id="property-view-public",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )

    async with tenant_tx(system_ctx) as conn:
        link = await _resolve_link(conn, token)

        s3_key = None
        if kind == "video":
            s3_key = _put_video_to_storage(data, content_type, str(link["tenant_id"]))

        # `url` is NOT NULL and embeds the row id (see media_api._persist).
        media_id = uuid4()
        await conn.execute(
            """
            INSERT INTO property_media (
                id, tenant_id, lead_id, listing_id, kind, surface, url, s3_key,
                uploaded_via, review_status
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'client_link','pending')
            """,
            media_id, link["tenant_id"], link["lead_id"], link["listing_id"],
            kind, surface, f"/api/media/{media_id}", s3_key,
        )

        if kind == "photo":
            await conn.execute(
                """
                INSERT INTO media_blobs (media_id, content_type, byte_size, bytes)
                VALUES ($1,$2,$3,$4)
                """,
                media_id, content_type, len(data), data,
            )

        await conn.execute(
            """
            UPDATE property_view_upload_links
               SET upload_count = upload_count + 1, last_used_at = now()
             WHERE id = $1
            """,
            link["id"],
        )

    log.info("client upload accepted: media=%s kind=%s surface=%s", media_id, kind, surface)
    return {
        "id": str(media_id),
        "kind": kind,
        "surface": surface,
        "review_status": "pending",
        "remaining_uploads": link["max_uploads"] - link["upload_count"] - 1,
    }


def _put_video_to_storage(data: bytes, content_type: str, tenant_id: str) -> str:
    """Store a video in durable object storage and return its key.

    Which backend that is — the Azure Files mount, Blob, or the legacy S3
    bucket — is configuration; see object_storage."""
    import object_storage  # local import: keeps cloud SDKs off the hot import path

    key = f"property-view/{tenant_id}/{secrets.token_hex(16)}"
    return object_storage.put_bytes(key, data, content_type)
