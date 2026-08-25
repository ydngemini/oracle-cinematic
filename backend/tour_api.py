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
    """Resolve the tour for one property (lead or listing).

    Returns `assets`: every asset the property has — 3D capture, 360 scenes,
    photos, floor plan, exterior — each carrying its own provenance and label.
    They compose into one tour rather than competing for a single slot.

    The `best_tier` / `badge` / `splat_url` fields are derived from `assets` and
    kept for existing callers. They are a summary, not a filter: selecting on
    them is what caused a property holding a splat, 360s and photos to display
    only the splat, and a property holding photos alone to display nothing."""
    if lead_id is None and listing_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id."
        )

    async with tenant_tx(ctx) as conn:
        rows, scene_rows, plan_row = await fetch_tour_rows(conn, lead_id, listing_id)

    return build_tour(rows, scene_rows, plan_row, lead_id=lead_id, listing_id=listing_id)


async def fetch_tour_rows(conn, lead_id, listing_id):
    """The three reads behind a tour, on a caller-supplied connection.

    Separated from resolve_tour so the agent tool surface can answer "what does
    this property have" without opening a second transaction inside the one it
    is already running in.
    """
    rows = await conn.fetch(
        """
        SELECT id, kind, url, sort_order,
               COALESCE(provenance, 'captured') AS provenance
          FROM property_media
         WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
             OR ($2::uuid IS NOT NULL AND listing_id = $2))
         ORDER BY sort_order ASC, created_at ASC
        """,
        lead_id, listing_id,
    )
    scene_rows = await conn.fetch(
        """
        SELECT s.id, s.media_id, s.floor_index, s.label, s.sort_order,
               s.position_x, s.position_y, s.position_z, s.heading_deg,
               s.neighbour_ids, m.url,
               COALESCE(m.provenance, 'captured') AS provenance
          FROM property_pano_scenes AS s
          JOIN property_media       AS m ON m.id = s.media_id
         WHERE (($1::uuid IS NOT NULL AND s.lead_id = $1)
             OR ($2::uuid IS NOT NULL AND s.listing_id = $2))
         ORDER BY s.floor_index ASC, s.sort_order ASC, s.created_at ASC
        """,
        lead_id, listing_id,
    )
    plan_row = await conn.fetchrow(
        """
        SELECT document
          FROM property_floorplans
         WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
             OR ($2::uuid IS NOT NULL AND listing_id = $2))
         LIMIT 1
        """,
        lead_id, listing_id,
    )
    return rows, scene_rows, plan_row


def build_tour(rows, scene_rows, plan_row, *, lead_id=None, listing_id=None) -> dict:
    """Assemble the tour from already-fetched rows. Pure, so it is testable
    without a database and reusable by any caller that has the rows."""
    document = plan_row["document"] if plan_row else None
    floors = _floors_from_plan(document)

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

    # ---- the tour itself: every asset, each labelled for what it is ---------
    #
    # `best_tier` below picks a single winner, and the viewer used to render
    # only that winner: a property holding a splat AND 360s AND photos showed
    # the splat and silently dropped the rest, discarding captures the agent
    # paid to take. Worse, a property with photos but no splat opened nothing at
    # all, because the viewer bailed when there was no splat_url.
    #
    # So the tour is the union of what exists, not the maximum of it. Ordering
    # is most-immersive-first, which decides only what opens by default — it
    # never removes anything from the list.
    #
    # Honesty moves onto each asset. One tour-wide `is_this_property` had to be
    # computed from the splat alone, which is why real 360s of a house were
    # suppressed whenever a demo splat sat beside them: the flag said "not this
    # property" and the viewer believed it about everything.
    assets: list[dict] = []

    for row in all_splats:
        captured = _captured(row)
        assets.append({
            "kind": "splat",
            "url": row["url"],
            "provenance": row["provenance"],
            "is_this_property": captured,
            "walkable": True,
            "label": "Full 3D walkthrough" if captured else _DEMO_BADGE,
            "note": _TIER_NOTE[3] if captured else _DEMO_NOTE,
            "disclosure": SPATIAL_AI_DISCLOSURE,
        })

    if scenes:
        # >= 2 vantage points is what makes it a walkthrough rather than a
        # single view; one 360 still belongs in the tour, just not described as
        # somewhere you can move between rooms.
        real_scenes = [sc for sc in scenes if sc["is_this_property"]]
        assets.append({
            "kind": "pano",
            "scenes": scenes,
            "count": len(scenes),
            "provenance": "captured" if len(real_scenes) == len(scenes) else "mixed",
            "is_this_property": bool(real_scenes) and len(real_scenes) == len(scenes),
            "walkable": has_pano,
            "label": "360° walkthrough" if has_pano else "360° view",
            "note": _TIER_NOTE[2] if has_pano else
                    "A single 360° capture of this property — a view, not a walkthrough.",
            "disclosure": None,
        })

    if has_photos:
        assets.append({
            "kind": "photo",
            "count": len(photos),
            "urls": [r["url"] for r in photos],
            "provenance": "captured",
            "is_this_property": True,
            "walkable": False,
            "label": f"{len(photos)} photo{'s' if len(photos) != 1 else ''}",
            "note": "Photographs of this property.",
            "disclosure": None,
        })

    if floors:
        assets.append({
            "kind": "floorplan",
            "floors": floors,
            "count": len(floors),
            # Geometry may be estimated rather than surveyed; the floor plan
            # surfaces its own per-dimension provenance, so this asset does not
            # claim measurement it cannot back.
            "provenance": "recorded",
            "is_this_property": True,
            "walkable": False,
            "label": "Floor plan",
            "note": "Recorded floor plan for this property.",
            "disclosure": None,
        })

    # The exterior always exists, because the property always has an address.
    assets.append({
        "kind": "exterior",
        "provenance": "licensed",
        "is_this_property": True,
        "walkable": False,
        "label": "Exterior 3D",
        "note": _TIER_NOTE[0],
        "disclosure": None,
    })

    return {
        # The tour. Everything the property actually has, each item carrying its
        # own provenance so a label describes the asset on screen rather than
        # the tour as a whole.
        "assets": assets,

        # ---- derived, kept for existing callers -------------------------
        # These summarise `assets`; they no longer decide what is shown. A
        # caller that renders only the winner drops real captures, which is the
        # bug this shape replaces. Read `assets` and render all of it.
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
        # The guided route over those same scenes. Empty when there is nothing
        # to guide through, which the viewer reads as "free roam only" rather
        # than as a missing feature.
        "tourpoints": _tourpoints(scenes, document, floors),
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
            # Per-scene, not per-tour. A property can hold real 360s of the
            # house alongside a generated asset, and one flag over the whole
            # tour cannot say which is which.
            "provenance": r["provenance"],
            "is_this_property": r["provenance"] == "captured",
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


def _tourpoints(scenes: list[dict], document, floors: list[dict]) -> list[dict]:
    """An ordered guided route through the vantage points that exist.

    The scene graph is free roam: a visitor can go anywhere, which is the right
    default and a poor first impression. SPHR's runtime (MIT, lukehollis/sphr)
    models the guided version as an ordered list of *tourpoints*, each one
    moving the camera and saying something, over the same spaces the free-roam
    mode uses. That separation is the good idea and it is adopted here — the
    route is a VIEW of the scenes, never a second copy of them, so nothing can
    drift out of step with the graph it describes.

    Two rules keep this honest:

      * it invents no vantage points. A tourpoint always references a scene the
        capture actually produced, so the route cannot promise a room nobody
        photographed;
      * it names rooms only from a saved floor plan, and only when the counts
        line up. Guessing "Kitchen" because a route reached its third stop is
        exactly the kind of confident fiction the rest of this pipeline refuses.

    Ordered by floor and then by capture order, which is the order the
    photographer walked — a better route than anything derivable from the
    positions alone, because they were there.
    """
    if len(scenes) < 2:
        # One vantage point is a view, not a tour. Same rule the pano tier uses.
        return []

    by_floor: dict[int, str] = {int(f["index"]): f["name"] for f in floors}
    rooms = _room_names(document)
    ordered = sorted(
        scenes, key=lambda sc: (int(sc.get("floor_index") or 0), scenes.index(sc))
    )

    # Room names are only attached when there is one per stop. A partial match
    # would label some stops and silently leave others, which reads as missing
    # data rather than as a deliberate absence.
    named = rooms if len(rooms) == len(ordered) else []

    points = []
    for position, scene in enumerate(ordered):
        floor_index = int(scene.get("floor_index") or 0)
        label = (
            scene.get("label")
            or (named[position] if named else "")
            or (by_floor.get(floor_index) or f"Stop {position + 1}")
        )
        points.append({
            "id": f"tp_{scene['scene_id']}",
            "index": position,
            # What the viewer moves to. A reference, never a copy — the scene
            # carries the position, heading and neighbours.
            "scene_id": scene["scene_id"],
            "floor_index": floor_index,
            "label": label,
            # Deliberately empty. Narration is authored, not generated: a
            # sentence invented about a room the model has never seen is the
            # one thing a property tour must not do.
            "narration": "",
            "is_this_property": bool(scene.get("is_this_property", True)),
        })
    return points


def _room_names(document) -> list[str]:
    """Room names from a saved plan, in level then plan order, or []."""
    import json as _json

    if not document:
        return []
    if isinstance(document, str):
        try:
            document = _json.loads(document)
        except ValueError:
            return []
    rooms = document.get("rooms") or []
    names = [str(r.get("name") or "").strip() for r in rooms]
    # The reconstruction path names every room "Room 1", "Room 2" because it
    # has no OCR pass. Those are placeholders, not names, and a tour that
    # announces "Room 3" is worse than one that says nothing.
    if all(name.lower().startswith("room ") for name in names if name):
        return []
    return [name for name in names if name]


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
