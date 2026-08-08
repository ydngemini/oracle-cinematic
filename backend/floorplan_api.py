"""Persist and retrieve structured floor plans for a lead or listing.

The geometry document is authored by the Pascal 3D editor (or produced by
floorplan_pipeline) and validated here against the same schema the client
compiles against — see oracle-app/src/lib/floorplan/protocol.ts.

Design notes:
  * Server recomputes ALL derived metrics from the geometry. A client-supplied
    total_sqft is ignored, because square footage feeds ARV and MAO and must
    not be attacker- or bug-controlled.
  * Saves are upsert-per-subject plus an append-only revision row, so a rehab
    estimate can always be traced back to the layout it was computed against.
  * Machine-generated plans must declare a model version; the DB CHECK enforces
    it and this module surfaces the disclosure text with every read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

log = logging.getLogger("oracle.floorplan_api")

router = APIRouter(prefix="/api", tags=["floorplan"])

# Must track FLOORPLAN_PROTOCOL_VERSION in protocol.ts.
SCHEMA_VERSION = 1

FLOORPLAN_AI_DISCLOSURE = (
    "AI-generated floor plan derived from photos or parcel geometry — room "
    "dimensions are estimates, not a measured survey. Verify before relying on "
    "them for pricing, permitting, or disclosure."
)

M_TO_FT = 3.280839895013123
M2_TO_SQFT = M_TO_FT * M_TO_FT

# Guard rails on document size. A house floor plan is small; anything larger is
# either a bug or an attempt to stuff jsonb. Rejected at the edge, not the DB.
MAX_WALLS = 4000
MAX_ROOMS = 500
MAX_OPENINGS = 2000
MAX_LEVELS = 40
MAX_POLYGON_POINTS = 512

_VALID_SOURCES = {"manual", "ai_vision", "parcel_vector", "imported"}
_VALID_ROOM_TYPES = {
    "bedroom", "bathroom", "kitchen", "living", "dining",
    "hallway", "garage", "utility", "closet", "other",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Point2D = tuple[float, float]


def _finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


class WallIn(_Strict):
    id: str = Field(min_length=1, max_length=128)
    start: Point2D
    end: Point2D
    thickness: float = Field(default=0.1, ge=0, le=5)
    height: float = Field(default=2.5, ge=0, le=30)
    levelId: Optional[str] = Field(default=None, max_length=128)
    interior: bool = False

    @field_validator("start", "end")
    @classmethod
    def _check_point(cls, v: Point2D) -> Point2D:
        # NaN/Inf would poison every downstream metric silently.
        return (_finite(v[0], "wall coordinate"), _finite(v[1], "wall coordinate"))


class RoomIn(_Strict):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(default="Room", max_length=200)
    type: str = "other"
    polygon: list[Point2D]
    levelId: Optional[str] = Field(default=None, max_length=128)
    boundaryWallIds: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        # Unknown types cost as 'other' rather than 422 — a client that adds a
        # room type shouldn't be unable to save its work.
        return v if v in _VALID_ROOM_TYPES else "other"

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, v: list[Point2D]) -> list[Point2D]:
        if len(v) > MAX_POLYGON_POINTS:
            raise ValueError(f"polygon exceeds {MAX_POLYGON_POINTS} points")
        return [(_finite(p[0], "polygon coordinate"), _finite(p[1], "polygon coordinate")) for p in v]


class OpeningIn(_Strict):
    id: str = Field(min_length=1, max_length=128)
    kind: str
    wallId: Optional[str] = Field(default=None, max_length=128)
    width: float = Field(default=0, ge=0, le=30)
    height: float = Field(default=0, ge=0, le=30)

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in {"door", "window"}:
            raise ValueError("opening kind must be 'door' or 'window'")
        return v


class LevelIn(_Strict):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(default="Level", max_length=200)
    index: int = Field(default=0, ge=-20, le=200)


class ProvenanceIn(_Strict):
    source: str = "manual"
    ai_generated: bool = False
    model_version: Optional[str] = Field(default=None, max_length=120)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    notes: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if v not in _VALID_SOURCES:
            raise ValueError(f"source must be one of {sorted(_VALID_SOURCES)}")
        return v


class FloorplanDocumentIn(_Strict):
    schema_version: int = SCHEMA_VERSION
    units: str = "metric"
    levels: list[LevelIn] = Field(default_factory=list)
    walls: list[WallIn] = Field(default_factory=list)
    rooms: list[RoomIn] = Field(default_factory=list)
    openings: list[OpeningIn] = Field(default_factory=list)
    provenance: ProvenanceIn = Field(default_factory=ProvenanceIn)

    @field_validator("units")
    @classmethod
    def _check_units(cls, v: str) -> str:
        # Everything downstream assumes metres. An imperial document would be
        # silently 10.7x wrong on area, so refuse rather than guess.
        if v != "metric":
            raise ValueError("units must be 'metric'")
        return v

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v > SCHEMA_VERSION:
            raise ValueError(
                f"document schema_version {v} is newer than this server supports "
                f"({SCHEMA_VERSION}); upgrade the backend"
            )
        return v

    @field_validator("walls")
    @classmethod
    def _cap_walls(cls, v: list[WallIn]) -> list[WallIn]:
        if len(v) > MAX_WALLS:
            raise ValueError(f"too many walls (max {MAX_WALLS})")
        return v

    @field_validator("rooms")
    @classmethod
    def _cap_rooms(cls, v: list[RoomIn]) -> list[RoomIn]:
        if len(v) > MAX_ROOMS:
            raise ValueError(f"too many rooms (max {MAX_ROOMS})")
        return v

    @field_validator("openings")
    @classmethod
    def _cap_openings(cls, v: list[OpeningIn]) -> list[OpeningIn]:
        if len(v) > MAX_OPENINGS:
            raise ValueError(f"too many openings (max {MAX_OPENINGS})")
        return v

    @field_validator("levels")
    @classmethod
    def _cap_levels(cls, v: list[LevelIn]) -> list[LevelIn]:
        if len(v) > MAX_LEVELS:
            raise ValueError(f"too many levels (max {MAX_LEVELS})")
        return v


class SaveFloorplanRequest(_Strict):
    document: FloorplanDocumentIn
    # Optional snapshot of the line items this layout produced, stored on the
    # revision so an estimate stays reproducible after the cost table changes.
    rehab_items: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Server-side metric derivation (authoritative)
# ---------------------------------------------------------------------------

def _polygon_area_m2(polygon: list[Point2D]) -> float:
    """Shoelace. Absolute value — winding order is not guaranteed."""
    if len(polygon) < 3:
        return 0.0
    twice = 0.0
    for i in range(len(polygon)):
        x1, y1 = polygon[i - 1]
        x2, y2 = polygon[i]
        twice += x1 * y2 - x2 * y1
    return abs(twice) / 2.0


def derive_metrics(doc: FloorplanDocumentIn) -> dict[str, float]:
    """Recompute every persisted metric from geometry. Never trust the client."""
    wall_linear_m = 0.0
    for wall in doc.walls:
        wall_linear_m += math.dist(wall.start, wall.end)

    floor_area_m2 = sum(_polygon_area_m2(room.polygon) for room in doc.rooms)

    return {
        "total_sqft": round(floor_area_m2 * M2_TO_SQFT, 2),
        "wall_linear_ft": round(wall_linear_m * M_TO_FT, 2),
        "room_count": len(doc.rooms),
        "level_count": len(doc.levels),
    }


def _subject_or_422(lead_id: Optional[UUID], listing_id: Optional[UUID]) -> None:
    if (lead_id is None) == (listing_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide exactly one of lead_id or listing_id.",
        )


async def _assert_subject_exists(conn, lead_id, listing_id) -> None:
    """RLS scopes these SELECTs, so a cross-tenant id reads as 404, not 403."""
    if lead_id is not None and not await conn.fetchval("SELECT 1 FROM leads WHERE id = $1", lead_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
    if listing_id is not None and not await conn.fetchval("SELECT 1 FROM listings WHERE id = $1", listing_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")


def _row_to_response(row) -> dict[str, Any]:
    document = row["document"]
    if isinstance(document, str):  # asyncpg returns jsonb as str unless a codec is set
        document = json.loads(document)
    ai_generated = bool(row["ai_generated"])
    return {
        "floorplan_id": str(row["id"]),
        "schema_version": row["schema_version"],
        "document": document,
        "metrics": {
            "total_sqft": float(row["total_sqft"]),
            "wall_linear_ft": float(row["wall_linear_ft"]),
            "room_count": row["room_count"],
            "level_count": row["level_count"],
        },
        "source": row["source"],
        "ai_generated": ai_generated,
        "model_version": row["model_version"],
        "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
        # Surfaced on every read so the UI cannot forget to show it.
        "disclosure": FLOORPLAN_AI_DISCLOSURE if ai_generated else None,
        "updated_at": row["updated_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/crm/floorplan")
async def get_floorplan(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Fetch the current floor plan for one property. 204 when none exists."""
    _subject_or_422(lead_id, listing_id)

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, schema_version, document, total_sqft, wall_linear_ft,
                   room_count, level_count, source, ai_generated, model_version,
                   confidence, updated_at
              FROM property_floorplans
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             LIMIT 1
            """,
            lead_id, listing_id,
        )

    if row is None:
        # Empty rather than 404: "this property has no plan yet" is a normal
        # state the drawer renders as a blank editor.
        return {"floorplan_id": None, "document": None, "metrics": None}

    if row["schema_version"] > SCHEMA_VERSION:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This floor plan was saved by a newer client version and cannot be read safely.",
        )
    return _row_to_response(row)


@router.put("/crm/floorplan")
async def save_floorplan(
    body: SaveFloorplanRequest,
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Upsert the current floor plan and append an immutable revision."""
    _subject_or_422(lead_id, listing_id)

    doc = body.document
    metrics = derive_metrics(doc)
    prov = doc.provenance

    # Mirror the DB CHECK at the edge so the client gets 422 with a reason
    # rather than an opaque integrity error.
    if prov.ai_generated and not prov.model_version:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "AI-generated floor plans must declare a model_version.",
        )

    document_json = json.dumps(doc.model_dump(mode="json"))
    rehab_json = json.dumps(body.rehab_items) if body.rehab_items is not None else None

    async with tenant_tx(ctx) as conn:
        await _assert_subject_exists(conn, lead_id, listing_id)

        # The unique indexes are partial (one per nullable FK), so a single
        # ON CONFLICT target cannot address both. Do an explicit select-for-
        # update then insert-or-update inside the transaction instead;
        # tenant_tx gives us the isolation that makes this safe against a
        # concurrent double-save from two tabs.
        existing = await conn.fetchrow(
            """
            SELECT id FROM property_floorplans
             WHERE (($1::uuid IS NOT NULL AND lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND listing_id = $2))
             FOR UPDATE
            """,
            lead_id, listing_id,
        )

        if existing is None:
            row = await conn.fetchrow(
                """
                INSERT INTO property_floorplans (
                    tenant_id, lead_id, listing_id, schema_version, document,
                    total_sqft, wall_linear_ft, room_count, level_count,
                    source, ai_generated, model_version, confidence, created_by
                )
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING id
                """,
                ctx.tenant_id, lead_id, listing_id, doc.schema_version, document_json,
                metrics["total_sqft"], metrics["wall_linear_ft"],
                metrics["room_count"], metrics["level_count"],
                prov.source, prov.ai_generated, prov.model_version, prov.confidence,
                ctx.agent_id,
            )
            floorplan_id = row["id"]
        else:
            floorplan_id = existing["id"]
            await conn.execute(
                """
                UPDATE property_floorplans
                   SET document = $2::jsonb,
                       schema_version = $3,
                       total_sqft = $4,
                       wall_linear_ft = $5,
                       room_count = $6,
                       level_count = $7,
                       source = $8,
                       ai_generated = $9,
                       model_version = $10,
                       confidence = $11,
                       updated_at = now()
                 WHERE id = $1
                """,
                floorplan_id, document_json, doc.schema_version,
                metrics["total_sqft"], metrics["wall_linear_ft"],
                metrics["room_count"], metrics["level_count"],
                prov.source, prov.ai_generated, prov.model_version, prov.confidence,
            )

        next_revision = await conn.fetchval(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM property_floorplan_revisions WHERE floorplan_id = $1",
            floorplan_id,
        )
        await conn.execute(
            """
            INSERT INTO property_floorplan_revisions (
                tenant_id, floorplan_id, revision, document,
                total_sqft, wall_linear_ft, rehab_items, created_by
            )
            VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8)
            """,
            ctx.tenant_id, floorplan_id, next_revision, document_json,
            metrics["total_sqft"], metrics["wall_linear_ft"], rehab_json, ctx.agent_id,
        )

    return {
        "floorplan_id": str(floorplan_id),
        "revision": next_revision,
        "metrics": metrics,
        "disclosure": FLOORPLAN_AI_DISCLOSURE if prov.ai_generated else None,
    }


class ExtractParcelRequest(_Strict):
    """GeoJSON building footprint → exterior shell."""
    geometry: dict[str, Any]
    wall_height_m: float = Field(default=2.5, gt=0, le=30)


class FootprintCandidatesRequest(_Strict):
    """Locate a building outline for a subject.

    Either an address (licensed, address-matched) or a coordinate (open data,
    proximity-matched) works; supplying both searches both."""

    address: str = Field(default="", max_length=300)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    radius_m: int = Field(default=40, ge=5, le=150)

    @field_validator("address")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()


@router.post("/crm/floorplan/footprint-candidates")
async def footprint_candidates(
    body: FootprintCandidatesRequest,
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Find building outlines for a subject, from licensed and open sources.

    Returns candidates for the agent to CHOOSE from — it does not pick one and
    it does not save. Each carries its source, licence and attribution, because
    ODbL requires the credit wherever the geometry is shown and because an agent
    deciding whether to trust an outline needs to know where it came from.

    Feed the chosen `geometry` to /crm/floorplan/extract-parcel, which turns it
    into an exterior shell. That shell has no interior walls by design — a
    footprint contains no interior information, and inventing one would put
    fabricated square footage into a rehab estimate.
    """
    _subject_or_422(lead_id, listing_id)

    if not body.address and (body.latitude is None or body.longitude is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide an address, a latitude/longitude pair, or both.",
        )

    async with tenant_tx(ctx) as conn:
        await _assert_subject_exists(conn, lead_id, listing_id)

    from data_integrations.building_footprint import resolve_footprints

    candidates = await resolve_footprints(
        address=body.address,
        lat=body.latitude,
        lon=body.longitude,
        radius_m=body.radius_m,
    )
    return {
        "candidates": [candidate.to_dict() for candidate in candidates],
        # An empty list is a real answer, not an error. Rural OSM coverage is
        # patchy and not every address matches a licensed building record.
        "count": len(candidates),
    }


@router.post("/crm/floorplan/auto-dimensions")
async def auto_dimensions(
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    wall_height_m: Optional[float] = Query(default=None, gt=0, le=30),
    ctx: TenantContext = Depends(require_context),
):
    """Resolve EVERY dimension needed to construct this property, inside and out.

    Chain: subject address (from the lead/listing row) → geocode → building
    footprint (licensed Regrid, then OSM) → complete-dimension engine, which
    fills anything unsourced with an explicitly-labelled estimate or default.
    The response's `manifest` attributes every number to
    measured/sourced/estimated/default, and `estimated_fields` lists exactly
    where the guesses are so review effort lands there.

    Returns WITHOUT saving — machine output never writes itself into
    underwriting; the agent reviews the scaffold in the editor and saves."""
    _subject_or_422(lead_id, listing_id)

    async with tenant_tx(ctx) as conn:
        await _assert_subject_exists(conn, lead_id, listing_id)
        row = await conn.fetchrow(
            "SELECT address FROM leads WHERE id = $1", lead_id
        ) if lead_id else await conn.fetchrow(
            "SELECT address FROM listings WHERE id = $1", listing_id
        )
    address = (row["address"] if row else "") or ""

    # Geocode is best-effort: without coordinates OSM cannot answer, but Regrid
    # matches on the address string and the default plate needs neither.
    lat = lon = None
    if address:
        try:
            from data_integrations.geocoder import CascadingGeocoder

            located = await CascadingGeocoder().geocode(address)
            if located:
                lat, lon = located.get("lat"), located.get("lng")
        except Exception:  # noqa: BLE001 — degrade to address-only resolution
            log.warning("auto-dimensions: geocode failed for %r", address, exc_info=True)

    from data_integrations.building_footprint import resolve_footprints
    from floorplan_pipeline.dimensions import complete_dimensions

    candidates = await resolve_footprints(address=address, lat=lat, lon=lon)
    best = candidates[0] if candidates else None

    document, manifest = complete_dimensions(
        footprint_geometry=best.geometry if best else None,
        footprint_source=best.source if best else "",
        sourced_levels=best.levels if best else None,
        wall_height_m=wall_height_m,
    )

    return {
        "document": document.to_json(),
        "manifest": manifest.to_json(),
        "estimated_fields": manifest.estimated_fields(),
        "footprint": {
            "found": best is not None,
            "source": best.source if best else None,
            "licence": best.licence if best else None,
            "attribution": best.attribution if best else None,
            "candidates": len(candidates),
        },
        "address": address,
        "geocoded": lat is not None,
    }


@router.post("/crm/floorplan/extract-parcel")
async def extract_parcel(
    body: ExtractParcelRequest,
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Derive an exterior-shell floor plan from parcel GIS geometry.

    Returns the document WITHOUT saving — the agent reviews it in the editor
    and saves explicitly. Machine output never writes itself into underwriting.
    """
    _subject_or_422(lead_id, listing_id)

    from floorplan_pipeline import extract_from_parcel_geometry  # local: heavy-ish import
    from floorplan_pipeline.errors import ExtractionError

    async with tenant_tx(ctx) as conn:
        await _assert_subject_exists(conn, lead_id, listing_id)

    try:
        document = extract_from_parcel_geometry(
            body.geometry, wall_height_m=body.wall_height_m,
        )
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return {
        "document": document.to_json(),
        "metrics": {
            "total_sqft": round(document.total_sqft, 2),
            "room_count": len(document.rooms),
        },
        "disclosure": FLOORPLAN_AI_DISCLOSURE,
        "saved": False,
    }


MAX_PLAN_IMAGE_BYTES = 25 * 1024 * 1024


@router.post("/crm/floorplan/extract-image")
async def extract_image(
    file: UploadFile = File(...),
    metres_per_pixel: Optional[float] = Query(default=None, gt=0, le=1),
    known_total_sqft: Optional[float] = Query(default=None, gt=0, le=100_000),
    wall_height_m: float = Query(default=2.5, gt=0, le=30),
    lead_id: Optional[UUID] = Query(default=None),
    listing_id: Optional[UUID] = Query(default=None),
    ctx: TenantContext = Depends(require_context),
):
    """Extract a floor plan from an uploaded raster floor-plan image.

    Requires a scale reference — either metres_per_pixel or known_total_sqft.
    Without one this 422s rather than guessing: a wrong scale multiplies every
    rehab line item by a constant while looking entirely plausible.

    Returns the document WITHOUT saving. The agent reviews it in the editor and
    saves explicitly — machine output never writes itself into underwriting.

    This is a CPU-bound OpenCV pass (~1-3 s on a large scan), so it runs in a
    worker thread; doing it inline would stall the event loop for every other
    request on this worker.
    """
    _subject_or_422(lead_id, listing_id)

    data = await file.read(MAX_PLAN_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty file.")
    if len(data) > MAX_PLAN_IMAGE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Floor-plan image exceeds the {MAX_PLAN_IMAGE_BYTES // (1024 * 1024)} MB limit.",
        )

    async with tenant_tx(ctx) as conn:
        await _assert_subject_exists(conn, lead_id, listing_id)

    from floorplan_pipeline import extract_from_floorplan_image
    from floorplan_pipeline.errors import ExtractionError, MissingScale

    try:
        document = await asyncio.to_thread(
            extract_from_floorplan_image,
            data,
            metres_per_pixel=metres_per_pixel,
            known_total_sqft=known_total_sqft,
            wall_height_m=wall_height_m,
        )
    except MissingScale as exc:
        # Distinct from other extraction failures: the operator can fix this by
        # supplying a scale, so say exactly that.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        # opencv/numpy absent — the pipeline imports lazily so the rest of the
        # API keeps working; this endpoint degrades honestly instead of 500ing.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return {
        "document": document.to_json(),
        "metrics": {
            "total_sqft": round(document.total_sqft, 2),
            "room_count": len(document.rooms),
            "wall_count": len(document.walls),
            "opening_count": len(document.openings),
        },
        "confidence": document.provenance.confidence,
        "disclosure": FLOORPLAN_AI_DISCLOSURE,
        "saved": False,
    }


@router.get("/crm/floorplan/{floorplan_id}/revisions")
async def list_revisions(
    floorplan_id: UUID,
    limit: int = Query(default=25, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    """Revision history (metadata only — fetch a document via ?revision=)."""
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT revision, total_sqft, wall_linear_ft, created_by, created_at
              FROM property_floorplan_revisions
             WHERE floorplan_id = $1
             ORDER BY revision DESC
             LIMIT $2
            """,
            floorplan_id, limit,
        )
    return {
        "revisions": [
            {
                "revision": r["revision"],
                "total_sqft": float(r["total_sqft"]),
                "wall_linear_ft": float(r["wall_linear_ft"]),
                "created_by": str(r["created_by"]) if r["created_by"] else None,
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }
