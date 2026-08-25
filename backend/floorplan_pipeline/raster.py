"""Raster floor-plan → structured geometry, using classical CV only.

No GPU, no model weights, no network. Runs in the existing backend container
given opencv-python-headless + numpy.

Pipeline, in order:
    binarise → isolate wall strokes → Hough segments → merge/snap into a wall
    graph → connected-component rooms → gap-detected openings → scale to metres

Every stage is a separate function so it can be unit tested against a fixture
image and tuned without touching the others.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Iterable, Optional

from .errors import DegenerateGeometry, MissingScale
from .schema import (
    FloorplanDocument,
    FloorplanLevel,
    FloorplanOpening,
    FloorplanRoom,
    FloorplanWall,
    Provenance,
    Point2D,
)

log = logging.getLogger("oracle.floorplan_pipeline.raster")

SQFT_PER_M2 = 10.763910416709722

# Typical residential dimensions, metres. Used only to CLASSIFY detected
# openings, never to invent them.
DOOR_WIDTH_RANGE = (0.65, 1.10)
WINDOW_WIDTH_RANGE = (0.45, 2.60)

# Tuning constants. Exposed as module-level so a per-market or per-source
# profile can override them without editing logic.
MIN_WALL_RUN_PX = 28        # Hough minLineLength — drops furniture glyphs
MAX_LINE_GAP_PX = 6         # bridges anti-aliasing breaks in a wall stroke
COLLINEAR_ANGLE_TOL = math.radians(4.0)
COLLINEAR_OFFSET_TOL_PX = 5.0
SNAP_RADIUS_PX = 9.0
MIN_ROOM_AREA_M2 = 1.2      # smaller than this is a closet artefact or noise


def _require_cv():
    """Import OpenCV lazily so the backend boots without it installed."""
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "Floor-plan extraction requires opencv-python-headless and numpy. "
            "Add them to backend/requirements.txt to enable this route."
        ) from exc
    return cv2, np


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def resolve_scale(
    *,
    metres_per_pixel: Optional[float],
    known_total_sqft: Optional[float],
    interior_pixel_area: Optional[float],
) -> float:
    """Determine metres-per-pixel, or refuse.

    Preference order:
      1. Explicit caller-supplied scale (a dimension the agent measured).
      2. Solve from a known total square footage (MLS record) against the
         measured interior pixel area.

    There is no fallback. A guessed scale produces a document that looks
    correct and is uniformly wrong, which would flow silently into MAO.
    """
    if metres_per_pixel and metres_per_pixel > 0:
        return float(metres_per_pixel)

    if known_total_sqft and known_total_sqft > 0 and interior_pixel_area and interior_pixel_area > 0:
        known_m2 = known_total_sqft / SQFT_PER_M2
        # area_m2 = area_px * (m/px)^2  ->  m/px = sqrt(area_m2 / area_px)
        return math.sqrt(known_m2 / interior_pixel_area)

    raise MissingScale(
        "Cannot convert this plan to real dimensions. Provide metres_per_pixel, "
        "or a known total square footage to solve scale against."
    )


# ---------------------------------------------------------------------------
# Stage 1 — wall mask
# ---------------------------------------------------------------------------

def build_wall_mask(image_bgr):
    """Isolate wall strokes from text, hatching, furniture, and dimension lines.

    Long-kernel morphological opening is the workhorse: walls are the only
    features that survive as continuous runs of ~30+ px in one axis, so this
    removes room labels, north arrows, and furniture symbols without any
    text detection.
    """
    cv2, np = _require_cv()

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    # Adaptive rather than global: scanned plans are unevenly lit, and a global
    # Otsu threshold loses whole wall runs on the shaded side of a photo.
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 12
    )

    # Kernel length scales with image size so the same constants work on a
    # 900px thumbnail and a 4000px scan.
    run = max(12, int(min(image_bgr.shape[:2]) * 0.035))
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (run, 1))
    )
    vertical = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, run))
    )

    mask = cv2.bitwise_or(horizontal, vertical)
    # Close small breaks where a door symbol interrupts a wall stroke.
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    )
    return mask


def estimate_wall_thickness_px(mask) -> float:
    """Median stroke width, via distance transform on the wall mask.

    Gives a real thickness per plan instead of assuming 0.1 m, which matters
    because thickness feeds nothing in cost today but does feed the 3D model
    the agent walks through.
    """
    cv2, np = _require_cv()
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ridge = distance[distance > 0]
    if ridge.size == 0:
        return 4.0
    # Distance transform gives half-width at the ridge; take a high percentile
    # to land on wall centres rather than edges.
    return float(np.percentile(ridge, 92) * 2.0)


# ---------------------------------------------------------------------------
# Stage 2 — segments
# ---------------------------------------------------------------------------

def detect_segments(mask) -> list[tuple[float, float, float, float]]:
    """Probabilistic Hough over the wall mask."""
    cv2, np = _require_cv()
    lines = cv2.HoughLinesP(
        mask,
        rho=1,
        theta=math.pi / 360,          # 0.5° resolution — plans are axis-heavy
        threshold=55,
        minLineLength=MIN_WALL_RUN_PX,
        maxLineGap=MAX_LINE_GAP_PX,
    )
    if lines is None:
        return []
    # OpenCV <5 returns shape (N, 1, 4); OpenCV 5 returns (N, 4). Normalise so
    # this works across both without pinning a major version.
    flat = np.asarray(lines).reshape(-1, 4)
    return [(float(x1), float(y1), float(x2), float(y2)) for x1, y1, x2, y2 in flat]


def _angle_of(seg) -> float:
    x1, y1, x2, y2 = seg
    return math.atan2(y2 - y1, x2 - x1) % math.pi


def _point_line_distance(px, py, seg) -> float:
    x1, y1, x2, y2 = seg
    dx, dy = x2 - x1, y2 - y1
    denom = math.hypot(dx, dy)
    if denom == 0:
        return math.dist((px, py), (x1, y1))
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / denom


def merge_collinear(segments: list, angle_tol=COLLINEAR_ANGLE_TOL,
                    offset_tol=COLLINEAR_OFFSET_TOL_PX) -> list:
    """Collapse Hough's many fragments per wall into one segment each.

    Hough returns overlapping duplicates along a single wall; without this the
    total linear footage — which drives framing and drywall cost — is inflated
    several-fold. This is the single most cost-relevant cleanup step.
    """
    merged: list[list[float]] = []
    for seg in sorted(segments, key=lambda s: -math.dist((s[0], s[1]), (s[2], s[3]))):
        angle = _angle_of(seg)
        placed = False
        for group in merged:
            if abs((_angle_of(group) - angle + math.pi / 2) % math.pi - math.pi / 2) > angle_tol:
                continue
            if (_point_line_distance(seg[0], seg[1], group) > offset_tol
                    or _point_line_distance(seg[2], seg[3], group) > offset_tol):
                continue
            # Same wall: extend the group to the extreme endpoints along its axis.
            points = [(group[0], group[1]), (group[2], group[3]),
                      (seg[0], seg[1]), (seg[2], seg[3])]
            ux, uy = math.cos(angle), math.sin(angle)
            projections = [(p[0] * ux + p[1] * uy, p) for p in points]
            lo = min(projections, key=lambda t: t[0])[1]
            hi = max(projections, key=lambda t: t[0])[1]
            group[0], group[1], group[2], group[3] = lo[0], lo[1], hi[0], hi[1]
            placed = True
            break
        if not placed:
            merged.append(list(seg))
    return [tuple(g) for g in merged]


def snap_endpoints(segments: list, radius=SNAP_RADIUS_PX) -> list:
    """Weld near-coincident endpoints so walls form a closed graph.

    Rooms are only detectable as enclosed regions if wall ends actually meet;
    a 3px gap at a corner leaks the flood fill into the next room and merges
    two rooms into one.
    """
    nodes: list[list[float]] = []

    def node_for(x: float, y: float) -> int:
        for index, node in enumerate(nodes):
            if math.dist((x, y), (node[0], node[1])) <= radius:
                # Running mean keeps a corner shared by 3 walls centred.
                node[2] += 1
                node[0] += (x - node[0]) / node[2]
                node[1] += (y - node[1]) / node[2]
                return index

        nodes.append([x, y, 1])
        return len(nodes) - 1

    indexed = [(node_for(s[0], s[1]), node_for(s[2], s[3])) for s in segments]
    out = []
    for a, b in indexed:
        if a == b:
            continue  # collapsed to a point — not a wall
        out.append((nodes[a][0], nodes[a][1], nodes[b][0], nodes[b][1]))
    return out


# ---------------------------------------------------------------------------
# Stage 3 — rooms
# ---------------------------------------------------------------------------

def render_wall_graph(segments, shape, thickness_px: float):
    """Rasterise the merged wall graph as solid strokes.

    Rooms are segmented from THIS rather than the raw mask. Two reasons:
      * Door and window gaps are already bridged, because the wall runs either
        side of an opening merge into one collinear segment. Segmenting the raw
        mask instead leaks the flood fill through every doorway and returns the
        whole floor as a single room.
      * Detection noise (furniture outlines, dimension ticks) never made it
        into the graph, so it cannot carve phantom rooms.
    """
    cv2, np = _require_cv()
    canvas = np.zeros(shape[:2], np.uint8)
    stroke = max(2, int(round(thickness_px)))
    for x1, y1, x2, y2 in segments:
        cv2.line(canvas, (int(round(x1)), int(round(y1))),
                 (int(round(x2)), int(round(y2))), 255, stroke)
    return canvas


def detect_rooms(mask, scale: float, min_area_m2=MIN_ROOM_AREA_M2):
    """Connected components of the non-wall interior → room polygons.

    `mask` should be a sealed wall rendering (see render_wall_graph), not the
    raw stroke mask.

    Returns (polygons_in_metres, total_interior_pixel_area).
    """
    cv2, np = _require_cv()

    # Thicken walls slightly before inverting so hairline gaps don't leak.
    sealed = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)
    interior = cv2.bitwise_not(sealed)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(interior, connectivity=4)

    height, width = interior.shape
    polygons: list[list[Point2D]] = []
    interior_pixels = 0

    for label in range(1, count):
        x, y, w, h, area = stats[label]
        # The exterior background is the component touching the image border
        # with a large area; skip it or every plan gets one giant "room".
        touches_border = x <= 1 or y <= 1 or (x + w) >= width - 1 or (y + h) >= height - 1
        if touches_border and area > (width * height) * 0.20:
            continue

        area_m2 = area * scale * scale
        if area_m2 < min_area_m2:
            continue

        component = (labels == label).astype(np.uint8) * 255
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        # Simplify to the room's real corners; ε proportional to perimeter so
        # it adapts to room size rather than a fixed pixel budget.
        epsilon = 0.012 * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        if len(approx) < 3:
            continue

        polygons.append([(float(p[0][0]) * scale, float(p[0][1]) * scale) for p in approx])
        interior_pixels += int(area)

    return polygons, interior_pixels


# ---------------------------------------------------------------------------
# Stage 4 — openings
# ---------------------------------------------------------------------------

def detect_openings(
    walls: list[FloorplanWall],
    mask,
    scale: float,
) -> list[FloorplanOpening]:
    """Find gaps along wall runs and classify them as doors or windows.

    Walks each wall's centreline sampling the wall mask; a contiguous run of
    'no wall pixel here' bounded by wall on both sides is an opening. Widths
    outside plausible residential ranges are discarded rather than forced into
    a category — an unclassifiable gap is more likely a detection artefact.
    """
    cv2, np = _require_cv()
    height, width = mask.shape
    openings: list[FloorplanOpening] = []

    for wall in walls:
        (x1, y1), (x2, y2) = wall.start, wall.end
        # Back to pixel space for sampling.
        px1, py1 = x1 / scale, y1 / scale
        px2, py2 = x2 / scale, y2 / scale
        length_px = math.hypot(px2 - px1, py2 - py1)
        if length_px < 8:
            continue

        steps = int(length_px)
        occupied: list[bool] = []
        for i in range(steps + 1):
            t = i / steps
            sx, sy = int(round(px1 + (px2 - px1) * t)), int(round(py1 + (py2 - py1) * t))
            if not (0 <= sx < width and 0 <= sy < height):
                occupied.append(True)
                continue
            # Sample a 3x3 neighbourhood: the centreline can drift a pixel off a
            # thick stroke, which would read as a false gap. Keep it tight —
            # every extra pixel of radius shortens every measured opening by
            # 2px, which pushed doors into the window size class.
            lo_y, hi_y = max(0, sy - 1), min(height, sy + 2)
            lo_x, hi_x = max(0, sx - 1), min(width, sx + 2)
            occupied.append(bool(mask[lo_y:hi_y, lo_x:hi_x].any()))

        # Extract interior runs of False bounded by True on both sides.
        run_start: Optional[int] = None
        for i, is_wall in enumerate(occupied):
            if not is_wall and run_start is None:
                run_start = i
            elif is_wall and run_start is not None:
                if run_start > 0:  # bounded on the left
                    gap_px = i - run_start
                    gap_m = gap_px * scale
                    kind = None
                    if DOOR_WIDTH_RANGE[0] <= gap_m <= DOOR_WIDTH_RANGE[1]:
                        kind = "door"
                    elif WINDOW_WIDTH_RANGE[0] <= gap_m <= WINDOW_WIDTH_RANGE[1]:
                        kind = "window"
                    if kind:
                        openings.append(FloorplanOpening(
                            id=f"{kind}_{uuid.uuid4().hex[:12]}",
                            kind=kind,
                            wallId=wall.id,
                            width=round(gap_m, 3),
                            # Standard heights. The plan is 2D — height is not
                            # measurable from it, so these are declared
                            # assumptions, not measurements.
                            height=2.03 if kind == "door" else 1.20,
                        ))
                run_start = None

    return openings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def extract_from_floorplan_image(
    image_bytes: bytes,
    *,
    metres_per_pixel: Optional[float] = None,
    known_total_sqft: Optional[float] = None,
    level_name: str = "Ground Floor",
    level_index: int = 0,
    wall_height_m: float = 2.5,
    model_version: str = "floorplan-cv-1.0.0",
) -> FloorplanDocument:
    """Extract a FloorplanDocument from a raster floor-plan image.

    Raises MissingScale when pixels cannot be converted to metres, and
    DegenerateGeometry when the image yields too little structure to persist.
    """
    cv2, np = _require_cv()

    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise DegenerateGeometry("Could not decode the uploaded image.")

    mask = build_wall_mask(image)
    thickness_px = estimate_wall_thickness_px(mask)

    segments = detect_segments(mask)
    # Hough fires on BOTH edges of every wall stroke, so the two sides of one
    # wall arrive as separate parallel segments. Left unmerged they double the
    # linear footage — and linear footage is billed directly as framing and
    # drywall. The offset tolerance must therefore exceed the wall thickness,
    # not a fixed pixel count.
    segments = merge_collinear(
        segments,
        offset_tol=max(COLLINEAR_OFFSET_TOL_PX, thickness_px * 1.35),
    )
    segments = snap_endpoints(segments, radius=max(SNAP_RADIUS_PX, thickness_px * 1.5))

    # Room segmentation runs on the sealed graph so doorways don't merge rooms.
    sealed = render_wall_graph(segments, image.shape, thickness_px)

    # Interior pixel area at unit scale is what lets a known square footage
    # solve for metres-per-pixel.
    _, interior_pixels = detect_rooms(sealed, scale=1.0)
    scale = resolve_scale(
        metres_per_pixel=metres_per_pixel,
        known_total_sqft=known_total_sqft,
        interior_pixel_area=interior_pixels,
    )

    thickness_m = max(0.05, thickness_px * scale)

    level = FloorplanLevel(id=f"level_{uuid.uuid4().hex[:12]}", name=level_name, index=level_index)

    walls = [
        FloorplanWall(
            id=f"wall_{uuid.uuid4().hex[:12]}",
            start=(round(x1 * scale, 4), round(y1 * scale, 4)),
            end=(round(x2 * scale, 4), round(y2 * scale, 4)),
            thickness=round(thickness_m, 4),
            height=wall_height_m,
            levelId=level.id,
            # Interiority is resolved client-side from zone boundary references;
            # the CV pass has no reliable signal for it.
            interior=False,
        )
        for (x1, y1, x2, y2) in segments
    ]

    polygons, _ = detect_rooms(sealed, scale=scale)
    rooms = [
        FloorplanRoom(
            id=f"zone_{uuid.uuid4().hex[:12]}",
            # No OCR pass, so room names are unknown. 'other' costs conservatively
            # and prompts the agent to type each room in the editor.
            name=f"Room {index + 1}",
            type="other",
            polygon=[(round(px, 4), round(py, 4)) for px, py in polygon],
            levelId=level.id,
        )
        for index, polygon in enumerate(polygons)
    ]

    if not walls or not rooms:
        raise DegenerateGeometry(
            "No usable walls or rooms were detected. This is usually a stylised "
            "marketing render rather than a dimensioned floor plan."
        )

    openings = detect_openings(walls, mask, scale)

    document = FloorplanDocument(
        provenance=Provenance(
            source="ai_vision",
            ai_generated=True,
            model_version=model_version,
            confidence=_confidence(walls, rooms, explicit_scale=metres_per_pixel is not None),
            notes=(
                "Extracted from a raster floor plan with classical CV. Room names "
                "and types were not detected and default to 'other'. Wall and "
                "opening heights are standard assumptions, not measurements."
            ),
        ),
        levels=[level],
        walls=walls,
        rooms=rooms,
        openings=openings,
    )

    log.info(
        "floorplan extracted: %d walls, %d rooms, %d openings, %.0f sqft (scale=%.5f m/px)",
        len(walls), len(rooms), len(openings), document.total_sqft, scale,
    )
    return document


#: How hard a room outline is simplified, as a fraction of its perimeter.
#:
#: `detect_rooms` uses 0.012, which is right for a DRAWN plan: the true shape is
#: rectilinear and the job is to find its corners through rasterisation noise.
#: A room outline traced from measured floor occupancy is not rectilinear, and
#: 0.012 of a 20 m perimeter is a 24 cm tolerance that takes the alcoves off —
#: measured, it cost 24% of the area on a real scanned room.
MEASURED_OUTLINE_EPSILON = 0.004


def _rooms_from_sealed_mask(mask, scale: float) -> list[list[Point2D]]:
    """Interior components of a sealed mask, simplified gently.

    Deliberately not `detect_rooms`: that stage is tuned for drawn plans and is
    shared with the image path, and the last time a shared stage was tuned for
    one caller it fired on every mode and made all of them worse. Same rules —
    skip the background component, drop anything under the minimum room area —
    with a tolerance that suits a traced outline instead of a drafted one.
    """
    cv2, np = _require_cv()

    interior = cv2.bitwise_not(mask)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(interior, connectivity=4)
    height, width = interior.shape

    polygons: list[list[Point2D]] = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        touches_border = x <= 1 or y <= 1 or (x + w) >= width - 1 or (y + h) >= height - 1
        if touches_border and area > (width * height) * 0.20:
            continue
        if area * scale * scale < MIN_ROOM_AREA_M2:
            continue
        component = (labels == label).astype(np.uint8) * 255
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        approx = cv2.approxPolyDP(
            contour, MEASURED_OUTLINE_EPSILON * cv2.arcLength(contour, True), True
        )
        if len(approx) < 3:
            continue
        polygons.append([(float(p[0][0]) * scale, float(p[0][1]) * scale) for p in approx])
    return polygons


def extract_from_wall_mask(
    mask, *, metres_per_pixel: float, level_name: str = "Ground Floor",
    level_index: int = 0, wall_height_m: float = 2.5,
    model_version: str = "floorplan-mask-1.0.0", notes: str = "",
) -> FloorplanDocument:
    """A document from an already-sealed wall mask, skipping the drawn-plan pass.

    `extract_from_floorplan_image` is built for a PICTURE of a floor plan, and
    two of its stages actively destroy a mask that was constructed rather than
    photographed:

      * `build_wall_mask` re-derives walls with a long-kernel opening, which
        removes a drawn outline — a 5 px one left 2.34 m² of a 16 m² room;
      * `render_wall_graph` rebuilds the plan from HOUGH LINE SEGMENTS, and the
        boundary of a real scanned room is not a set of straight lines.

    Both assumptions are correct for their input and wrong for this one. So this
    is a sibling path rather than a flag on that one: nothing here touches the
    image pipeline, because the last time a change was made inside a shared
    stage it fired on every mode and made all of them worse.

    Walls come from the room boundaries themselves. That is the honest source
    when the mask was built from measured occupancy — a room's edge is where the
    floor stopped, and no line-fitting step has been asked to invent a
    rectilinear version of it.
    """
    cv2, np = _require_cv()

    scale = float(metres_per_pixel)
    if scale <= 0:
        raise MissingScale("A wall mask carries no scale of its own; supply metres_per_pixel.")

    polygons = _rooms_from_sealed_mask(mask, scale)
    if not polygons:
        raise DegenerateGeometry(
            "The sealed mask enclosed no rooms. Either the floor never closed, "
            "or everything inside it was classified as wall."
        )

    # Thickness from the mask itself, the same measure the image path uses.
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ridge = distance[distance > 0]
    thickness_px = float(np.percentile(ridge, 92) * 2.0) if ridge.size else 4.0
    thickness_m = max(0.05, thickness_px * scale)

    level = FloorplanLevel(
        id=f"level_{uuid.uuid4().hex[:12]}", name=level_name, index=level_index
    )
    rooms = [
        FloorplanRoom(
            id=f"zone_{uuid.uuid4().hex[:12]}",
            name=f"Room {index + 1}",
            type="other",
            polygon=[(round(px, 4), round(py, 4)) for px, py in polygon],
            levelId=level.id,
        )
        for index, polygon in enumerate(polygons)
    ]

    # One wall per room boundary edge. Shared partitions appear from both sides
    # and are deduplicated on their midpoint, so linear footage — which is
    # billed directly as framing and drywall — is not doubled.
    walls: list[FloorplanWall] = []
    seen: set[tuple[int, int, int]] = set()
    for polygon in polygons:
        for index in range(len(polygon)):
            start, end = polygon[index - 1], polygon[index]
            length = math.dist(start, end)
            if length < thickness_m:
                continue
            key = (
                int(round((start[0] + end[0]) / 2 / max(thickness_m, 1e-6))),
                int(round((start[1] + end[1]) / 2 / max(thickness_m, 1e-6))),
                int(round(math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) / 15)) % 12,
            )
            if key in seen:
                continue
            seen.add(key)
            walls.append(FloorplanWall(
                id=f"wall_{uuid.uuid4().hex[:12]}",
                start=(round(start[0], 4), round(start[1], 4)),
                end=(round(end[0], 4), round(end[1], 4)),
                thickness=round(thickness_m, 4),
                height=wall_height_m,
                levelId=level.id,
                interior=False,
            ))

    if not walls:
        raise DegenerateGeometry("The mask enclosed rooms with no measurable boundary.")

    document = FloorplanDocument(
        provenance=Provenance(
            source="ai_vision",
            ai_generated=True,
            model_version=model_version,
            confidence=_confidence(walls, rooms, explicit_scale=True),
            notes=" ".join(filter(None, [
                "Rooms bounded by the measured floor rather than by closing walls. "
                "Openings were not detected: this path has no stroke to read a "
                "door out of.",
                notes,
            ])),
        ),
        levels=[level],
        walls=walls,
        rooms=rooms,
        openings=[],
    )
    log.info(
        "mask extraction: %d walls, %d rooms, %.0f sqft (scale=%.5f m/px)",
        len(walls), len(rooms), document.total_sqft, scale,
    )
    return document


def _confidence(walls, rooms, *, explicit_scale: bool) -> float:
    """A deliberately conservative self-assessment.

    Reported to the agent and stored on the row. Solved scale is materially
    less trustworthy than a measured one, so it caps lower.
    """
    score = 0.35
    if len(walls) >= 8:
        score += 0.15
    if len(rooms) >= 3:
        score += 0.15
    # A plan whose rooms are wildly uneven usually means the flood fill leaked.
    areas = sorted(room.area for room in rooms)
    if areas and areas[-1] < sum(areas) * 0.6:
        score += 0.10
    score += 0.15 if explicit_scale else 0.0
    return round(min(score, 0.85), 3)
