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

# Every note above tier 0 says "the actual home". A splat that was generated
# rather than captured cannot support that, so it never sets the tier — it is
# surfaced alongside whatever the real media supports, labelled for what it is.
# It stays viewable on purpose: it is the only way to exercise the viewer,
# controls and bounds clamp without a GPU.
_DEMO_BADGE = "Demo space (not this home)"
_DEMO_NOTE = (
    "This walkthrough is a generated demo space, not a capture of this property. "
    "It is here to preview how a tour behaves; nothing in it depicts the real home."
)


@router.get("/crm/property-tour")
async def resolve_tour(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Resolve the best available tour tier for one property (lead or listing).

    Returns the tier flags, the chosen tier + honest badge/note, and the asset
    URLs the viewer needs (splat_url for tier 3, pano_scenes for tier 2).
    The frontend only offers a "Step inside" affordance when best_tier >= 2."""
    if lead_id is None and listing_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id."
        )

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, url, sort_order,
                   COALESCE(provenance, 'captured') AS provenance
              FROM property_media
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             ORDER BY sort_order ASC, created_at ASC
            """,
            lead_id,
            listing_id,
        )
        # Vantage points for a 360 walkthrough. Ordered the way the agent
        # captured them, which is the fallback walk sequence when no explicit
        # adjacency has been recorded.
        scene_rows = await conn.fetch(
            """
            SELECT s.id, s.media_id, s.floor_index, s.label, s.sort_order,
                   s.position_x, s.position_y, s.position_z, s.heading_deg,
                   s.neighbour_ids, m.url
              FROM property_pano_scenes AS s
              JOIN property_media       AS m ON m.id = s.media_id
             WHERE (($1::uuid IS NOT NULL AND s.lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND s.listing_id = $2))
             ORDER BY s.floor_index ASC, s.sort_order ASC, s.created_at ASC
            """,
            lead_id,
            listing_id,
        )
        # The saved floor plan (if any) supplies the tour's floor navigation:
        # level list + storey height → per-floor camera heights in the viewer.
        plan_row = await conn.fetchrow(
            """
            SELECT document
              FROM property_floorplans
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             LIMIT 1
            """,
            lead_id,
            listing_id,
        )

    floors = _floors_from_plan(plan_row["document"] if plan_row else None)

    def _captured(row) -> bool:
        return row["provenance"] == "captured"

    photos = [r for r in rows if r["kind"] == "photo"]
    pano_media = [r for r in rows if r["kind"] in ("pano", "tour")]
    all_splats = [r for r in rows if r["kind"] == "splat"]

    # Only a captured splat is evidence about this property. A synthetic one is
    # still returned below, but it does not earn a tier.
    splats = [r for r in all_splats if _captured(r)]
    demo_splats = [r for r in all_splats if not _captured(r)]

    scenes = _pano_scenes(scene_rows)

    # Every writer creates the scene alongside the media, so a pano image with
    # no scene row means something deleted one or wrote media directly. Say so
    # rather than silently dropping a capture the agent paid to take.
    if pano_media and not scenes:
        log.warning(
            "Pano media with no scene rows (lead=%s listing=%s, %d image(s)) — "
            "these will not appear in the walkthrough.",
            lead_id, listing_id, len(pano_media),
        )

    has_photos = len(photos) > 0
    # One 360° image is a view, not a walkthrough. Tier 2 means you can move
    # between vantage points, so it needs at least two of them — otherwise the
    # badge promises "walk room-to-room" over a single fixed viewpoint.
    has_pano = len(scenes) >= 2
    has_splat = len(splats) > 0
    has_demo_splat = len(demo_splats) > 0

    # Exterior (tier 0) is always available — the property always has an address.
    if has_splat:
        best = 3
    elif has_pano:
        best = 2
    elif has_photos or scenes:
        # A lone 360 still shows the room; it just does not earn tier 2.
        best = 1
    else:
        best = 0

    # A demo splat is walkable, so the viewer can open — but it is not a
    # walkable interior *of this home*, which is what the flag means to callers.
    is_demo = has_demo_splat and not has_splat

    return {
        "best_tier": best,
        "badge": _DEMO_BADGE if is_demo else _TIER_BADGE[best],
        "honest_note": _DEMO_NOTE if is_demo else _TIER_NOTE[best],
        "walkable_interior": best >= 2,  # the only tiers you can truly walk inside
        # True only when the walkable asset depicts this address. The viewer
        # renders a persistent badge when it is False.
        "is_this_property": not is_demo,
        "disclosure": SPATIAL_AI_DISCLOSURE if (best >= 2 or is_demo) else None,
        "tiers": {
            "exterior": True,
            "photos": has_photos,
            "pano": has_pano,
            "splat": has_splat,
        },
        # Falls back to the demo asset so the viewer still has something to
        # open; `is_this_property` is what tells the UI how to label it.
        "splat_url": (
            splats[0]["url"] if has_splat
            else demo_splats[0]["url"] if has_demo_splat
            else None
        ),
        # The ordered scene graph itself, not a URL to one image. `panos[0].url`
        # used to be returned under this name, which gave the viewer a single
        # photo and called it a manifest.
        "pano_scenes": scenes,
        "pano_scene_count": len(scenes),
        "photo_count": len(photos),
        "floors": floors,
    }


def _pano_scenes(rows) -> list[dict]:
    """Ordered 360° vantage points, with adjacency resolved to scene ids.

    `neighbour_ids` is authoritative when an agent has recorded links. When it
    is empty the scenes fall back to capture order within a floor — the walk is
    then a sequence rather than a graph, which is what an ordered upload of
    360s actually is. Nothing here invents a spatial relationship: a scene with
    no recorded position keeps `position: null`, and the viewer places it by
    order instead of pretending to know where it sits.
    """
    scenes = [
        {
            "scene_id": str(r["id"]),
            "media_id": str(r["media_id"]),
            "url": r["url"],
            "floor_index": int(r["floor_index"] or 0),
            "label": r["label"] or "",
            "position": (
                {"x": r["position_x"], "y": r["position_y"], "z": r["position_z"]}
                if r["position_x"] is not None
                and r["position_y"] is not None
                and r["position_z"] is not None
                else None
            ),
            "heading_deg": r["heading_deg"],
            "neighbours": [str(n) for n in (r["neighbour_ids"] or [])],
        }
        for r in rows
    ]

    known = {s["scene_id"] for s in scenes}
    by_floor: dict[int, list[dict]] = {}
    for scene in scenes:
        # Drop links to scenes that are gone (a deleted media row cascades).
        scene["neighbours"] = [n for n in scene["neighbours"] if n in known]
        by_floor.setdefault(scene["floor_index"], []).append(scene)

    # Sequential fallback, per floor, only where nothing was recorded.
    for floor_scenes in by_floor.values():
        for index, scene in enumerate(floor_scenes):
            if scene["neighbours"]:
                continue
            adjacent = []
            if index > 0:
                adjacent.append(floor_scenes[index - 1]["scene_id"])
            if index + 1 < len(floor_scenes):
                adjacent.append(floor_scenes[index + 1]["scene_id"])
            scene["neighbours"] = adjacent

    return scenes


def _floors_from_plan(document) -> list[dict]:
    """Viewer floor list from a saved FloorplanDocument, or [] when none exists.

    y is each level's floor plane in metres: index × storey height, where storey
    height is the median wall height in the plan (walls carry it) falling back
    to 2.5 m. An empty list simply hides the viewer's floor navigation — it must
    never invent storeys for a plan nobody drew."""
    import json as _json

    if not document:
        return []
    if isinstance(document, str):
        try:
            document = _json.loads(document)
        except ValueError:
            return []

    levels = document.get("levels") or []
    if not levels:
        return []

    heights = sorted(
        wall.get("height") for wall in document.get("walls") or []
        if isinstance(wall.get("height"), (int, float)) and wall.get("height") > 0
    )
    storey = heights[len(heights) // 2] if heights else 2.5

    floors = []
    for level in sorted(levels, key=lambda item: item.get("index", 0)):
        index = int(level.get("index", 0))
        floors.append({
            "id": str(level.get("id") or f"level_{index}"),
            "name": str(level.get("name") or f"Level {index + 1}"),
            "index": index,
            "y": round(index * storey, 2),
        })
    return floors


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
