"""Neoh 2D image upload + authenticated delivery API.

Agents (in-app, JWT) and homeowners (passwordless client portal) attach property
photos to a lead or listing. The metadata row lives in the existing tenant-scoped
`property_media` table; delivery always joins through that row inside
``tenant_tx`` so possession of a media UUID is never sufficient to read another
tenant's image.

Where the bytes live depends on the deployment, and both shapes are permanent:

  * object storage (`property_media.s3_key`) whenever ORACLE_STORAGE_BACKEND is
    configured — the default for anything real. See media_storage.
  * `media_blobs.bytes` otherwise, so a bare `docker compose up` needs no cloud
    account, and for every row written before storage was configured.

The blob table was originally the only option, on the reasoning that the
container's bind mount was not reliably writable and per-tenant RLS made a
shared directory awkward. Both are still true of a *local disk*; neither applies
to object storage, and keeping full-size images in a bytea column made the
primary database the image server — every thumbnail view a row read competing
for the same connection pool, every byte replicated and backed up. Storage is
now preferred wherever it exists; nothing migrates existing blobs, and
media_storage.load_media_bytes reads either.

Endpoints
  POST   /api/crm/leads/{lead_id}/media        (agent JWT)  — upload photo(s)
  POST   /api/crm/listings/{listing_id}/media  (agent JWT)  — upload photo(s)
  GET    /api/crm/media?lead_id=&listing_id=    (agent JWT)  — list photos
  DELETE /api/crm/media/{media_id}             (agent JWT)  — remove a photo
  POST   /api/portal/media                     (portal token) — explicit read-only rejection
  GET    /api/media/{media_id}                 (agent JWT)  — stream the image
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from collections import defaultdict
from typing import Any, List, Optional
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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import ALGORITHM, SECRET_KEY
from audit_ledger import AuditCategory, ledger
from billing_usage import record_usage
from contract_vault import SovereignVault, VaultUploadError
import media_storage
from db.connection import tenant_tx
from tenancy import Role, TenantContext, require_context

log = logging.getLogger("oracle.media_api")

router = APIRouter(prefix="/api", tags=["media"])

# 12 MB hard cap per image.
MAX_BYTES = 12 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 30
_CONTRACT_RATE_LIMIT = int(os.getenv("CONTRACT_GENERATION_RATE_LIMIT", "10"))
_CONTRACT_RATE_WINDOW = 60 * 60.0
_contract_generation_timestamps: dict[str, list[float]] = defaultdict(list)
_SYNTHESIS_FINANCIAL_FIELDS = {
    "wholesale_buy_price",
    "investor_buy_price",
    "earnest_money_deposit",
    "purchase_price",
    "assignment_fee",
}


class ContractSynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_id: UUID
    doc_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    state: str = Field(pattern=r"^[A-Za-z]{2}$")
    financial_override: dict[str, float] = Field(default_factory=dict)

    @field_validator("state")
    @classmethod
    def normalize_state(cls, value: str) -> str:
        return value.upper()

    @field_validator("financial_override")
    @classmethod
    def validate_financial_override(cls, value: dict[str, float]) -> dict[str, float]:
        if len(value) > len(_SYNTHESIS_FINANCIAL_FIELDS):
            raise ValueError("too many financial override fields")
        unsupported = sorted(set(value) - _SYNTHESIS_FINANCIAL_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported financial override fields: {unsupported}")
        for key, amount in value.items():
            if amount < 0 or amount > 1_000_000_000:
                raise ValueError(f"{key} is outside the supported range")
        return value

# Media kinds permitted by property_media's chk_media_kind constraint (0012). The
# photo-upload endpoints below only produce 'photo'; the walkable-tour pipeline
# (capture wizard / reconstruction worker) writes 'pano'/'splat'/'tour' rows that
# the tour resolver (tour_api.py) reads to pick the highest available tier.
_ALLOWED_KINDS = {"photo", "pano", "splat", "tour", "floorplan", "document"}

# `private` is load-bearing: it lets the requesting user's browser cache the
# image but forbids any shared cache (proxy, CDN) from holding a tenant-scoped
# asset it could hand to somebody else. `immutable` is honest here because a
# media id is never reused — this route was previously `no-store`, which meant
# every thumbnail re-read the full file from the database on every render.
_MEDIA_CACHE_CONTROL = "private, max-age=86400, immutable"

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
        # url is NOT NULL and points at the authenticated serve path, which embeds
        # the row id. Generate the id client-side so the url is known up front and
        # the whole thing is a single INSERT (a writable CTE can't UPDATE the row
        # its own INSERT just created — same-statement snapshot).
        new_id = uuid4()
        # Durable storage when configured, blob otherwise. Done before the row
        # is written because s3_key is part of the INSERT — same ordering the
        # video path has always used.
        s3_key = await media_storage.put_media_bytes(
            data, content_type, str(tenant_id), kind=kind
        )
        row = await conn.fetchrow(
            """
            INSERT INTO property_media
                (id, tenant_id, lead_id, listing_id, kind, url, sort_order,
                 s3_key, content_type)
            VALUES ($1, $2, $3, $4, $7, $5, $6, $8, $9)
            RETURNING id, url, kind, sort_order, created_at
            """,
            new_id,
            tenant_id,
            lead_id,
            listing_id,
            f"/api/media/{new_id}",
            base,
            kind,
            s3_key,
            content_type,
        )
        if s3_key is None:
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
        # Capture volume is a tracked metric, not just a gallery side effect.
        # The comparable outcome in this category (Matterport → CoStar, $1.6B)
        # priced the accumulated capture corpus rather than the viewer, so the
        # rate at which captures land is a number worth having a history of.
        # Keyed on the media id, which is unique per capture — a retry of this
        # request generates a new id and a genuinely new capture.
        await record_usage(
            TenantContext(agent_id="media-capture", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN),
            metric="media_capture",
            quantity=1,
            idempotency_key=f"media-capture:{row['id']}",
            conn=conn,
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


async def _mark_synthesis_failed(
    ctx: TenantContext,
    artifact_id: UUID,
    failure_code: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Move a started artifact to a terminal state without exposing internals."""
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            UPDATE contract_synthesis_artifacts
               SET status='failed',failure_code=$2,
                   metadata=metadata || $3::jsonb,updated_at=now()
             WHERE id=$1
            """,
            artifact_id,
            failure_code,
            json.dumps(metadata or {}),
        )


@router.post("/contracts/synthesize")
async def synthesize_contract(
    body: ContractSynthesisRequest,
    expiration_seconds: int = Query(default=3600, ge=60, le=3600),
    ctx: TenantContext = Depends(require_context),
):
    """Render one attorney-approved template and store it in the private vault."""
    _check_contract_generation_rate(ctx)
    from ml_forge.synthetic_lawyer import (
        fetch_assignment_transaction_for_client,
        render_approved_contract_template,
        template_sha256,
        write_contract_pdf,
    )

    async with tenant_tx(ctx) as conn:
        template = await conn.fetchrow(
            """
            SELECT id,template_key,version,document_type,jurisdiction,
                   body_template,required_fields,template_sha256,
                   attorney_reviewed_by,attorney_reviewed_at
              FROM contract_templates
             WHERE tenant_id=$1::uuid
               AND status='approved'
               AND jurisdiction IN ($2,'US-GENERIC')
               AND (template_key=$3 OR id::text=$3)
             ORDER BY CASE WHEN jurisdiction=$2 THEN 0 ELSE 1 END,
                      updated_at DESC
             LIMIT 1
            """,
            ctx.tenant_id,
            body.state,
            body.doc_id,
        )
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This document has no attorney-approved executable template.",
            )

        artifact_id = uuid4()
        document_id = f"{str(template['template_key'])[:96]}-{artifact_id.hex[:12]}"
        await conn.execute(
            """
            INSERT INTO contract_synthesis_artifacts (
                id,tenant_id,client_id,doc_id,state_code,template_id,
                template_sha256,status,created_by
            ) VALUES ($1,$2::uuid,$3,$4,$5,$6,$7,'generating',$8)
            """,
            artifact_id,
            ctx.tenant_id,
            body.client_id,
            document_id,
            body.state,
            template["id"],
            template["template_sha256"],
            ctx.agent_id,
        )

    if template_sha256(template["body_template"]) != template["template_sha256"]:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                UPDATE contract_synthesis_artifacts
                   SET status='failed',failure_code='template_checksum_mismatch',
                       updated_at=now()
                 WHERE id=$1
                """,
                artifact_id,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approved template checksum mismatch.",
        )

    try:
        transaction = await fetch_assignment_transaction_for_client(str(body.client_id), ctx)
        transaction.update(body.financial_override)
        rendered = render_approved_contract_template(
            document_type=template["document_type"],
            body_template=template["body_template"],
            required_fields=list(template["required_fields"]),
            transaction_data=transaction,
        )
    except Exception as exc:  # database/template failures must not strand "generating"
        await _mark_synthesis_failed(ctx, artifact_id, "contract_context_unavailable")
        log.exception("Contract synthesis context failed artifact=%s", artifact_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contract data service unavailable.",
        ) from exc
    if rendered.get("status") == "FATAL_ERROR":
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                UPDATE contract_synthesis_artifacts
                   SET status='failed',failure_code='missing_required_fields',
                       metadata=$2::jsonb,updated_at=now()
                 WHERE id=$1
                """,
                artifact_id,
                json.dumps({"missing_variables": rendered.get("missing_variables", [])}),
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "status": "FATAL_ERROR",
                "missing_variables": rendered.get("missing_variables", []),
            },
        )

    try:
        with tempfile.TemporaryDirectory(prefix="neoh_synthesis_") as directory:
            pdf_path = Path(directory) / f"{document_id}.pdf"
            write_contract_pdf(rendered["final_contract_text"], pdf_path)
            pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            try:
                vaulted = SovereignVault().vault_pdf(
                    pdf_path,
                    client_id=str(body.client_id),
                    document_id=document_id,
                    expiration_seconds=expiration_seconds,
                    tenant_id=ctx.tenant_id,
                )
                vault_result = vaulted.to_dict()
                synthesis_status = "ENCRYPTED_IN_VAULT"
            except (VaultUploadError, ValueError) as exc:
                import config

                if not config.IS_DEV:
                    await _mark_synthesis_failed(ctx, artifact_id, "vault_unavailable")
                    log.exception("Contract synthesis vault upload failed artifact=%s", artifact_id)
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Contract vault unavailable.",
                    ) from exc
                vault_result = {
                    "bucket": None,
                    "s3_key": None,
                    "presigned_url": None,
                    "expires_in": 0,
                }
                synthesis_status = "LOCAL_PREVIEW_ONLY"
    except HTTPException:
        raise
    except Exception as exc:
        await _mark_synthesis_failed(ctx, artifact_id, "pdf_generation_failed")
        log.exception("Contract PDF generation failed artifact=%s", artifact_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contract generation unavailable.",
        ) from exc

    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            UPDATE contract_synthesis_artifacts
               SET status=$2,pdf_sha256=$3,s3_key=$4,
                   encryption=CASE WHEN $4::text IS NULL THEN NULL ELSE 'AES256' END,
                   expires_at=CASE WHEN $5::int > 0
                       THEN now()+make_interval(secs=>$5) ELSE NULL END,
                   metadata=$6::jsonb,updated_at=now()
             WHERE id=$1
            """,
            artifact_id,
            synthesis_status.lower(),
            pdf_sha256,
            vault_result.get("s3_key"),
            int(vault_result.get("expires_in") or 0),
            json.dumps(
                {
                    "document_type": template["document_type"],
                    "template_key": template["template_key"],
                    "template_version": template["version"],
                    "professional_review_required": True,
                }
            ),
        )

    await ledger.record(
        category=AuditCategory.LEGAL_CONTRACT,
        action="contract_synthesized_encrypted",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(artifact_id),
        metadata={
            "client_id": str(body.client_id),
            "document_type": template["document_type"],
            "template_sha256": template["template_sha256"],
            "pdf_sha256": pdf_sha256,
            "status": synthesis_status,
        },
    )
    return {
        "status": synthesis_status,
        "client_id": str(body.client_id),
        "document_id": str(artifact_id),
        "doc_id": document_id,
        "document_type": template["document_type"],
        "state": body.state,
        "template": {
            "id": str(template["id"]),
            "key": template["template_key"],
            "version": template["version"],
            "sha256": template["template_sha256"],
        },
        "pdf_sha256": pdf_sha256,
        "encryption": "AES256" if vault_result.get("s3_key") else None,
        "download_url": vault_result.get("presigned_url"),
        "expires_in": vault_result.get("expires_in"),
        "professional_review_required": True,
    }


@router.get("/contracts/synthesis-artifacts")
async def list_contract_synthesis_artifacts(
    client_id: UUID,
    state_code: Optional[str] = Query(default=None, pattern=r"^[A-Za-z]{2}$"),
    ctx: TenantContext = Depends(require_context),
):
    """List generated document state for one tenant-owned client."""
    normalized_state = state_code.upper() if state_code else None
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,client_id,doc_id,state_code,status,pdf_sha256,encryption,
                   expires_at,metadata,created_at,updated_at
              FROM contract_synthesis_artifacts
             WHERE client_id=$1
               AND ($2::char(2) IS NULL OR state_code=$2)
             ORDER BY created_at DESC
             LIMIT 200
            """,
            client_id,
            normalized_state,
        )
    return {
        "artifacts": [
            {
                "id": str(row["id"]),
                "client_id": str(row["client_id"]),
                "doc_id": row["doc_id"],
                "state": str(row["state_code"]).strip(),
                "status": str(row["status"]).upper(),
                "pdf_sha256": row["pdf_sha256"],
                "encryption": row["encryption"],
                "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                "template_key": (row["metadata"] or {}).get("template_key"),
                "document_type": (row["metadata"] or {}).get("document_type"),
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/contracts/synthesis-artifacts/{artifact_id}/download")
async def download_contract_synthesis_artifact(
    artifact_id: UUID,
    expiration_seconds: int = Query(default=3600, ge=60, le=3600),
    ctx: TenantContext = Depends(require_context),
):
    """Issue a fresh one-hour URL after re-checking tenant ownership under RLS."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT id,client_id,doc_id,s3_key,status
              FROM contract_synthesis_artifacts
             WHERE id=$1
            """,
            artifact_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contract artifact not found.")
    if row["status"] != "encrypted_in_vault" or not row["s3_key"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This contract is not available in the encrypted vault.",
        )

    try:
        vault = SovereignVault()
        expected_key = vault.s3_key(
            str(row["client_id"]),
            row["doc_id"],
            tenant_id=ctx.tenant_id,
        )
        if expected_key != row["s3_key"]:
            log.error("Contract vault key mismatch artifact=%s", artifact_id)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Contract vault metadata failed integrity validation.",
            )
        download_url = vault.generate_expiring_link(
            str(row["client_id"]),
            row["doc_id"],
            expiration_seconds,
            tenant_id=ctx.tenant_id,
        )
    except HTTPException:
        raise
    except Exception as exc:  # credentials/configuration errors stay private
        log.exception("Contract vault presign failed artifact=%s", artifact_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Contract vault unavailable.",
        ) from exc
    if not download_url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Contract vault unavailable.",
        )

    await ledger.record(
        category=AuditCategory.LEGAL_CONTRACT,
        action="contract_download_link_issued",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(artifact_id),
        metadata={"expires_in": expiration_seconds},
    )
    return {
        "artifact_id": str(artifact_id),
        "download_url": download_url,
        "expires_in": expiration_seconds,
    }


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
# Authenticated delivery. ``media_blobs`` intentionally has no tenant column, so
# every read must join through ``property_media`` while tenant RLS is active.
# The frontend fetches this route with its Bearer token and renders a short-lived
# object URL; browsers cannot attach Authorization headers to a plain <img src>.
# ---------------------------------------------------------------------------

@router.get("/media/{media_id}")
async def serve_media(
    media_id: UUID,
    ctx: TenantContext = Depends(require_context),
    if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
):
    # A media row's bytes never change — a replacement upload gets a new id — so
    # the id alone is a sound strong validator. Answering a repeat view with 304
    # is what stops a gallery of eight photos costing eight full reads every time
    # someone opens the property.
    etag = f'"{media_id}"'
    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        # Still inside the authenticated route: a 304 confirms nothing to a
        # caller who could not already read the row.
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={
            "ETag": etag,
            "Cache-Control": _MEDIA_CACHE_CONTROL,
        })

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT pm.kind, pm.s3_key, pm.content_type AS media_content_type,
                   mb.content_type, mb.bytes
              FROM property_media AS pm
              LEFT JOIN media_blobs AS mb ON mb.media_id = pm.id
             WHERE pm.id = $1
            """,
            media_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found.")

    content = row["bytes"]
    content_type = row["content_type"]
    if content is None:
        # Not every media row is blob-backed: migration 0066 forbids a video row
        # from carrying a blob at all (CHECK kind <> 'video' OR s3_key IS NOT
        # NULL), so those bytes live in object storage. A plain JOIN here used to
        # drop them and 404 every generated video.
        if not row["s3_key"]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found.")
        import object_storage

        try:
            content = await asyncio.to_thread(object_storage.get_bytes, row["s3_key"])
        except object_storage.StorageError as exc:
            log.warning("Object-storage media unreadable: id=%s %s", media_id, exc)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Media could not be read from storage."
            ) from exc
        # Use the type recorded at upload. Hardcoding video/mp4 here served an
        # iPhone .mov (sniffed video/quicktime) or a .webm as mp4, and under
        # X-Content-Type-Options: nosniff the browser refuses to re-sniff — the
        # player just goes black with no error. Fall back only when the row
        # predates migration 0068, which added the column.
        content_type = (
            row["media_content_type"]
            or ("video/mp4" if row["kind"] == "video" else "application/octet-stream")
        )

    return Response(
        content=bytes(content),
        media_type=content_type,
        headers={
            "Cache-Control": _MEDIA_CACHE_CONTROL,
            "ETag": etag,
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
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
