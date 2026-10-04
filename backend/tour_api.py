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
import os
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from audit_middleware import AuditCategory, audit_now
from db.connection import tenant_tx
from tenancy import Role, TenantContext, require_context
from billing import require_active_subscription
from reconstruction_providers import SPATIAL_AI_DISCLOSURE, get_provider
from reconstruction_worker import QUEUE_MAX as RECON_QUEUE_MAX, ReconstructionJob, enqueue

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

    tour = build_tour(rows, scene_rows, plan_row, lead_id=lead_id, listing_id=listing_id)
    tour["splat_scene"] = await _scene_manifest_for(rows, tour.get("splat_url"))
    tour["splat_stream_url"] = _stream_url_for(rows, tour.get("splat_url"), ctx)
    return tour


def _stream_url_for(rows, splat_url: Optional[str], ctx: TenantContext) -> Optional[str]:
    """A short-lived signed URL for the splat the tour opens, if it is stored.

    Lets the viewer stream with real progress and HTTP caching instead of
    pulling the whole file into memory behind the JWT first. Minted only here,
    inside an authenticated RLS-scoped read of that very row.
    """
    if not splat_url:
        return None
    row = next((r for r in rows if r["kind"] == "splat" and r["url"] == splat_url
                and _has_key(r)), None)
    if row is None:
        return None
    try:
        import space_assets

        return space_assets.signed_path(str(row["id"]), str(ctx.tenant_id))
    except Exception:  # noqa: BLE001 — the JWT path still works without it
        log.info("Could not sign a stream URL for %s", row["id"])
        return None


async def _scene_manifest_for(rows, splat_url: Optional[str]) -> Optional[dict]:
    """The canonical frame recorded beside the splat the tour will open.

    Which way is up and where to start are decided once, by the worker, and
    stored as a sidecar under the artifact's own key — the same answer the
    floor plan reads. Served here so the viewer opens at a real captured
    viewpoint instead of wherever the solver's frame happened to leave it.

    Best-effort: a capture from before this existed has no manifest, and the
    viewer then frames the dense bounds, which is what it did before.
    """
    if not splat_url:
        return None
    row = next((r for r in rows
                if r["kind"] == "splat" and r["url"] == splat_url and _has_key(r)), None)
    key = row["s3_key"] if row is not None else None
    if not key:
        return None
    import asyncio
    import json as _json

    import object_storage
    import scene_manifest

    try:
        raw = await asyncio.to_thread(object_storage.get_bytes, key + scene_manifest.SUFFIX)
        payload = _json.loads(raw)
    except Exception:  # noqa: BLE001 — absent or unreadable: frame bounds instead
        return None
    # v1 is upgraded explicitly (marked legacy, scale unknown), never silently
    # read as if it carried v2's fields; anything else is ignored.
    payload = scene_manifest.normalise(payload)
    if payload is None:
        return None
    override = _row_get(row, "scene_override")
    if isinstance(override, str):
        try:
            override = _json.loads(override)
        except ValueError:
            override = None
    return scene_manifest.apply_override(payload, override)


def _row_get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _has_key(row) -> bool:
    try:
        return bool(row["s3_key"])
    except (KeyError, IndexError, TypeError):
        return False


def _delivery_format(row) -> Optional[str]:
    """The stored file's extension, lowercased, e.g. `.sog`.

    Returned to the browser as a format hint because `/api/media/{id}` is
    deliberately extensionless — the id is the whole path, and a viewer that
    guesses from it guesses wrong.

    Takes the row, not the key, because a caller that assembled its own rows
    (every test, and the agent tool surface) will not have selected `s3_key`.
    A missing key means "unknown format", which the viewer already handles by
    falling back to inference — not a crash that takes the whole tour down.
    """
    try:
        s3_key = row["s3_key"]
    except (KeyError, IndexError, TypeError):
        return None
    if not s3_key:
        return None
    _, dot, ext = str(s3_key).rpartition(".")
    if not dot or not ext.isalnum() or len(ext) > 10:
        return None
    return f".{ext.lower()}"


async def fetch_tour_rows(conn, lead_id, listing_id):
    """The three reads behind a tour, on a caller-supplied connection.

    Separated from resolve_tour so the agent tool surface can answer "what does
    this property have" without opening a second transaction inside the one it
    is already running in.
    """
    rows = await conn.fetch(
        """
        SELECT id, kind, url, sort_order, s3_key, scene_override,
               COALESCE(provenance, 'captured') AS provenance
          FROM property_media
         WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
             OR ($2::uuid IS NOT NULL AND listing_id = $2))
           -- A replaced space is kept for rollback but never shown: publish
           -- stamps the old one in the same transaction as it inserts the new.
           AND NOT (kind = 'splat' AND superseded_at IS NOT NULL)
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
    # Newest first. Rows arrive in sort order (oldest first), and a property
    # can briefly hold more than one current capture — an uploaded scan beside
    # a reconstruction, or rows from before atomic publish existed. The most
    # recent one is the one someone meant to show.
    all_splats = [r for r in reversed(rows) if r["kind"] == "splat"]

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
            # `/api/media/{id}` carries no extension on purpose, so a viewer
            # cannot tell a .sog from a .splat by looking at it — PlayCanvas
            # answered "No parser found for resource" and the tour stayed
            # black. The server is the only party that knows, so it says.
            "format": _delivery_format(row),
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
        # Same reason as `format` on the asset above: the viewer needs the
        # delivery format, and the URL cannot carry it.
        "splat_format": (
            _delivery_format(splats[0]) if has_splat
            else _delivery_format(demo_splats[0]) if has_demo_splat
            else None
        ),
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
# Neoh Space builds — enqueue a capture→space job + poll its state.
# Long jobs run in the reconstruction worker pool (reconstruction_worker.py);
# this is the 202-accept-then-poll surface. Customer responses carry the
# stage-based public view (space_status.public_view) — never a provider name,
# a percentage or raw toolchain errors. Operators read /diagnostics.
# ---------------------------------------------------------------------------

#: Full rebuilds of one property per rolling 24 h before the cost guard says
#: no. Quality-gate refusals and conversion-only retries cost no GPU time and
#: are not counted.
RERUN_LIMIT_PER_DAY = max(1, int(os.environ.get("RECON_RERUN_LIMIT_PER_DAY", "3") or 3))
#: Platform-wide bound on queued builds, so many brokerages together cannot
#: grow the queue without limit (each is already bounded by RECON_QUEUE_MAX).
GLOBAL_QUEUE_MAX = max(1, int(os.environ.get("RECON_GLOBAL_QUEUE_MAX", "100") or 100))

_OPERATOR_ROLES = (Role.PLATFORM_ADMIN, Role.BROKER_OWNER)

_JOB_COLUMNS = """id, status, stage, provider, provider_job_id, progress, media_id, error,
                  diagnostics, quality_gate, failure_category, attempts,
                  pipeline_version, cost_estimate_usd, gpu_seconds, output_bytes,
                  raw_output_key, retry_of, created_at, updated_at"""


def _subject_filter() -> str:
    return ("(($1::uuid IS NOT NULL AND lead_id = $1) "
            "OR ($2::uuid IS NOT NULL AND listing_id = $2))")


def _job_payload(row, ctx: TenantContext) -> dict:
    """The job as this caller may see it."""
    import space_status

    view = (space_status.operator_view(row) if ctx.role in _OPERATOR_ROLES
            else space_status.public_view(row))
    # Legacy fields existing callers read. `status` is coarse and harmless;
    # `progress` is deliberately absent — no provider reports a real one.
    view["status"] = row["status"]
    view["media_id"] = str(row["media_id"]) if row["media_id"] else None
    view["quality_gate"] = row["quality_gate"]
    return view


async def _active_job(conn, lead_id, listing_id):
    return await conn.fetchrow(
        f"SELECT {_JOB_COLUMNS} FROM reconstruction_jobs "
        f"WHERE status IN ('queued', 'running') AND {_subject_filter()} "
        "ORDER BY created_at DESC LIMIT 1",
        lead_id, listing_id,
    )


async def _insert_job(conn, ctx, lead_id, listing_id, *, idempotency_key=None,
                      retry_of=None, resume_from=None, raw_output_key=None):
    """Insert one queued job, or return the one that already holds the slot.

    The partial unique index (one active build per property, migration 0126)
    makes a double-submit race lose cleanly instead of renting two GPUs.
    """
    import asyncpg

    try:
        row = await conn.fetchrow(
            f"""
            INSERT INTO reconstruction_jobs
                (tenant_id, lead_id, listing_id, status, stage, created_by,
                 idempotency_key, retry_of, resume_from, raw_output_key)
            VALUES ($1, $2, $3, 'queued', 'queued', $4, $5, $6, $7, $8)
            RETURNING {_JOB_COLUMNS}
            """,
            ctx.tenant_id, lead_id, listing_id, ctx.agent_id,
            idempotency_key, retry_of, resume_from, raw_output_key,
        )
        return row, False
    except asyncpg.UniqueViolationError:
        existing = await _active_job(conn, lead_id, listing_id)
        if existing is None and idempotency_key:
            existing = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM reconstruction_jobs WHERE idempotency_key = $1",
                idempotency_key,
            )
        if existing is None:
            raise
        return existing, True


@router.post("/crm/reconstruction-jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_active_subscription)])
async def enqueue_reconstruction(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    confirm_rebuild: bool = Query(default=False),
    idempotency_key: Optional[str] = Query(default=None, max_length=128),
    ctx: TenantContext = Depends(require_context),
):
    """Queue a Neoh Space build for one property. Returns 202 + job_id; poll
    GET /crm/reconstruction-jobs/{id}. 503 if the configured provider isn't
    available (no GPU / no key) — never silently fakes a result.

    Cost guard, in order: subscription (dependency) → idempotency key →
    one active build per property (a second tap returns the first job) →
    explicit confirmation before replacing a published space → a per-property
    daily rebuild limit → bounded queues per brokerage and platform-wide.
    """
    if lead_id is None and listing_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id.")
    ok, why = get_provider().available()
    if not ok:
        log.warning("Space build unavailable: %s", why)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Building 3D spaces is not available right now. Your photos and video are saved.",
        )

    async with tenant_tx(ctx) as conn:
        # Validate the target exists in this tenant BEFORE inserting. The
        # reconstruction_jobs FKs (lead_id->leads, listing_id->listings) would
        # otherwise raise ForeignKeyViolation -> unhandled 500 on a bogus or
        # cross-tenant id. RLS scopes these SELECTs to the caller's tenant.
        if lead_id is not None and not await conn.fetchval("SELECT 1 FROM leads WHERE id = $1", lead_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
        if listing_id is not None and not await conn.fetchval("SELECT 1 FROM listings WHERE id = $1", listing_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")

        if idempotency_key:
            prior = await conn.fetchrow(
                f"SELECT {_JOB_COLUMNS} FROM reconstruction_jobs WHERE idempotency_key = $1",
                idempotency_key,
            )
            if prior is not None:
                return {"job_id": str(prior["id"]), "deduplicated": True,
                        **_job_payload(prior, ctx)}

        active = await _active_job(conn, lead_id, listing_id)
        if active is not None:
            # A second tap, a second tab, a retried request: the same build.
            return {"job_id": str(active["id"]), "deduplicated": True,
                    **_job_payload(active, ctx)}

        published = await conn.fetchval(
            f"""SELECT count(*) FROM property_media
                 WHERE kind = 'splat' AND superseded_at IS NULL
                   AND COALESCE(provenance, 'captured') = 'captured'
                   AND {_subject_filter()}""",
            lead_id, listing_id,
        )
        if published and not confirm_rebuild:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This property already has a 3D space. Confirm to rebuild it — "
                "the current space stays visible until the new one is ready.",
            )

        recent = await conn.fetchval(
            f"""SELECT count(*) FROM reconstruction_jobs
                 WHERE {_subject_filter()}
                   AND created_at > now() - interval '24 hours'
                   AND resume_from IS NULL
                   AND status IN ('succeeded', 'failed', 'needs_attention')""",
            lead_id, listing_id,
        )
        if recent >= RERUN_LIMIT_PER_DAY:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "This space was rebuilt too many times today. Try again tomorrow.",
                headers={"Retry-After": "3600"},
            )

        # Backpressure: a bounded backlog per brokerage, counted from the rows
        # that ARE the queue (RLS scopes the count to this tenant). One
        # brokerage cannot bury every other one's captures behind its own.
        backlog = await conn.fetchval(
            "SELECT count(*) FROM reconstruction_jobs WHERE status = 'queued'"
        )
        if backlog >= RECON_QUEUE_MAX:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Many spaces are being built right now — try again shortly.",
                headers={"Retry-After": "60"},
            )
    # Platform-wide bound, read across tenants (count only, nothing returned).
    async with tenant_tx(_platform_ctx()) as pconn:
        everyone = await pconn.fetchval(
            "SELECT count(*) FROM reconstruction_jobs WHERE status = 'queued'")
    if everyone >= GLOBAL_QUEUE_MAX:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Many spaces are being built right now — try again shortly.",
            headers={"Retry-After": "120"},
        )

    async with tenant_tx(ctx) as conn:
        # The committed row is the job, and it precedes the local wake-up.
        row, deduplicated = await _insert_job(
            conn, ctx, lead_id, listing_id, idempotency_key=idempotency_key)
    job_id = str(row["id"])
    if not deduplicated:
        try:
            await audit_now(ctx, AuditCategory.GENERATE_TOUR, "space_build_requested",
                            target_id=job_id, metadata={"lead_id": str(lead_id) if lead_id else None,
                                                        "listing_id": str(listing_id) if listing_id else None})
        except Exception:  # noqa: BLE001 — audit is best-effort here; the row is durable
            log.debug("audit for space build %s failed", job_id)
    # The committed row is the job. This only wakes a worker sharing this
    # process (single-process dev); the worker service claims it regardless.
    enqueue(ReconstructionJob(
        ctx=ctx, job_id=job_id,
        lead_id=str(lead_id) if lead_id else None,
        listing_id=str(listing_id) if listing_id else None,
    ))
    return {"job_id": job_id, "deduplicated": deduplicated, **_job_payload(row, ctx)}


def _platform_ctx() -> TenantContext:
    import os as _os

    return TenantContext(
        agent_id="space-queue-bound",
        tenant_id=_os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )


@router.get("/crm/reconstruction-jobs/{job_id}")
async def reconstruction_job_status(
    job_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Poll a build (RLS-scoped). Stage-based and in product language for
    everyone; operators additionally get provider, cost and diagnostics."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"SELECT {_JOB_COLUMNS} FROM reconstruction_jobs WHERE id = $1", job_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return _job_payload(row, ctx)


@router.get("/crm/reconstruction-jobs/{job_id}/diagnostics")
async def reconstruction_job_diagnostics(
    job_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Everything recorded about one build — operators only.

    Counts, sizes, durations, tool names, provider ids and cost estimates.
    Never capture content."""
    if ctx.role not in _OPERATOR_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Diagnostics are for brokerage admins.")
    import space_status

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"SELECT {_JOB_COLUMNS} FROM reconstruction_jobs WHERE id = $1", job_id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return space_status.operator_view(row)


@router.post("/crm/reconstruction-jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(require_active_subscription)])
async def retry_reconstruction(
    job_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Retry a failed build the cheapest honest way.

    * Conversion failed after a good reconstruction → reconvert the preserved
      raw output. No GPU, not counted against the daily limit.
    * Anything else → a new full build, which goes through the same cost
      guard as a fresh one (POST /crm/reconstruction-jobs).
    The failed job is never mutated; the retry is a new row that names it.
    """
    async with tenant_tx(ctx) as conn:
        old = await conn.fetchrow(
            f"SELECT {_JOB_COLUMNS}, lead_id, listing_id FROM reconstruction_jobs WHERE id = $1",
            job_id,
        )
        if old is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
        if old["status"] in ("queued", "running", "succeeded"):
            raise HTTPException(status.HTTP_409_CONFLICT, "This build does not need a retry.")
        if not old["raw_output_key"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Start a new build for this property — this one has nothing to resume from.",
            )
        row, deduplicated = await _insert_job(
            conn, ctx, old["lead_id"], old["listing_id"],
            retry_of=old["id"], resume_from="conversion",
            raw_output_key=old["raw_output_key"],
        )
    new_id = str(row["id"])
    enqueue(ReconstructionJob(
        ctx=ctx, job_id=new_id,
        lead_id=str(old["lead_id"]) if old["lead_id"] else None,
        listing_id=str(old["listing_id"]) if old["listing_id"] else None,
        resume_from="conversion", raw_output_key=old["raw_output_key"],
    ))
    return {**_job_payload(row, ctx), "job_id": new_id, "deduplicated": deduplicated,
            "retry_kind": "conversion"}


@router.get("/crm/space")
async def space_summary(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """The property's Neoh Space at a glance: what is published, and the
    latest build. Lets the capture panel resume watching a build after the
    page was left, instead of forgetting it existed."""
    if lead_id is None and listing_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id.")
    async with tenant_tx(ctx) as conn:
        published = await conn.fetchrow(
            f"""SELECT id, s3_key, created_at, COALESCE(provenance, 'captured') AS provenance
                  FROM property_media
                 WHERE kind = 'splat' AND superseded_at IS NULL AND {_subject_filter()}
                 ORDER BY (COALESCE(provenance, 'captured') = 'captured') DESC, created_at DESC
                 LIMIT 1""",
            lead_id, listing_id,
        )
        latest = await conn.fetchrow(
            f"SELECT {_JOB_COLUMNS} FROM reconstruction_jobs WHERE {_subject_filter()} "
            "ORDER BY created_at DESC LIMIT 1",
            lead_id, listing_id,
        )
        previous = await conn.fetchval(
            f"""SELECT count(*) FROM property_media
                 WHERE kind = 'splat' AND superseded_at IS NOT NULL AND {_subject_filter()}""",
            lead_id, listing_id,
        )
    return {
        "published": ({
            "media_id": str(published["id"]),
            "published_at": published["created_at"].isoformat() if published["created_at"] else None,
            "is_this_property": published["provenance"] == "captured",
            "format": _delivery_format(published),
        } if published else None),
        "previous_versions": int(previous or 0),
        "latest_build": _job_payload(latest, ctx) if latest else None,
    }


class SceneOverrideIn(BaseModel):
    """Curated corrections an operator may apply over the computed scene."""

    model_config = ConfigDict(extra="forbid")

    entryCamera: Optional[dict] = None
    scaleCalibration: Optional[dict] = None


@router.put("/crm/space/{media_id}/scene-override")
async def put_scene_override(
    media_id: UUID,
    body: SceneOverrideIn,
    ctx: TenantContext = Depends(require_context),
):
    """Set a curated starting view and/or an operator scale calibration.

    The computed scene.json is never rewritten; the override is stored on the
    media row and merged on read, recorded under `overrides`. A calibration
    is the ONLY way (besides measured capture metadata) a space becomes
    `metric`, so it is restricted to brokerage admins and must carry its basis.
    """
    import scene_manifest

    payload = body.model_dump(exclude_none=True)
    if "entryCamera" in payload and not scene_manifest._valid_camera(payload["entryCamera"]):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "entryCamera needs numeric position and target triples.")
    calibration = payload.get("scaleCalibration")
    if calibration is not None:
        if ctx.role not in _OPERATOR_ROLES:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Only brokerage admins can calibrate measurements.")
        try:
            mpu = float(calibration.get("metresPerUnit"))
        except (TypeError, ValueError):
            mpu = 0.0
        basis = str(calibration.get("basis") or "").strip()
        if not (0 < mpu < 1000) or not basis:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "A calibration needs a positive metresPerUnit and its basis "
                                "(what known distance it was measured against).")
        payload["scaleCalibration"] = {"metresPerUnit": mpu, "basis": basis[:300],
                                       "by": ctx.agent_id}
    import json as _json

    async with tenant_tx(ctx) as conn:
        updated = await conn.fetchval(
            "UPDATE property_media SET scene_override = $2::jsonb "
            "WHERE id = $1 AND kind = 'splat' RETURNING id",
            media_id, _json.dumps(payload) if payload else None,
        )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found.")
    return {"media_id": str(media_id), "override": payload or None}


@router.delete("/crm/space")
async def delete_space(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    confirm: bool = Query(default=False),
    ctx: TenantContext = Depends(require_context),
):
    """Delete a property's 3D space: every published and previous version,
    its poses, point cloud, scene file, provenance manifest and any preserved
    raw output. Original photos and video are NOT deleted here — they are the
    customer's source media and are removed through media deletion or the
    privacy lifecycle, which own them.
    """
    if lead_id is None and listing_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide lead_id or listing_id.")
    if not confirm:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Confirm to delete this property's 3D space. Photos and video are kept.")
    import space_assets

    async with tenant_tx(ctx) as conn:
        active = await _active_job(conn, lead_id, listing_id)
        if active is not None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "A build is still running for this property. Try again when it finishes.")
        media = await conn.fetch(
            f"SELECT id, s3_key FROM property_media WHERE kind = 'splat' AND {_subject_filter()}",
            lead_id, listing_id,
        )
        raw = await conn.fetch(
            f"SELECT raw_output_key FROM reconstruction_jobs "
            f"WHERE raw_output_key IS NOT NULL AND {_subject_filter()}",
            lead_id, listing_id,
        )
    keys: list[str] = []
    for row in media:
        keys.extend(space_assets.keys_for_space(row["s3_key"]))
    keys.extend(space_assets.keys_for_space(None, [r["raw_output_key"] for r in raw]))
    result = await asyncio.to_thread(space_assets.delete_objects, keys)
    if result["failed"]:
        # Rows stay so a retry can find the objects again; nothing is orphaned.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Some files could not be deleted yet. Nothing was removed from the property; try again.")
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            f"UPDATE reconstruction_jobs SET raw_output_key = NULL WHERE {_subject_filter()}",
            lead_id, listing_id,
        )
        await conn.execute(
            f"DELETE FROM property_media WHERE kind = 'splat' AND {_subject_filter()}",
            lead_id, listing_id,
        )
    try:
        await audit_now(ctx, AuditCategory.DATA_DELETE, "space_deleted",
                        target_id=str(lead_id or listing_id),
                        metadata={"versions": len(media), "objects": result["deleted"]})
    except Exception:  # noqa: BLE001
        log.debug("audit for space deletion failed")
    return {"deleted_versions": len(media), "deleted_objects": result["deleted"],
            "photos_and_video": "kept"}


@router.get("/space/assets/{media_id}")
async def signed_space_asset(
    media_id: UUID,
    request: Request,
    t: str = Query(..., max_length=64),
    exp: int = Query(...),
    sig: str = Query(..., max_length=128),
):
    """Stream a space's delivery file to a holder of a fresh signed URL.

    No JWT: the URL itself is the short-lived capability, minted by the
    authenticated resolver for one media id and tenant. Range requests are
    honoured so the viewer can stream; the response is privately cacheable
    for no longer than the URL lives. Never lists, never redirects to a raw
    storage URL, and answers 404 for anything it will not serve.
    """
    import time as _time

    import space_assets

    if not space_assets.verify(str(media_id), t, exp, sig):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    try:
        tenant = str(UUID(t))
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    ctx = TenantContext(agent_id="space-asset", tenant_id=tenant, role=Role.AGENT)
    async with tenant_tx(ctx) as conn:
        key = await conn.fetchval(
            "SELECT s3_key FROM property_media "
            "WHERE id = $1 AND kind = 'splat' AND s3_key IS NOT NULL",
            media_id,
        )
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": f"private, max-age={max(0, int(exp) - int(_time.time()))}",
    }
    range_header = request.headers.get("range")
    try:
        if not range_header:
            data, _size = await asyncio.to_thread(space_assets.read_range, key, None)
            return Response(content=data, media_type="application/octet-stream", headers=headers)
        # Ranged: learn the size from a one-byte read, then read the slice.
        _, size = await asyncio.to_thread(space_assets.read_range, key, (0, 0))
        try:
            rng = space_assets.parse_range(range_header, size)
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        data, size = await asyncio.to_thread(space_assets.read_range, key, rng)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if rng is None:
        return Response(content=data, media_type="application/octet-stream", headers=headers)
    headers["Content-Range"] = f"bytes {rng[0]}-{rng[0] + len(data) - 1}/{size}"
    return Response(content=data, status_code=206, media_type="application/octet-stream",
                    headers=headers)
