"""Neoh walkable-tour resolver.

ONE endpoint the frontend reads to decide which tour to show a given property and
how to label it honestly. There is no honest path from a bare address to an
interior, so the resolver only ever advertises a "walk inside" tier when real
captured media exists for that property. Tiers (highest wins):

  0  exterior   Google Photorealistic 3D Tiles — address-only, ~100% coverage
  1  photos     uploaded 2D photos (filmstrip + lightbox over the exterior)
  2  pano       360° equirectangular room-to-room teleport-walk (property_media kind='pano'/'tour')
  3  splat      full 3D Gaussian-splat free-roam walkthrough (property_media kind='splat')

The exterior tier is always available (the lead/listing always has an address),
so the resolver returns the best INTERIOR tier plus the always-on exterior flag.
RLS scopes every read to the caller's tenant.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from db.connection import tenant_tx
from tenancy import TenantContext, require_context
from reconstruction_providers import SPATIAL_AI_DISCLOSURE, get_provider
from reconstruction_worker import ReconstructionJob, enqueue

log = logging.getLogger("oracle.tour_api")

router = APIRouter(prefix="/api", tags=["tour"])

# kind → interior tier rank. 'tour' is treated as a pano-style guided tour.
_TIER_BADGE = {
    0: "Exterior 3D",
    1: "Photos + Exterior 3D",
    2: "360° Walkthrough",
    3: "Full 3D Walkthrough",
}
_TIER_NOTE = {
    0: "Photoreal exterior 3D for this address. No interior has been captured yet.",
    1: "Photoreal exterior 3D plus uploaded photos. No walkable interior captured yet.",
    2: "Walk room-to-room through 360° captures of the actual home.",
    3: "Free-roam the actual home in a photoreal 3D reconstruction.",
}


@router.get("/crm/property-tour")
async def resolve_tour(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Resolve the best available tour tier for one property (lead or listing).

    Returns the tier flags, the chosen tier + honest badge/note, and the asset
    URLs the viewer needs (splat_url for tier 3, pano_manifest_url for tier 2).
    The frontend only offers a "Step inside" affordance when best_tier >= 2."""
    if lead_id is None and listing_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id."
        )

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, url, sort_order
              FROM property_media
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             ORDER BY sort_order ASC, created_at ASC
            """,
            lead_id,
            listing_id,
        )

    photos = [r for r in rows if r["kind"] == "photo"]
    panos = [r for r in rows if r["kind"] in ("pano", "tour")]
    splats = [r for r in rows if r["kind"] == "splat"]

    has_photos = len(photos) > 0
    has_pano = len(panos) > 0
    has_splat = len(splats) > 0

    # Exterior (tier 0) is always available — the property always has an address.
    if has_splat:
        best = 3
    elif has_pano:
        best = 2
    elif has_photos:
        best = 1
    else:
        best = 0

    return {
        "best_tier": best,
        "badge": _TIER_BADGE[best],
        "honest_note": _TIER_NOTE[best],
        "walkable_interior": best >= 2,  # the only tiers you can truly walk inside
        "disclosure": SPATIAL_AI_DISCLOSURE if best >= 2 else None,
        "tiers": {
            "exterior": True,
            "photos": has_photos,
            "pano": has_pano,
            "splat": has_splat,
        },
        "splat_url": splats[0]["url"] if has_splat else None,
        "pano_manifest_url": panos[0]["url"] if has_pano else None,
        "photo_count": len(photos),
    }


# ---------------------------------------------------------------------------
# Reconstruction jobs — enqueue a capture→splat job (tier 3) + poll its status.
# Long jobs run in the reconstruction worker pool (reconstruction_worker.py);
# this is the 202-accept-then-poll surface.
# ---------------------------------------------------------------------------
@router.post("/crm/reconstruction-jobs", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_reconstruction(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Queue a Gaussian-splat reconstruction for one property. Returns 202 +
    job_id; poll GET /crm/reconstruction-jobs/{id}. 503 if the configured
    provider isn't available (no GPU / no key) — never silently fakes a result."""
    if lead_id is None and listing_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id.")
    ok, why = get_provider().available()
    if not ok:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Reconstruction provider unavailable: {why}")

    async with tenant_tx(ctx) as conn:
        # Validate the target exists in this tenant BEFORE inserting. The
        # reconstruction_jobs FKs (lead_id->leads, listing_id->listings) would
        # otherwise raise ForeignKeyViolation -> unhandled 500 on a bogus or
        # cross-tenant id. RLS scopes these SELECTs to the caller's tenant.
        if lead_id is not None and not await conn.fetchval("SELECT 1 FROM leads WHERE id = $1", lead_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
        if listing_id is not None and not await conn.fetchval("SELECT 1 FROM listings WHERE id = $1", listing_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")
        row = await conn.fetchrow(
            """
            INSERT INTO reconstruction_jobs (tenant_id, lead_id, listing_id, status, created_by)
            VALUES ($1, $2, $3, 'queued', $4)
            RETURNING id
            """,
            ctx.tenant_id, lead_id, listing_id, ctx.agent_id,
        )
    job_id = str(row["id"])
    try:
        enqueue(ReconstructionJob(
            ctx=ctx, job_id=job_id,
            lead_id=str(lead_id) if lead_id else None,
            listing_id=str(listing_id) if listing_id else None,
        ))
    except asyncio.QueueFull:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                "UPDATE reconstruction_jobs SET status='failed', error='queue full' WHERE id=$1", row["id"]
            )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Reconstruction queue is full — try again shortly.")
    return {"job_id": job_id, "status": "queued"}


@router.get("/crm/reconstruction-jobs/{job_id}")
async def reconstruction_job_status(
    job_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Poll a reconstruction job (RLS-scoped)."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT id, status, provider, progress, media_id, error FROM reconstruction_jobs WHERE id = $1",
            job_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return {
        "job_id": str(row["id"]),
        "status": row["status"],
        "provider": row["provider"],
        "progress": row["progress"],
        "media_id": str(row["media_id"]) if row["media_id"] else None,
        "error": row["error"],
    }
