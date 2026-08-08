"""Parcel / building-footprint vectors → a floor-plan shell.

Input is a GeoJSON Polygon in WGS84 (county GIS building outlines, Microsoft
Building Footprints, OSM `building=*` ways). Output is an exterior wall loop
and one unnamed zone.

HONEST CEILING: a footprint contains zero interior information. This produces
the outer shell and nothing else. It does not guess room counts, does not
subdivide, and does not use bed/bath counts from the MLS record to invent a
layout — a fabricated interior would flow into the rehab estimate as though it
were measured. The agent draws the interior in the editor, on top of a shell
whose exterior dimensions are genuinely accurate.
"""

from __future__ import annotations

import math
import uuid
from typing import Any, Optional, Sequence

from .errors import DegenerateGeometry
from .schema import (
    FloorplanDocument,
    FloorplanLevel,
    FloorplanRoom,
    FloorplanWall,
    Provenance,
    Point2D,
)

EARTH_RADIUS_M = 6378137.0

# Exterior walls on a residential structure are typically 150–250 mm.
DEFAULT_EXTERIOR_THICKNESS_M = 0.2


def _to_local_metres(ring: Sequence[Sequence[float]]) -> list[Point2D]:
    """Project lon/lat degrees to metres in a local tangent plane.

    Equirectangular about the ring's centroid. Over a single building (tens of
    metres) the distortion is far below the accuracy of the source footprint,
    and it avoids a pyproj dependency.
    """
    lons = [float(p[0]) for p in ring]
    lats = [float(p[1]) for p in ring]
    lon0 = sum(lons) / len(lons)
    lat0 = sum(lats) / len(lats)
    cos_lat0 = math.cos(math.radians(lat0))

    return [
        (
            math.radians(lon - lon0) * EARTH_RADIUS_M * cos_lat0,
            math.radians(lat - lat0) * EARTH_RADIUS_M,
        )
        for lon, lat in zip(lons, lats)
    ]


def _simplify(points: list[Point2D], tolerance: float) -> list[Point2D]:
    """Douglas–Peucker. Footprint rings carry many near-collinear vertices from
    digitisation; without this a simple rectangle arrives as 40 walls."""
    if len(points) < 3:
        return points

    def rdp(pts: list[Point2D]) -> list[Point2D]:
        if len(pts) < 3:
            return pts
        start, end = pts[0], pts[-1]
        dx, dy = end[0] - start[0], end[1] - start[1]
        denom = math.hypot(dx, dy)

        max_distance = -1.0
        index = 0
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            if denom == 0:
                distance = math.dist((px, py), start)
            else:
                distance = abs(dy * px - dx * py + end[0] * start[1] - end[1] * start[0]) / denom
            if distance > max_distance:
                max_distance, index = distance, i

        if max_distance <= tolerance:
            return [start, end]
        return rdp(pts[: index + 1])[:-1] + rdp(pts[index:])

    return rdp(points)


def extract_from_parcel_geometry(
    geojson_geometry: dict[str, Any],
    *,
    level_name: str = "Ground Floor",
    wall_height_m: float = 2.5,
    simplify_tolerance_m: float = 0.35,
    model_version: str = "floorplan-parcel-1.0.0",
    notes: Optional[str] = None,
) -> FloorplanDocument:
    """Build an exterior-shell FloorplanDocument from a GeoJSON footprint."""
    geometry_type = (geojson_geometry or {}).get("type")
    coordinates = (geojson_geometry or {}).get("coordinates")

    if geometry_type == "Polygon":
        ring = coordinates[0] if coordinates else None
    elif geometry_type == "MultiPolygon":
        # Largest ring by vertex count is the primary structure; outbuildings
        # (sheds, detached garages) are not the subject of the plan.
        if not coordinates:
            ring = None
        else:
            ring = max((poly[0] for poly in coordinates if poly), key=len, default=None)
    else:
        raise DegenerateGeometry(
            f"Expected a GeoJSON Polygon or MultiPolygon, got {geometry_type!r}."
        )

    if not ring or len(ring) < 4:
        raise DegenerateGeometry("Footprint ring has too few vertices to form a building.")

    points = _to_local_metres(ring)
    # GeoJSON rings are closed (last == first); drop the duplicate before
    # simplifying so the closing wall isn't emitted twice.
    if points and math.dist(points[0], points[-1]) < 1e-6:
        points = points[:-1]

    points = _simplify(points, simplify_tolerance_m)
    if len(points) < 3:
        raise DegenerateGeometry("Footprint collapsed to fewer than three corners.")

    level = FloorplanLevel(id=f"level_{uuid.uuid4().hex[:12]}", name=level_name, index=0)

    walls: list[FloorplanWall] = []
    for i in range(len(points)):
        start = points[i]
        end = points[(i + 1) % len(points)]
        if math.dist(start, end) < 0.05:
            continue
        walls.append(FloorplanWall(
            id=f"wall_{uuid.uuid4().hex[:12]}",
            start=(round(start[0], 4), round(start[1], 4)),
            end=(round(end[0], 4), round(end[1], 4)),
            thickness=DEFAULT_EXTERIOR_THICKNESS_M,
            height=wall_height_m,
            levelId=level.id,
            interior=False,
        ))

    if len(walls) < 3:
        raise DegenerateGeometry("Could not form a closed exterior wall loop.")

    shell = FloorplanRoom(
        id=f"zone_{uuid.uuid4().hex[:12]}",
        name="Building footprint",
        type="other",
        polygon=[(round(x, 4), round(y, 4)) for x, y in points],
        levelId=level.id,
        boundaryWallIds=[wall.id for wall in walls],
    )

    return FloorplanDocument(
        provenance=Provenance(
            source="parcel_vector",
            ai_generated=True,
            model_version=model_version,
            # Exterior geometry is genuinely accurate; the document as a whole
            # is not a floor plan, which is what this score communicates.
            confidence=0.55,
            notes=notes or (
                "Exterior building footprint from parcel GIS data. Exterior "
                "dimensions are accurate; NO interior walls or rooms are "
                "included, because a footprint contains no interior information. "
                "Draw the interior in the editor."
            ),
        ),
        levels=[level],
        walls=walls,
        rooms=[shell],
        openings=[],
    )
