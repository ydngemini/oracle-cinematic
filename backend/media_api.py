"""Neoh 2D image upload + serve API.

Agents (in-app, JWT) and homeowners (passwordless client portal) attach property
photos to a lead or listing. The metadata row lives in the existing tenant-scoped
`property_media` table (kind='photo'); the raw bytes live in the non-tenant-scoped
`media_blobs` table (migration 0022) so the PUBLIC `GET /api/media/{id}` serve
path — which backs <img src> references and has no tenant context — can stream
them without tripping property_media's FORCE row-level security.

Why bytes-in-Postgres instead of files-on-disk: the container runs as uid 1001
against a bind mount that is not reliably writable (CLAUDE.md / prod-readiness
audit), and per-tenant RLS makes a shared writable dir awkward. A DB blob keyed
by the media row's random uuid is the robust, deployment-agnostic choice.

Endpoints
  POST   /api/crm/leads/{lead_id}/media        (agent JWT)  — upload photo(s)
  POST   /api/crm/listings/{listing_id}/media  (agent JWT)  — upload photo(s)
  GET    /api/crm/media?lead_id=&listing_id=    (agent JWT)  — list photos
  DELETE /api/crm/media/{media_id}             (agent JWT)  — remove a photo
  POST   /api/portal/media                     (portal token) — explicit read-only rejection
  GET    /api/media/{media_id}                 (public)     — stream the image
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import List, Optional
from uuid import UUID, uuid4

import jwt
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response

from auth import ALGORITHM, SECRET_KEY
from contract_vault import VaultUploadError
from db.connection import get_pool, tenant_tx
from tenancy import TenantContext, require_context

log = logging.getLogger("oracle.media_api")

router = APIRouter(prefix="/api", tags=["media"])

# 12 MB hard cap per image.
MAX_BYTES = 12 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 30
_CONTRACT_RATE_LIMIT = int(os.getenv("CONTRACT_GENERATION_RATE_LIMIT", "10"))
_CONTRACT_RATE_WINDOW = 60 * 60.0
_contract_generation_timestamps: dict[str, list[float]] = defaultdict(list)

# Media kinds permitted by property_media's chk_media_kind constraint (0012). The
# photo-upload endpoints below only produce 'photo'; the walkable-tour pipeline
# (capture wizard / reconstruction worker) writes 'pano'/'splat'/'tour' rows that
# the tour resolver (tour_api.py) reads to pick the highest available tier.
_ALLOWED_KINDS = {"photo", "pano", "splat", "tour", "floorplan", "document"}

# Magic-byte sniff → canonical content-type. We trust the file signature over the
# client-declared Content-Type (which is attacker-controlled and easily spoofed).
def _sniff_image(data: bytes) -> Optional[str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _read_and_validate(f: UploadFile) -> tuple[bytes, str]:
    """Slurp one upload, enforce the size cap, and confirm it is really an image.

    Raises 413 (too big), 415 (not an image), or 422 (empty)."""
    # Read at most MAX_BYTES+1 so an oversized upload is rejected WITHOUT first
    # materialising the whole body in memory (a full f.read() lets a huge file
    # exhaust the worker before the size check below).
    data = await f.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty file upload.")
    if len(data) > MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Image exceeds {MAX_BYTES // (1024 * 1024)}MB limit.",
        )
    content_type = _sniff_image(data)
    if content_type is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Upload is not a recognised image (png, jpeg, gif, webp).",
        )
    return data, content_type


async def _persist(
    conn,
    *,
    tenant_id: str,
    lead_id: Optional[UUID],
    listing_id: Optional[UUID],
    files: List[UploadFile],
    kind: str = "photo",
) -> list[dict]:
    """Insert each validated image as a property_media row (+ media_blobs bytes),
    appending to the existing order. Runs inside the caller's tenant_tx so
    RLS scopes every write to the request's tenant. `kind` defaults to 'photo'
    (the only kind these image endpoints accept today); the capture/reconstruction
    pipeline passes 'pano'/'splat'/'tour'."""
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide at least one image.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Upload exceeds the {MAX_FILES_PER_UPLOAD} image limit.",
        )
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported media kind: {kind!r}")
    # Serialize concurrent uploads to the SAME property so two requests can't read
    # the same MAX(sort_order) and collide on positions (gallery order must stay
    # deterministic). A per-property advisory xact-lock is lighter than locking the
    # parent row and releases automatically when this tenant_tx commits.
    _lock_key = str(listing_id or lead_id or "")
    if _lock_key:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", _lock_key)
    # Next sort_order for this property (tenant-scoped via RLS on the SELECT).
    base = await conn.fetchval(
        """
        SELECT COALESCE(MAX(sort_order), -1)
          FROM property_media
         WHERE ($1::uuid IS NOT NULL AND lead_id = $1)
            OR ($2::uuid IS NOT NULL AND listing_id = $2)
        """,
        lead_id,
        listing_id,
    )

    created: list[dict] = []
    for f in files:
        data, content_type = await _read_and_validate(f)
        base += 1
        # url is NOT NULL and must point at the public serve path, which embeds
        # the row id. Generate the id client-side so the url is known up front and
        # the whole thing is a single INSERT (a writable CTE can't UPDATE the row
        # its own INSERT just created — same-statement snapshot).
        new_id = uuid4()
        row = await conn.fetchrow(
            """
            INSERT INTO property_media
                (id, tenant_id, lead_id, listing_id, kind, url, sort_order)
            VALUES ($1, $2, $3, $4, $7, $5, $6)
            RETURNING id, url, kind, sort_order, created_at
            """,
            new_id,
            tenant_id,
            lead_id,
            listing_id,
            f"/api/media/{new_id}",
            base,
            kind,
        )
        await conn.execute(
            """
            INSERT INTO media_blobs (media_id, content_type, byte_size, bytes)
            VALUES ($1, $2, $3, $4)
            """,
            row["id"],
            content_type,
            len(data),
            data,
        )
        created.append(
            {
                "id": str(row["id"]),
                "url": row["url"],
                "kind": row["kind"],
                "sort_order": row["sort_order"],
                "created_at": row["created_at"].isoformat(),
            }
        )

    return created


# ---------------------------------------------------------------------------
# Agent endpoints — JWT (require_context → TenantContext), RLS-scoped.
# ---------------------------------------------------------------------------

@router.post("/crm/leads/{lead_id}/media", status_code=status.HTTP_201_CREATED)
async def upload_lead_media(
    lead_id: UUID,
    files: List[UploadFile] = File(...),
    ctx: TenantContext = Depends(require_context),
):
    """Attach one or more photos to a lead (the primary property record — 257k
    leads vs few listings). Returns the created media rows."""
    async with tenant_tx(ctx) as conn:
        if not await conn.fetchval("SELECT 1 FROM leads WHERE id = $1", lead_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
        created = await _persist(
            conn, tenant_id=ctx.tenant_id, lead_id=lead_id, listing_id=None, files=files
        )
    log.info("Lead media uploaded: lead_id=%s count=%d tenant=%s", lead_id, len(created), ctx.tenant_id)
    return {"media": created}


@router.post("/crm/listings/{listing_id}/media", status_code=status.HTTP_201_CREATED)
async def upload_listing_media(
    listing_id: UUID,
    files: List[UploadFile] = File(...),
    ctx: TenantContext = Depends(require_context),
):
    """Attach one or more photos to a listing. Returns the created media rows."""
    async with tenant_tx(ctx) as conn:
        if not await conn.fetchval("SELECT 1 FROM listings WHERE id = $1", listing_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")
        created = await _persist(
            conn, tenant_id=ctx.tenant_id, lead_id=None, listing_id=listing_id, files=files
        )
    log.info("Listing media uploaded: listing_id=%s count=%d tenant=%s", listing_id, len(created), ctx.tenant_id)
    return {"media": created}


@router.get("/crm/media")
async def list_media(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    kind: str = Query(default="photo", description="media kind to list, or 'all' for every kind"),
    ctx: TenantContext = Depends(require_context),
):
    """List media for a lead or listing, ordered by sort_order. RLS-scoped.

    Defaults to kind='photo' so the existing photo filmstrip is unchanged; pass
    kind='all' (or a specific kind like 'splat'/'pano'/'tour') so the walkable-
    tour surfaces can enumerate non-photo media."""
    if lead_id is None and listing_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide lead_id or listing_id.",
        )
    kind_filter = None if kind == "all" else kind
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, url, kind, sort_order, created_at
              FROM property_media
             WHERE ($3::text IS NULL OR kind = $3)
               AND (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             ORDER BY sort_order ASC, created_at ASC
            """,
            lead_id,
            listing_id,
            kind_filter,
        )
    return {
        "media": [
            {
                "id": str(r["id"]),
                "url": r["url"],
                "kind": r["kind"],
                "sort_order": r["sort_order"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.delete("/crm/media/{media_id}")
async def delete_media(
    media_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Delete a photo. RLS confines the DELETE to this tenant; media_blobs
    cascades via the 0022 FK."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "DELETE FROM property_media WHERE id = $1 RETURNING id", media_id
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media not found.")
    log.info("Media deleted: media_id=%s tenant=%s", media_id, ctx.tenant_id)
    return {"deleted": True, "id": str(media_id)}


# ---------------------------------------------------------------------------
# Contract vault — agent JWT only. Legal PDFs go to private S3 with SSE-S3 and
# are downloaded through short-lived presigned URLs, never public media routes.
# ---------------------------------------------------------------------------

def _check_contract_generation_rate(ctx: TenantContext) -> None:
    key = f"{ctx.tenant_id}:{ctx.agent_id}"
    now = time.monotonic()
    recent = [
        stamp
        for stamp in _contract_generation_timestamps[key]
        if now - stamp < _CONTRACT_RATE_WINDOW
    ]
    if len(recent) >= _CONTRACT_RATE_LIMIT:
        _contract_generation_timestamps[key] = recent
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Contract generation rate limit exceeded.",
        )
    recent.append(now)
    _contract_generation_timestamps[key] = recent

@router.post("/contracts/clients/{client_id}/assignment")
async def generate_assignment_contract(
    client_id: UUID,
    expiration_seconds: int = Query(default=3600, ge=1, le=3600),
    ctx: TenantContext = Depends(require_context),
):
    _check_contract_generation_rate(ctx)
    try:
        from ml_forge.synthetic_lawyer import generate_assignment_contract_for_client

        result = await generate_assignment_contract_for_client(
            str(client_id),
            ctx,
            expiration_seconds=expiration_seconds,
        )
    except RuntimeError:
        log.exception("Contract data service unavailable for client_id=%s", client_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Contract data service unavailable.",
        )
    except (ValueError, VaultUploadError):
        log.exception("Contract vault failed for client_id=%s tenant=%s", client_id, ctx.tenant_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Contract vault unavailable.",
        )
    except Exception:  # noqa: BLE001
        log.exception("Assignment contract generation failed for client_id=%s", client_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Assignment contract generation failed.",
        )

    if result.get("status") != "SUCCESS":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "status": result.get("status", "FATAL_ERROR"),
                "missing_variables": result.get("missing_variables", []),
                "assignment_fee_calculated": result.get("assignment_fee_calculated", 0),
            },
        )

    log.info(
        "Assignment contract vaulted: client_id=%s document_id=%s tenant=%s actor=%s",
        client_id,
        result["document_id"],
        ctx.tenant_id,
        ctx.agent_id,
    )
    return {
        "status": "SUCCESS",
        "client_id": str(client_id),
        "document_id": result["document_id"],
        "download_url": result["presigned_url"],
        "expires_in": result["expires_in"],
        "assignment_fee_calculated": result["assignment_fee_calculated"],
    }


# ---------------------------------------------------------------------------
# Public serve — no auth. The media id is a 122-bit random uuid; media_blobs is
# deliberately non-RLS (it holds only the publicly-served image bytes), so this
# reads on a plain pooled connection with no tenant context.
# ---------------------------------------------------------------------------

@router.get("/media/{media_id}")
async def serve_media(media_id: UUID):
    pool = get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Storage offline.")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content_type, bytes FROM media_blobs WHERE media_id = $1",
            media_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found.")
    return Response(
        content=bytes(row["bytes"]),
        media_type=row["content_type"],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ---------------------------------------------------------------------------
# Client-portal mutation guard — passwordless portal-session token
# (client_portal.py). Dossier sessions are deliberately read-only; keeping this
# route returns an explicit 403 to older clients without parsing an upload body.
# ---------------------------------------------------------------------------

@router.post("/portal/media", status_code=status.HTTP_201_CREATED)
async def portal_upload_media(
    authorization: Optional[str] = Header(default=None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing or malformed Authorization header."
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session.")

    if claims.get("role") != "portal_client":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a portal session token.")

    # Scoped dossier links are intentionally read-only.  Agent-authenticated
    # capture/upload routes remain available for brokerage workflows.
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "This revocable dossier link is read-only.",
    )
