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

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

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
