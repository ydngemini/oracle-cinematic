"""Complete-dimension resolution: every number needed to construct a building,
inside and outside, with nothing left blank.

The rule this module lives by: **a field is never None — and never silently
invented either.** Each value carries its provenance:

    measured    read from geometry a source actually measured (a footprint ring)
    sourced     read from a data source's attribute (OSM building:levels)
    estimated   derived from sourced data by a stated rule (beds from area)
    default     a stated fallback used because nothing better existed

That labelling is what makes "nothing blank" compatible with this pipeline's
long-standing refusal to fabricate: the agent (and the underwriting trail) can
always see which numbers came from the world and which came from a rule.

Interior scaffolding is deliberately conservative. When the footprint is
roughly rectangular, rooms are laid out in a simple two-band grid the agent can
drag into shape. When it is not (L-shapes, courtyards), the interior is left as
ONE room covering the shell with the bed/bath programme reported in the
manifest — a wrong-but-plausible grid on an L-shape reads as truth, and that is
exactly the failure mode this pipeline exists to avoid.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .parcel import extract_from_parcel_geometry
from .schema import (
    FloorplanDocument,
    FloorplanLevel,
    FloorplanOpening,
    FloorplanRoom,
    FloorplanWall,
    Point2D,
)

logger = logging.getLogger("oracle.floorplan.dimensions")

MODEL_VERSION = "auto-dimensions-1"

# US residential defaults, stated once so every basis string can cite them.
DEFAULT_STOREY_HEIGHT_M = 2.5
DEFAULT_EXTERIOR_WALL_THICKNESS_M = 0.2
DEFAULT_INTERIOR_WALL_THICKNESS_M = 0.1
DEFAULT_FOOTPRINT_AREA_M2 = 140.0          # ~1500 sqft, a median US single-family plate
M2_PER_BEDROOM = 55.0                      # living area per bedroom, US average
RECTANGULARITY_THRESHOLD = 0.78            # bbox fill ratio above which a grid scaffold is honest
DOOR_WIDTH_M = 0.9
DOOR_HEIGHT_M = 2.0
WINDOW_WIDTH_M = 1.2
WINDOW_HEIGHT_M = 1.2
PERIMETER_M_PER_WINDOW = 5.0               # one window per ~5 m of exterior wall


@dataclass(slots=True)
class DimensionValue:
    """One resolved number and the story of where it came from."""

    value: float | int
    unit: str
    provenance: str      # measured | sourced | estimated | default
    basis: str

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit,
                "provenance": self.provenance, "basis": self.basis}


@dataclass(slots=True)
class DimensionManifest:
    """Every dimension the construction needs, each one filled and attributed."""

    footprint_area_m2: DimensionValue
    footprint_perimeter_m: DimensionValue
    levels: DimensionValue
    storey_height_m: DimensionValue
    wall_height_m: DimensionValue
    total_height_m: DimensionValue
    exterior_wall_thickness_m: DimensionValue
    interior_wall_thickness_m: DimensionValue
    bedrooms: DimensionValue
    bathrooms: DimensionValue
    doors: DimensionValue
    windows: DimensionValue
    total_floor_area_m2: DimensionValue
    total_floor_area_sqft: DimensionValue
    fields: dict[str, DimensionValue] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload = {
            name: getattr(self, name).to_json()
            for name in (
                "footprint_area_m2", "footprint_perimeter_m", "levels",
                "storey_height_m", "wall_height_m", "total_height_m",
                "exterior_wall_thickness_m", "interior_wall_thickness_m",
                "bedrooms", "bathrooms", "doors", "windows",
                "total_floor_area_m2", "total_floor_area_sqft",
            )
        }
        payload.update({k: v.to_json() for k, v in self.fields.items()})
        return payload

    def estimated_fields(self) -> list[str]:
        """Names of every value that did not come from a measurement — the list
        the UI shows the agent so review effort lands where the guesses are."""
        out = []
        for name, dim in self.to_json().items():
            if dim["provenance"] in ("estimated", "default"):
                out.append(name)
        return out


def _polygon_area(points: list[Point2D]) -> float:
    if len(points) < 3:
        return 0.0
    twice = 0.0
    for i in range(len(points)):
        x1, y1 = points[i - 1]
        x2, y2 = points[i]
        twice += x1 * y2 - x2 * y1
    return abs(twice) / 2.0


def _polygon_perimeter(points: list[Point2D]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(
        math.dist(points[i - 1], points[i]) for i in range(len(points))
    )


def _bbox(points: list[Point2D]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    zs = [p[1] for p in points]
    return min(xs), min(zs), max(xs), max(zs)


def _default_footprint(area_m2: float) -> dict[str, Any]:
    """A square footprint of the given area, centred on the origin, in the fake
    lon/lat degrees extract_from_parcel_geometry expects (it projects to a local
    tangent plane, so tiny degree offsets at the equator are metres)."""
    side_m = math.sqrt(area_m2)
    half_deg_lat = (side_m / 2.0) / 111_132.0
    half_deg_lon = (side_m / 2.0) / 111_320.0
    ring = [
        [-half_deg_lon, -half_deg_lat],
        [half_deg_lon, -half_deg_lat],
        [half_deg_lon, half_deg_lat],
        [-half_deg_lon, half_deg_lat],
        [-half_deg_lon, -half_deg_lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _estimate_levels(area_m2: float) -> int:
    """One storey for a modest plate, two above ~180 m². Deliberately coarse —
    it exists to be overridden by sourced data, and its basis says so."""
    return 2 if area_m2 >= 180.0 else 1


def _room_programme(total_area_m2: float) -> tuple[int, int]:
    beds = max(1, min(6, round(total_area_m2 / M2_PER_BEDROOM)))
    baths = max(1, beds // 2)
    return beds, baths


def _scaffold_rooms(
    document: FloorplanDocument,
    shell: list[Point2D],
    beds: int,
    baths: int,
    interior_thickness: float,
    wall_height: float,
) -> bool:
    """Grid-scaffold the interior when the shell is honest to grid.

    Returns True when a scaffold was drawn, False when the shape forced the
    single-room fallback. Two bands: living/kitchen across the top, sleeping
    band along the bottom with bedrooms then bathrooms."""
    min_x, min_z, max_x, max_z = _bbox(shell)
    width = max_x - min_x
    depth = max_z - min_z
    if width <= 0 or depth <= 0:
        return False

    fill = _polygon_area(shell) / (width * depth)
    if fill < RECTANGULARITY_THRESHOLD:
        # Non-rectangular plate: one honest room over the whole shell.
        document.rooms.append(FloorplanRoom(
            id="room_interior",
            name="Interior (partition to suit)",
            type="other",
            polygon=list(shell),
            levelId=document.levels[0].id if document.levels else None,
        ))
        return False

    inset = interior_thickness
    ix0, iz0 = min_x + inset, min_z + inset
    ix1, iz1 = max_x - inset, max_z - inset
    level_id = document.levels[0].id if document.levels else None

    # Band split: living gets the larger share of depth.
    band_z = iz0 + (iz1 - iz0) * 0.45

    def _rect(x0: float, z0: float, x1: float, z1: float) -> list[Point2D]:
        return [(x0, z0), (x1, z0), (x1, z1), (x0, z1)]

    # Living band: living room + kitchen split 60/40.
    kitchen_x = ix0 + (ix1 - ix0) * 0.6
    document.rooms.append(FloorplanRoom(
        id="room_living", name="Living", type="living",
        polygon=_rect(ix0, iz0, kitchen_x, band_z), levelId=level_id,
    ))
    document.rooms.append(FloorplanRoom(
        id="room_kitchen", name="Kitchen", type="kitchen",
        polygon=_rect(kitchen_x, iz0, ix1, band_z), levelId=level_id,
    ))

    # Sleeping band: bedrooms side by side, bathrooms at the end.
    slots = beds + baths
    slot_w = (ix1 - ix0) / slots
    for i in range(beds):
        x0 = ix0 + slot_w * i
        document.rooms.append(FloorplanRoom(
            id=f"room_bed{i + 1}", name=f"Bedroom {i + 1}", type="bedroom",
            polygon=_rect(x0, band_z, x0 + slot_w, iz1), levelId=level_id,
        ))
    for i in range(baths):
        x0 = ix0 + slot_w * (beds + i)
        document.rooms.append(FloorplanRoom(
            id=f"room_bath{i + 1}", name=f"Bath {i + 1}", type="bathroom",
            polygon=_rect(x0, band_z, x0 + slot_w, iz1), levelId=level_id,
        ))

    # Interior walls: the band divider plus each slot divider.
    def _wall(wall_id: str, start: Point2D, end: Point2D) -> None:
        document.walls.append(FloorplanWall(
            id=wall_id, start=start, end=end,
            thickness=interior_thickness, height=wall_height,
            levelId=level_id, interior=True,
        ))

    _wall("iwall_band", (ix0, band_z), (ix1, band_z))
    _wall("iwall_kitchen", (kitchen_x, iz0), (kitchen_x, band_z))
    for i in range(1, slots):
        x = ix0 + slot_w * i
        _wall(f"iwall_slot{i}", (x, band_z), (x, iz1))
    return True


def complete_dimensions(
    *,
    footprint_geometry: Optional[dict[str, Any]] = None,
    footprint_source: str = "",
    sourced_levels: Optional[int] = None,
    sourced_area_m2: Optional[float] = None,
    wall_height_m: Optional[float] = None,
) -> tuple[FloorplanDocument, DimensionManifest]:
    """Resolve every construction dimension, filling gaps with labelled rules.

    `footprint_geometry` is GeoJSON (from the footprint resolver) when a real
    outline exists. Without one, a default square plate is used — and says so.
    """
    # --- footprint -----------------------------------------------------------
    if footprint_geometry:
        geometry = footprint_geometry
        footprint_prov = "measured"
        footprint_basis = f"building outline from {footprint_source or 'GIS source'}"
    elif sourced_area_m2 and sourced_area_m2 > 0:
        geometry = _default_footprint(sourced_area_m2)
        footprint_prov = "estimated"
        footprint_basis = (
            f"square plate of the assessor-reported {sourced_area_m2:.0f} m² — "
            "no outline was available, only the area"
        )
    else:
        geometry = _default_footprint(DEFAULT_FOOTPRINT_AREA_M2)
        footprint_prov = "default"
        footprint_basis = (
            f"no outline or area found; default {DEFAULT_FOOTPRINT_AREA_M2:.0f} m² "
            "square plate (≈ median US single-family)"
        )

    storey_height = float(wall_height_m or DEFAULT_STOREY_HEIGHT_M)
    storey_prov = "sourced" if wall_height_m else "default"

    document = extract_from_parcel_geometry(geometry, wall_height_m=storey_height)
    shell: list[Point2D] = []
    if document.rooms:
        shell = list(document.rooms[0].polygon)
    elif document.walls:
        shell = [w.start for w in document.walls]

    area_m2 = _polygon_area(shell) if shell else 0.0
    perimeter_m = _polygon_perimeter(shell) if shell else 0.0

    # --- levels --------------------------------------------------------------
    if sourced_levels and 1 <= sourced_levels <= 100:
        levels = int(sourced_levels)
        levels_prov, levels_basis = "sourced", "building:levels from the footprint source"
    else:
        levels = _estimate_levels(area_m2)
        levels_prov = "estimated"
        levels_basis = f"1 storey under 180 m² plate, 2 over (plate {area_m2:.0f} m²)"

    # Additional levels beyond the ground floor the shell came with.
    for index in range(1, levels):
        document.levels.append(FloorplanLevel(
            id=f"level_{index}", name=f"Level {index + 1}", index=index,
        ))

    # --- interior programme --------------------------------------------------
    total_area = area_m2 * levels
    beds, baths = _room_programme(total_area)

    document.rooms = []  # replace the bare shell room with the scaffold
    scaffolded = _scaffold_rooms(
        document, shell, beds, baths,
        DEFAULT_INTERIOR_WALL_THICKNESS_M, storey_height,
    ) if shell else False

    # --- openings ------------------------------------------------------------
    door_count = 1 + beds + baths            # entry + one per closable room
    window_count = max(2, round(perimeter_m / PERIMETER_M_PER_WINDOW))
    exterior_wall_ids = [w.id for w in document.walls if not w.interior]
    for i in range(door_count):
        document.openings.append(FloorplanOpening(
            id=f"door_{i}", kind="door",
            wallId=exterior_wall_ids[0] if i == 0 and exterior_wall_ids else None,
            width=DOOR_WIDTH_M, height=DOOR_HEIGHT_M,
        ))
    for i in range(window_count):
        document.openings.append(FloorplanOpening(
            id=f"window_{i}", kind="window",
            wallId=exterior_wall_ids[i % len(exterior_wall_ids)] if exterior_wall_ids else None,
            width=WINDOW_WIDTH_M, height=WINDOW_HEIGHT_M,
        ))

    # --- provenance ----------------------------------------------------------
    estimated_note = (
        "AUTO-DIMENSIONED: footprint "
        + footprint_prov
        + ("; interior scaffold estimated from floor area" if scaffolded
           else "; interior left as one room — plate too irregular for an honest grid")
        + f"; {beds} bed / {baths} bath programme is an estimate. Review before relying on it."
    )
    document.provenance.source = "parcel_vector" if footprint_geometry else "ai_vision"
    document.provenance.ai_generated = True
    document.provenance.model_version = MODEL_VERSION
    document.provenance.notes = estimated_note

    manifest = DimensionManifest(
        footprint_area_m2=DimensionValue(round(area_m2, 1), "m2", footprint_prov, footprint_basis),
        footprint_perimeter_m=DimensionValue(round(perimeter_m, 1), "m", footprint_prov, footprint_basis),
        levels=DimensionValue(levels, "count", levels_prov, levels_basis),
        storey_height_m=DimensionValue(
            storey_height, "m", storey_prov,
            "caller-provided wall height" if wall_height_m else f"US default {DEFAULT_STOREY_HEIGHT_M} m",
        ),
        wall_height_m=DimensionValue(storey_height, "m", storey_prov, "equal to storey height"),
        total_height_m=DimensionValue(
            round(storey_height * levels, 2), "m", "estimated", "storey height × levels",
        ),
        exterior_wall_thickness_m=DimensionValue(
            DEFAULT_EXTERIOR_WALL_THICKNESS_M, "m", "default", "US frame construction default",
        ),
        interior_wall_thickness_m=DimensionValue(
            DEFAULT_INTERIOR_WALL_THICKNESS_M, "m", "default", "US frame construction default",
        ),
        bedrooms=DimensionValue(
            beds, "count", "estimated", f"1 bedroom per {M2_PER_BEDROOM:.0f} m² of floor area",
        ),
        bathrooms=DimensionValue(baths, "count", "estimated", "1 bathroom per 2 bedrooms, min 1"),
        doors=DimensionValue(door_count, "count", "estimated", "entry + one per bed/bath"),
        windows=DimensionValue(
            window_count, "count", "estimated",
            f"one per {PERIMETER_M_PER_WINDOW:.0f} m of exterior perimeter, min 2",
        ),
        total_floor_area_m2=DimensionValue(
            round(total_area, 1), "m2", "estimated", "plate area × levels",
        ),
        total_floor_area_sqft=DimensionValue(
            round(total_area * 10.7639, 0), "sqft", "estimated", "plate area × levels, converted",
        ),
    )

    # Invariant: nothing blank. A None here is a bug, not a data condition.
    for name, dim in manifest.to_json().items():
        if dim["value"] is None:
            raise AssertionError(f"dimension {name} resolved to None — the contract is nothing blank")

    logger.info(
        "auto-dimensions: %s footprint, %d level(s), %d bed / %d bath, scaffolded=%s",
        footprint_prov, levels, beds, baths, scaffolded,
    )
    return document, manifest
