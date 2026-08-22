"""Video Marketing Studio API — tenant-scoped job lifecycle for Sora 2 reels.

The heavy lifting lives in video_studio.py (provider client, script drafting,
PyAV stitching, durable worker). This router is a thin authenticated surface:

  POST   /api/video-studio/jobs           create a job        → 202 (queued)
  GET    /api/video-studio/jobs           list tenant jobs    (newest first)
  GET    /api/video-studio/jobs/{id}      job detail
  POST   /api/video-studio/jobs/{id}/script   regenerate script
  POST   /api/video-studio/jobs/{id}/cancel   cancel a queued job
  DELETE /api/video-studio/jobs/{id}      delete a job record

Every route is gated on ``require_context`` (tenant JWT), writes are audited
(VIDEO_STUDIO), and the daily quota is enforced at submission AND in the worker
so a burst of multi-clip jobs cannot overspend the tenant's allowance.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from audit_ledger import AuditCategory, ledger
from db.connection import tenant_tx
from tenancy import TenantContext, require_context

import video_providers
import video_studio as studio

log = logging.getLogger("oracle.video_studio_api")

router = APIRouter(prefix="/api/video-studio", tags=["video-studio"])

_VIDEO_KIND = Literal["text", "image"]
_VIDEO_SIZE = Literal["720x1280", "1280x720"]


class VideoJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: _VIDEO_KIND
    size: _VIDEO_SIZE
    property: dict[str, Any] = Field(default_factory=dict, max_length=60)
    images: list[uuid.UUID] = Field(default_factory=list, max_length=4)
    voiceover: bool = False
    # Burned-in narration. Default on — social reels autoplay muted, so a video
    # whose message lives only in the audio track communicates nothing.
    captions: bool = True
    script: Optional[str] = Field(default=None, max_length=4000)
    lead_id: Optional[uuid.UUID] = None
    listing_id: Optional[uuid.UUID] = None

    @field_validator("images")
    @classmethod
    def limit_images(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) > studio.MAX_IMAGES:
            raise ValueError(f"at most {studio.MAX_IMAGES} images per job")
        if value and len(set(value)) != len(value):
            raise ValueError("image ids must be unique")
        return value

    @field_validator("script")
    @classmethod
    def strip_script(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("lead_id", "listing_id")
    @classmethod
    def none_empty(cls, value: Optional[uuid.UUID]) -> Optional[uuid.UUID]:
        return value or None


class VideoJobUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    script: str = Field(min_length=1, max_length=4000)


class VideoJobScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property: dict[str, Any] = Field(default_factory=dict, max_length=60)


def _row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif key in {"source", "provider_job_ids"}:
            result[key] = json.loads(value) if isinstance(value, str) else value
    return result


def _require_property_anchor(body: VideoJobCreate) -> None:
    if not body.lead_id and not body.listing_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "lead_id or listing_id is required (the finished video is attached to the property).",
        )


async def _verify_images_owned(ctx: TenantContext, image_ids: list[uuid.UUID]) -> None:
    """Reject jobs whose photo ids do not belong to this tenant."""
    if not image_ids:
        return
    async with tenant_tx(ctx) as conn:
        count = await conn.fetchval(
            """
            SELECT count(*) FROM property_media
             WHERE id = ANY($1::uuid[]) AND kind = 'photo'
            """,
            image_ids,
        )
    if count != len(image_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "one or more referenced photos do not exist in this workspace",
        )


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_video_job(
    body: VideoJobCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Create a video generation job. 202 Accepted — the durable worker picks it
    up; poll GET /jobs/{id} for progress."""
    if body.kind == "image" and not body.images:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "image jobs require at least one photo",
        )
    if body.voiceover and body.script:
        # Sora 2 native audio carries the narration; a voiceover toggle only
        # refines the prompt. The worker adds an audio direction when enabled.
        pass
    _require_property_anchor(body)
    await _verify_images_owned(ctx, body.images)

    # Refuse BEFORE quota is reserved. Previously an unconfigured provider was
    # discovered inside the worker, so the user spent their daily seconds and
    # then watched the reel fail — the failure looked like a bad generation
    # rather than a deployment that was never wired. Mirrors tour_api.py:310,
    # which forwards the provider's reason verbatim.
    ready, why = video_providers.get_provider().available()
    if not ready:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Video provider unavailable: {why}",
        )

    async with tenant_tx(ctx) as conn:
        consumed = await studio._quota_consumed_today(ctx, conn)
        clip_seconds = studio.CLIP_SECONDS
        estimated = max(1, len(body.images)) * clip_seconds
        if consumed + estimated > studio.DAILY_QUOTA_SECONDS:
            remaining = max(0.0, studio.DAILY_QUOTA_SECONDS - consumed)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "VIDEO_QUOTA_EXCEEDED",
                    "message": (
                        f"Daily video quota exceeded — this job needs {estimated}s, "
                        f"{remaining:.0f}s remaining."
                    ),
                    "daily_quota_seconds": studio.DAILY_QUOTA_SECONDS,
                    "consumed_seconds": round(consumed, 2),
                },
            )

        source = {
            "property": body.property or {},
            "images": [str(image_id) for image_id in body.images],
            "voiceover": body.voiceover,
            "captions": body.captions,
            "lead_id": str(body.lead_id) if body.lead_id else None,
            "listing_id": str(body.listing_id) if body.listing_id else None,
        }
        row = await conn.fetchrow(
            """
            INSERT INTO video_studio_jobs
                (tenant_id, kind, source, size, script, status)
            VALUES ($1, $2, $3::jsonb, $4, $5, 'queued')
            RETURNING *
            """,
            ctx.tenant_id,
            body.kind,
            json.dumps(source, separators=(",", ":")),
            body.size,
            body.script or "",
        )

    log.info(
        "Video studio job created: job=%s tenant=%s kind=%s images=%d voiceover=%s",
        row["id"], ctx.tenant_id, body.kind, len(body.images), body.voiceover,
    )
    return _row_dict(row)


@router.post("/script")
async def draft_script_for_property(
    body: VideoJobScriptRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Draft a reel script from property data before creating a job, so the
    operator can review/edit it ahead of generation."""
    script = await studio.draft_script(body.property or {})
    await ledger.record(
        category=AuditCategory.VIDEO_STUDIO,
        action="video-studio script draft",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
    )
    return {"script": script}


@router.get("/jobs")
async def list_video_jobs(
    ctx: TenantContext = Depends(require_context),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List the tenant's jobs, newest first."""
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM video_studio_jobs
             WHERE tenant_id = $1::uuid
             ORDER BY created_at DESC
             LIMIT $2
            """,
            ctx.tenant_id,
            limit,
        )
    return {"jobs": [_row_dict(row) for row in rows]}


@router.get("/jobs/{job_id}")
async def get_video_job(
    job_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM video_studio_jobs WHERE id = $1::uuid", job_id
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video job not found.")
    return _row_dict(row)


@router.post("/jobs/{job_id}/script")
async def regenerate_script(
    job_id: uuid.UUID,
    body: VideoJobScriptRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Draft a fresh script for the job's property data (does not re-run
    generation; a new job is required to re-render)."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status FROM video_studio_jobs
             WHERE id = $1::uuid AND tenant_id = $2::uuid
            """,
            job_id, ctx.tenant_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video job not found.")
    if row["status"] not in {"queued", "failed", "cancelled"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "script can only be regenerated while the job is queued, failed, or cancelled.",
        )
    script = await studio.draft_script(body.property or {})
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE video_studio_jobs SET script = $1 WHERE id = $2::uuid",
            script, job_id,
        )
    await ledger.record(
        category=AuditCategory.VIDEO_STUDIO,
        action="video-studio script regenerate",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(job_id),
    )
    return {"job_id": str(job_id), "script": script}


@router.post("/jobs/{job_id}/cancel")
async def cancel_video_job(
    job_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Cancel a job that has not started generating."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE video_studio_jobs
               SET status = 'cancelled'
             WHERE id = $1::uuid
               AND tenant_id = $2::uuid
               AND status IN ('queued', 'scripting')
             RETURNING id, status
            """,
            job_id, ctx.tenant_id,
        )
    if row is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job is not cancellable (only queued/scripting jobs can be cancelled).",
        )
    await ledger.record(
        category=AuditCategory.VIDEO_STUDIO,
        action="video-studio job cancel",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(job_id),
    )
    return _row_dict(row)


@router.delete("/jobs/{job_id}")
async def delete_video_job(
    job_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Remove a job record. Completed videos referenced by other tenants'
    property_media rows are never deleted; a ready job keeps its media."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM video_studio_jobs
             WHERE id = $1::uuid AND tenant_id = $2::uuid
             RETURNING id
            """,
            job_id, ctx.tenant_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video job not found.")
    await ledger.record(
        category=AuditCategory.VIDEO_STUDIO,
        action="video-studio job delete",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(job_id),
    )
    return {"deleted": str(job_id)}


@router.get("/config")
async def video_studio_config(ctx: TenantContext = Depends(require_context)):
    """Feature/limits surface for the studio UI (no secrets)."""
    async with tenant_tx(ctx) as conn:
        consumed = await studio._quota_consumed_today(ctx, conn)
    provider = video_providers.get_provider()
    ready, why = provider.available()
    return {
        "max_images": studio.MAX_IMAGES,
        "clip_seconds": studio.CLIP_SECONDS,
        "daily_quota_seconds": studio.DAILY_QUOTA_SECONDS,
        "consumed_seconds": round(consumed, 2),
        "allowed_sizes": sorted(studio.ALLOWED_SIZES),
        # `deployment` used to leak the Sora deployment name here and the UI never
        # read it. What the UI actually needs is whether it can generate at all,
        # and why not — so it can say so before a user fills in a script and
        # spends quota on a 503.
        "provider": provider.name,
        "provider_ready": ready,
        "provider_reason": why,
    }
