"""Reconstruction point cloud → top-down wall raster → the existing extractor.

This is the fourth input class (see the package docstring): a 3D reconstruction
of the property, produced by the COLMAP + splatfacto pipeline from ordinary
photographs. No phone scan, no LiDAR, no depth sensor — the geometry comes from
the same photos an agent already uploads.

Why this exists as only a *front half*: `raster.py` already turns a top-down
wall image into a FloorplanDocument — binarise, Hough, merge/snap, wall graph,
connected-component rooms, openings, scale. That machinery is tested and
tuned. So the job here is narrow: project a point cloud into the image that
pipeline already consumes, and be honest about scale.

Three decisions in here came out of the 2026-08-23 research
([[2026-08-23 — splat-derived-floorplans-interior-exterior]]) and are the
opposite of the obvious choice, so they are documented at their call sites:

  1. **The slice band is chosen from the data, and prefers the ceiling.**
     Furniture stands on the floor; the space near the ceiling is usually free.
     So the vertical distribution *is* the furniture filter. The obvious pick —
     waist height, "above the sofa, below the cabinets" — cuts straight through
     kitchen islands, wardrobes and bookcases, and every one of those becomes a
     wall.

  2. **Scale is never invented.** A reconstruction from images has no metric
     scale at all; this is inherent to structure-from-motion, not a tuning
     failure. A guessed metres-per-unit multiplies every length and area in the
     rehab estimate by an arbitrary constant and looks entirely plausible.
     `resolve_scale` in raster.py already refuses; this module hands it a real
     anchor or lets it refuse.

  3. **The exterior does not come from here.** Footprints from photogrammetric
     point clouds run above 90% accurate; Oracle's parcel vectors are exact.
     This module classifies which of ITS walls are exterior (the outer contour)
     so the interior/exterior split is right, but the authoritative building
     outline stays `parcel.py`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal, Optional

from .errors import DegenerateGeometry, MissingScale, UnsupportedInput

log = logging.getLogger("oracle.floorplan.slicing")

#: Below this the gaussian is a floater rather than surface. Reconstructions
#: carry a haze of low-opacity blobs in empty space; projected top-down they
#: smear into wall-shaped noise.
MIN_OPACITY = 0.35

#: Target width of the generated raster. Large enough for Hough to resolve a
#: partition wall, small enough that the extractor stays sub-second.
RASTER_PX = 1024

#: Fraction of the floor-to-ceiling height to cut, and how far below the
#: ceiling to start. 0.80-0.92 sits above worktops, wardrobes and door heads
#: while staying clear of cornices and ceiling fixtures.
CEILING_BAND = (0.80, 0.92)

#: Fallback band when no ceiling was observed. Deliberately wide and used with
#: a consensus test rather than a single cut — see `_consensus_mask`.
MID_BAND = (0.35, 0.75)

#: A vertical histogram bin must hold at least this share of all points to
#: count as a dominant horizontal surface.
PEAK_SHARE = 0.02

#: Whitespace around the projected building, as a fraction of its extent.
#: See occupancy_raster — without it the exterior walls sit on the image border
#: and the room detector discards the interior as background.
RASTER_MARGIN = 0.06

SQFT_PER_M2 = 10.763910416709722


# ---------------------------------------------------------------------------
# Scale anchoring
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ScaleAnchor:
    """How a scale-free reconstruction acquired real units.

    `provenance` uses the same vocabulary as dimensions.py — measured, sourced,
    estimated — because this number propagates into every length and area the
    rehab estimate bills, and the reader needs to know whether it came from the
    world or from a rule.
    """

    metres_per_unit: float
    basis: Literal["explicit", "parcel_footprint", "recorded_area"]
    provenance: Literal["measured", "sourced", "estimated"]
    detail: str


def resolve_scale_anchor(
    *,
    metres_per_unit: Optional[float] = None,
    parcel_footprint_m2: Optional[float] = None,
    footprint_units2: Optional[float] = None,
) -> Optional[ScaleAnchor]:
    """The best available anchor, or None to let the raster stage refuse.

    Order is by how much the number can be defended, not by convenience:

      1. **explicit** — someone measured something. Nothing beats it.
      2. **parcel_footprint** — the county's surveyed building outline, matched
         against the reconstruction's own outer footprint. This is the strongest
         anchor Oracle can produce unaided, because parcel.py's polygons are
         exact rather than estimated.
      3. *(recorded living area is handled downstream — see the note below.)*

    Returning None is a real outcome, not a failure to try: `raster.resolve_scale`
    raises MissingScale with the actionable message, and duplicating that
    judgement here would give two places to disagree about it.
    """
    if metres_per_unit and metres_per_unit > 0:
        return ScaleAnchor(
            metres_per_unit=float(metres_per_unit),
            basis="explicit",
            provenance="measured",
            detail="Supplied by the caller as a measured dimension.",
        )

    if (parcel_footprint_m2 and parcel_footprint_m2 > 0
            and footprint_units2 and footprint_units2 > 0):
        # area_m2 = area_u2 * (m/u)^2
        metres_per_unit = math.sqrt(parcel_footprint_m2 / footprint_units2)
        return ScaleAnchor(
            metres_per_unit=metres_per_unit,
            basis="parcel_footprint",
            provenance="sourced",
            detail=(
                f"Solved against the surveyed parcel footprint "
                f"({parcel_footprint_m2:.1f} m²). Area matching, not contour "
                f"alignment: it assumes the reconstruction covers the same "
                f"single storey the parcel outlines, and ignores wall thickness."
            ),
        )

    return None


# ---------------------------------------------------------------------------
# Point cloud input
# ---------------------------------------------------------------------------

def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment guard
        raise UnsupportedInput(
            "Reconstruction slicing needs numpy, which is not installed."
        ) from exc
    return np


def parse_ply(data: bytes):
    """Read positions and (if present) opacity from a binary or ASCII PLY.

    PLY rather than .sog because it is the format every reconstruction tool
    emits and the only one with a self-describing header — the element and
    property names say what each column means, so a splat PLY and a plain
    point cloud both parse without a flag from the caller.

    Only x/y/z and opacity are read. Gaussian scale and rotation are ignored on
    purpose: this projects centres to a 2D occupancy grid, where an ellipsoid's
    extent changes a pixel's weight and not which pixel it lands in.
    """
    np = _require_numpy()

    if not data.startswith(b"ply"):
        raise UnsupportedInput("Not a PLY file — the reconstruction path expects PLY.")

    header_end = data.find(b"end_header")
    if header_end == -1:
        raise UnsupportedInput("PLY header is truncated (no end_header).")
    newline = data.find(b"\n", header_end)
    header = data[:newline].decode("ascii", "replace")
    body = data[newline + 1:]

    fmt = None
    count = 0
    fields: list[tuple[str, str]] = []
    in_vertex = False
    for line in header.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            in_vertex = parts[1] == "vertex"
            if in_vertex:
                count = int(parts[2])
        elif parts[0] == "property" and in_vertex:
            if parts[1] == "list":
                raise UnsupportedInput("PLY list properties are not supported on vertices.")
            fields.append((parts[1], parts[2]))

    if not count or not fields:
        raise DegenerateGeometry("PLY declares no vertices.")

    names = [name for _, name in fields]
    for axis in ("x", "y", "z"):
        if axis not in names:
            raise UnsupportedInput(f"PLY vertices have no {axis} property.")

    if fmt == "binary_little_endian":
        _PLY_TYPES = {
            "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
            "uchar": "u1", "uint8": "u1", "char": "i1", "int8": "i1",
            "ushort": "<u2", "uint16": "<u2", "short": "<i2", "int16": "<i2",
            "uint": "<u4", "uint32": "<u4", "int": "<i4", "int32": "<i4",
        }
        try:
            dtype = np.dtype([(name, _PLY_TYPES[kind]) for kind, name in fields])
        except KeyError as exc:
            raise UnsupportedInput(f"Unsupported PLY property type {exc}.") from exc
        needed = dtype.itemsize * count
        if len(body) < needed:
            raise DegenerateGeometry(
                f"PLY body is truncated: {len(body)} bytes for {count} vertices."
            )
        table = np.frombuffer(body[:needed], dtype=dtype)
        xyz = np.stack([table["x"], table["y"], table["z"]], axis=1).astype("float64")
        opacity = table["opacity"].astype("float64") if "opacity" in names else None
    elif fmt == "ascii":
        parsed = np.array(
            [line.split() for line in body.decode("ascii", "replace").splitlines()[:count]],
            dtype="float64",
        )
        if parsed.ndim != 2 or parsed.shape[1] < len(fields):
            raise DegenerateGeometry("PLY body does not match its declared properties.")
        index = {name: i for i, (_, name) in enumerate(fields)}
        xyz = parsed[:, [index["x"], index["y"], index["z"]]]
        opacity = parsed[:, index["opacity"]] if "opacity" in index else None
    else:
        raise UnsupportedInput(f"PLY format {fmt!r} is not supported (use binary_little_endian).")

    if opacity is not None:
        # Splat PLYs store opacity as a logit; a plain point cloud stores none.
        # Values outside [0,1] mean logits, so squash them.
        if float(opacity.min()) < 0.0 or float(opacity.max()) > 1.0:
            opacity = 1.0 / (1.0 + np.exp(-opacity))

    return xyz, opacity


# ---------------------------------------------------------------------------
# Gravity
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class UpAxis:
    vector: tuple[float, float, float]
    confidence: float
    detail: str


def estimate_up_axis(xyz, *, camera_positions=None) -> UpAxis:
    """Find which way is up, and say how sure that is.

    A reconstruction's coordinate frame is arbitrary — structure-from-motion has
    no idea where gravity is — so slicing "horizontally" is meaningless until
    this is established. Slice on the wrong axis and you get a vertical section
    through the house rendered as a floor plan: plausible-looking, entirely wrong.

    Method: a building's mass concentrates on two parallel horizontal planes,
    the floor and the ceiling. Project onto each candidate axis and the correct
    one shows that as two sharp histogram peaks; the other two axes show mass
    spread across the width of the house. So the up axis is the one whose
    height distribution is most concentrated.

    The sign is then resolved EXACTLY when camera positions are available:
    cameras live between floor and ceiling, so whichever extreme is further
    from the camera centroid on the negative side is the floor. Without them a
    weaker rule is used and the confidence says so.
    """
    np = _require_numpy()

    if len(xyz) < 32:
        raise DegenerateGeometry("Too few points to establish an orientation.")

    centred = xyz - xyz.mean(axis=0)
    # Principal axes give three orthogonal candidates aligned with the building
    # rather than with the arbitrary world frame.
    _, _, components = np.linalg.svd(centred, full_matrices=False)

    best = None
    for axis in components:
        heights = centred @ axis
        spread = float(heights.max() - heights.min())
        if spread <= 0:
            continue
        hist, _ = np.histogram(heights, bins=64)
        share = hist / max(1, hist.sum())
        # Concentration: how much mass sits in the few fullest bins. Floor and
        # ceiling put it in two; a horizontal axis spreads it across many.
        top = float(np.sort(share)[-4:].sum())
        if best is None or top > best[0]:
            best = (top, axis)

    if best is None:
        raise DegenerateGeometry("Point cloud is degenerate; no axis could be scored.")

    concentration, axis = best
    axis = axis / np.linalg.norm(axis)

    heights = centred @ axis
    detail = "Up axis from the most vertically concentrated principal axis."
    confidence = min(0.95, max(0.0, (concentration - 0.25) / 0.5))

    if camera_positions is not None and len(camera_positions) >= 2:
        cameras = np.asarray(camera_positions, dtype="float64") - xyz.mean(axis=0)
        camera_height = float((cameras @ axis).mean())
        # A camera is held between floor and ceiling. If most structure sits
        # ABOVE the cameras, the axis is pointing at the floor and must flip.
        below = float((heights < camera_height).mean())
        if below < 0.5:
            axis = -axis
            heights = -heights
        confidence = min(0.98, confidence + 0.25)
        detail = "Up axis resolved against camera positions (exact sign)."
    else:
        # Without cameras: the floor peak is normally the stronger of the two —
        # it is fully observed and carries furniture bases and floor texture,
        # while a ceiling is plain and often only partly seen. This is a
        # heuristic, and the confidence is held down to say so.
        lower = float((heights < 0).sum())
        upper = float((heights > 0).sum())
        if lower < upper:
            axis = -axis
            heights = -heights
        confidence = min(0.6, confidence)
        detail = (
            "Up axis inferred from mass distribution; no camera poses were "
            "supplied, so the sign is a heuristic."
        )

    return UpAxis(vector=tuple(float(v) for v in axis), confidence=confidence, detail=detail)


# ---------------------------------------------------------------------------
# Vertical structure
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class VerticalProfile:
    floor: float
    ceiling: Optional[float]
    height: Optional[float]
    ceiling_observed: bool
    detail: str


def vertical_profile(heights, planar=None) -> VerticalProfile:
    """Locate the floor, and the ceiling if the capture actually saw one.

    `ceiling_observed` is the field that matters. A hand-held photo sweep at eye
    level frequently never points up, so a reconstruction of a real listing may
    contain no ceiling at all — and the preferred slice band depends on having
    one. Reporting a ceiling that was never observed would put the slice in
    open air and return an empty plan.
    """
    np = _require_numpy()

    lo, hi = float(np.percentile(heights, 0.5)), float(np.percentile(heights, 99.5))
    if hi - lo <= 0:
        raise DegenerateGeometry("Point cloud has no vertical extent.")

    hist, edges = np.histogram(heights, bins=96, range=(lo, hi))
    share = hist / max(1, hist.sum())
    centres = (edges[:-1] + edges[1:]) / 2.0

    dominant = [i for i, value in enumerate(share) if value >= PEAK_SHARE]
    if not dominant:
        raise DegenerateGeometry(
            "No dominant horizontal surface found — this does not look like a building interior."
        )

    floor = float(centres[dominant[0]])
    bin_width = float(edges[1] - edges[0])

    def _covers_the_building(height: float) -> bool:
        """Does the surface at this height span the footprint, or is it furniture?

        A height histogram cannot tell a ceiling from a kitchen island: both are
        horizontal planes that concentrate points in one bin. Measured on a
        synthetic house, a 0.95 m island top was confidently reported as the
        ceiling of a room whose walls ran to 2.5 m — and every slice derived
        from that is then cut through the middle of the furniture.

        What separates them is planar extent. A ceiling covers the building; a
        worktop covers a corner of it.
        """
        if planar is None:
            return True
        slab = (heights >= height - bin_width) & (heights <= height + bin_width)
        base = (heights >= floor - bin_width) & (heights <= floor + bin_width)
        if slab.sum() < 16 or base.sum() < 16:
            return False
        # Fill, not bounding box. Every height in a building contains the four
        # walls, so a bounding box spans the footprint at ANY height and the
        # test passes for a worktop. What separates a ceiling is that it FILLS
        # the interior, where walls only trace its outline.
        def _fill(points) -> float:
            cells = 24
            x0, x1 = float(planar[:, 0].min()), float(planar[:, 0].max())
            y0, y1 = float(planar[:, 1].min()), float(planar[:, 1].max())
            if x1 <= x0 or y1 <= y0:
                return 0.0
            cx = np.clip(((points[:, 0] - x0) / (x1 - x0) * (cells - 1)).astype("int32"),
                         0, cells - 1)
            cy = np.clip(((points[:, 1] - y0) / (y1 - y0) * (cells - 1)).astype("int32"),
                         0, cells - 1)
            grid = np.zeros((cells, cells), dtype=bool)
            grid[cy, cx] = True
            return float(grid.mean())

        floor_fill = _fill(planar[base])
        return floor_fill > 0 and (_fill(planar[slab]) / floor_fill) >= 0.55
    # A ceiling is only a ceiling if it is well clear of the floor. Anything
    # within a metre-ish of it (in unknown units, so: a fifth of the extent) is
    # the same surface's spread, not a second storey boundary.
    span = hi - lo
    upper = [i for i in dominant
             if centres[i] - floor > span * 0.2 and _covers_the_building(float(centres[i]))]
    if upper:
        ceiling = float(centres[upper[-1]])
        return VerticalProfile(
            floor=floor, ceiling=ceiling, height=ceiling - floor,
            ceiling_observed=True,
            detail="Floor and ceiling both resolved as dominant horizontal surfaces.",
        )

    return VerticalProfile(
        floor=floor, ceiling=None, height=None, ceiling_observed=False,
        detail=(
            "No ceiling surface was observed — the capture probably never "
            "pointed upward. Falling back to a mid-height consensus slice."
        ),
    )


def choose_slice_band(profile: VerticalProfile, heights) -> tuple[float, float, str]:
    """The height band to cut, chosen from the data rather than assumed.

    Ceiling-adjacent when a ceiling exists. This is the counter-intuitive part
    and it is the whole accuracy argument: furniture stands on the floor, so the
    band just under the ceiling contains walls and almost nothing else. A
    waist-height cut — the obvious choice — passes straight through kitchen
    islands, wardrobes and bookcases, and every one of them becomes a wall.
    """
    np = _require_numpy()

    if profile.ceiling_observed and profile.height:
        lo = profile.floor + profile.height * CEILING_BAND[0]
        hi = profile.floor + profile.height * CEILING_BAND[1]
        return lo, hi, "ceiling-adjacent"

    top = float(np.percentile(heights, 97.0))
    span = top - profile.floor
    lo = profile.floor + span * MID_BAND[0]
    hi = profile.floor + span * MID_BAND[1]
    return lo, hi, "mid-height consensus"


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

#: Dilation applied to each sub-slice before it votes, and again to the final
#: stroke. A drawn plan has thick wall lines; a projected point cloud has dots.
_VOTE_KERNEL = None          # built lazily, needs cv2


def _ground_basis(np, axis):
    """Two orthonormal vectors spanning the ground plane."""
    seed = np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    right = np.cross(axis, seed)
    right /= np.linalg.norm(right)
    forward = np.cross(axis, right)
    return right, forward


def _dominant_angle(cv2, planar) -> float:
    """Orientation of the building's own axes, in radians.

    Minimum-area rectangle rather than PCA: PCA follows the mass distribution,
    so a long interior partition or an unevenly sampled wall tilts it. The
    min-area rectangle follows the extent, which for a building is its walls.
    """
    rect = cv2.minAreaRect(planar.astype("float32"))
    return math.radians(rect[2] % 90.0)


def _consensus_mask(np, cv2, points, heights, lo, hi, *, sub_slices: int, width: int,
                    height_px: int, x_min, x_max, y_min, y_max):
    """Occupancy that survives across the band, not just somewhere in it.

    A wall is vertically continuous: cut the band into thin layers and a wall
    appears in nearly all of them. A worktop, a wardrobe top or a door head
    appears in one or two. Requiring agreement across layers is therefore a
    second, independent filter on clutter — and it is the one that carries the
    mid-height fallback, where the ceiling-adjacent trick is unavailable.
    """
    global _VOTE_KERNEL
    if _VOTE_KERNEL is None:
        _VOTE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    votes = np.zeros((height_px, width), dtype="uint16")
    bounds = np.linspace(lo, hi, sub_slices + 1)
    for index in range(sub_slices):
        in_layer = (heights >= bounds[index]) & (heights < bounds[index + 1])
        if not in_layer.any():
            continue
        layer = points[in_layer]
        cols = ((layer[:, 0] - x_min) / (x_max - x_min) * (width - 1)).astype("int32")
        rows = ((layer[:, 1] - y_min) / (y_max - y_min) * (height_px - 1)).astype("int32")
        hit = np.zeros((height_px, width), dtype="uint8")
        hit[rows, cols] = 1
        # Dilate before voting. A surface is sampled as scattered points, not
        # as filled pixels, so asking whether the SAME pixel was hit in several
        # thin layers is asking whether the same random sample recurred — it
        # does not, and the vote wipes out every real wall. Dilating first makes
        # the question "was this neighbourhood occupied", which is the one that
        # distinguishes a continuous wall from a worktop.
        hit = cv2.dilate(hit, _VOTE_KERNEL, iterations=1)
        votes += hit.astype("uint16")
    return votes, sub_slices


def occupancy_raster(xyz, up: UpAxis, lo: float, hi: float, *, sub_slices: int = 8,
                     band_basis: str = "ceiling-adjacent"):
    """Project the slab to a top-down wall image the raster extractor can read.

    Returns (png_bytes, units_per_pixel). Units are the reconstruction's own —
    metres only enter later, and only from an anchor.
    """
    np = _require_numpy()
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment guard
        raise UnsupportedInput(
            "Reconstruction slicing needs opencv-python-headless, which is not installed."
        ) from exc

    axis = np.asarray(up.vector, dtype="float64")
    right, forward = _ground_basis(np, axis)

    heights = xyz @ axis
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)

    band = (heights >= lo) & (heights <= hi)
    if band.sum() >= 8:
        # Rotate so the building's dominant wall direction is axis-aligned.
        # Not cosmetic: raster.build_wall_mask isolates walls by opening with
        # HORIZONTAL and VERTICAL kernels, so a house sitting at 37 degrees in
        # the raster is erased entirely before Hough ever runs. The
        # reconstruction's frame is arbitrary, so without this the result
        # depends on which way the photographer happened to be standing.
        angle = _dominant_angle(cv2, planar[band])
        cos_a, sin_a = math.cos(-angle), math.sin(-angle)
        planar = np.stack([
            planar[:, 0] * cos_a - planar[:, 1] * sin_a,
            planar[:, 0] * sin_a + planar[:, 1] * cos_a,
        ], axis=1)
    if band.sum() < 64:
        raise DegenerateGeometry(
            "The chosen slice contains almost no points — the capture may not cover "
            "the walls at this height."
        )

    inside = planar[band]
    x_min, y_min = float(inside[:, 0].min()), float(inside[:, 1].min())
    x_max, y_max = float(inside[:, 0].max()), float(inside[:, 1].max())
    if x_max - x_min <= 0 or y_max - y_min <= 0:
        raise DegenerateGeometry("Slice has no planar extent.")

    # Pad the extent. A drawn plan has whitespace around the building; a raw
    # projection does not, so the outer walls land ON the image border. That
    # matters because detect_rooms discards any component touching the border
    # as the exterior background — with no margin, the building's interior IS
    # that component, and every room is thrown away.
    pad = span_pad = max(x_max - x_min, y_max - y_min) * RASTER_MARGIN
    x_min, x_max = x_min - pad, x_max + pad
    y_min, y_max = y_min - pad, y_max + pad

    span = max(x_max - x_min, y_max - y_min)
    units_per_pixel = span / (RASTER_PX - 1)
    width = max(2, int(round((x_max - x_min) / units_per_pixel)) + 1)
    height_px = max(2, int(round((y_max - y_min) / units_per_pixel)) + 1)

    votes, total = _consensus_mask(
        np, cv2, planar[band], heights[band], lo, hi,
        sub_slices=sub_slices, width=width, height_px=height_px,
        x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
    )

    # A majority of layers must agree. Below half and clutter survives; at the
    # full count a single gap in coverage erases a real wall.
    occupied = (votes * 2 >= total).astype("uint8") * 255
    if occupied.sum() == 0:
        raise DegenerateGeometry("No surface survived the vertical consensus test.")

    # Close small gaps from sampling, then thin: raster.py expects wall STROKES
    # like a drawn plan, and estimates thickness from them.
    # Bridge the dots into strokes, ALONG the wall axes.
    #
    # A projected surface is a scatter of samples, so a wall arrives as a
    # dotted line. raster.build_wall_mask isolates walls by opening with a
    # ~27px run kernel, and a dotted line contains no 27px continuous run — so
    # every wall is erased and the plan comes back empty. Closing with long
    # 1-D kernels joins the dots without fattening the wall sideways, which a
    # square kernel would.
    #
    # Directional closing is only valid because the projection was already
    # rotated to the building's own axes; on an unaligned raster this would
    # smear diagonal walls into staircases.
    # How far to reach when joining dots, as a fraction of the building.
    #
    # A ceiling-adjacent slice can afford a long reach, because at that height
    # a doorway is NOT a gap — a door stops around 2 m while the wall carries
    # on to the ceiling. So the only gaps there are sampling artifacts, and
    # bridging them cannot erase an opening that the slice never saw anyway.
    #
    # The mid-height fallback cuts through door openings, so the reach stays
    # short: bridging there would seal real doorways into solid wall and
    # silently merge two rooms into one.
    reach = 0.08 if band_basis == "ceiling-adjacent" else 0.02
    run = max(9, int(min(width, height_px) * reach) | 1)
    along_x = cv2.morphologyEx(
        occupied, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (run, 1))
    )
    along_y = cv2.morphologyEx(
        occupied, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, run))
    )
    occupied = cv2.bitwise_or(along_x, along_y)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    occupied = cv2.morphologyEx(occupied, cv2.MORPH_CLOSE, kernel, iterations=2)
    # Give the stroke real width. raster.py measures wall thickness off the
    # mask and scales its collinear-merge tolerance from it, so a one-pixel
    # line makes that tolerance meaningless.
    occupied = cv2.dilate(occupied, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))

    # The extractor binarises a dark-on-light plan, so invert: walls dark.
    image = cv2.cvtColor(255 - occupied, cv2.COLOR_GRAY2BGR)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise DegenerateGeometry("Could not encode the projected slice.")
    return buffer.tobytes(), units_per_pixel


def footprint_area_units2(xyz, up: UpAxis, lo: float, hi: float) -> float:
    """Planar area the reconstruction covers, in reconstruction units squared.

    Used only to solve scale against a surveyed parcel footprint. Deliberately
    the convex hull of the sliced band: it is the outer shell, which is what a
    parcel polygon describes, and it is stable against the interior voids a
    concave hull would chase into.
    """
    np = _require_numpy()
    import cv2

    axis = np.asarray(up.vector, dtype="float64")
    right, forward = _ground_basis(np, axis)

    heights = xyz @ axis
    band = (heights >= lo) & (heights <= hi)
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)[band]
    if len(planar) < 3:
        raise DegenerateGeometry("Too few points in the slice to measure a footprint.")

    hull = cv2.convexHull(planar.astype("float32"))
    return float(cv2.contourArea(hull))


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def extract_from_reconstruction(
    ply_bytes: bytes,
    *,
    metres_per_unit: Optional[float] = None,
    parcel_footprint_m2: Optional[float] = None,
    known_total_sqft: Optional[float] = None,
    camera_positions=None,
    min_opacity: float = MIN_OPACITY,
    level_name: str = "Ground Floor",
    level_index: int = 0,
    model_version: str = "floorplan-slice-1.0.0",
):
    """A FloorplanDocument from a reconstruction of the property.

    Raises MissingScale when no anchor can put the plan in metres, and
    DegenerateGeometry when the capture yields too little structure — the same
    two failures the raster path already models, for the same reason: a plan
    with an invented scale looks correct and is uniformly wrong.
    """
    np = _require_numpy()
    from . import raster

    xyz, opacity = parse_ply(ply_bytes)
    if opacity is not None:
        keep = opacity >= min_opacity
        if keep.sum() >= 32:
            xyz = xyz[keep]
        else:
            log.warning(
                "Opacity filter would drop nearly everything (%d of %d survive); "
                "keeping all points.", int(keep.sum()), len(xyz),
            )

    up = estimate_up_axis(xyz, camera_positions=camera_positions)
    axis = np.asarray(up.vector, dtype="float64")
    heights = xyz @ axis
    right, forward = _ground_basis(np, axis)
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)
    profile = vertical_profile(heights, planar)
    lo, hi, band_basis = choose_slice_band(profile, heights)

    image_bytes, units_per_pixel = occupancy_raster(xyz, up, lo, hi, band_basis=band_basis)

    anchor = resolve_scale_anchor(
        metres_per_unit=metres_per_unit,
        parcel_footprint_m2=parcel_footprint_m2,
        footprint_units2=(
            footprint_area_units2(xyz, up, lo, hi) if parcel_footprint_m2 else None
        ),
    )

    # Either hand the extractor a real metres-per-pixel, or hand it the recorded
    # area and let its own solver do the work. It refuses when neither exists,
    # and that refusal is the correct behaviour rather than a gap to fill here.
    metres_per_pixel = anchor.metres_per_unit * units_per_pixel if anchor else None

    document = raster.extract_from_floorplan_image(
        image_bytes,
        metres_per_pixel=metres_per_pixel,
        known_total_sqft=known_total_sqft,
        level_name=level_name,
        level_index=level_index,
        model_version=model_version,
    )

    if band_basis == "ceiling-adjacent" and document.openings:
        # A ceiling-adjacent slice sits above every door head, so anything
        # detect_openings found there is a sampling gap wearing a door's
        # clothes. Openings are simply not observable from this band.
        document.openings = []

    document.provenance.source = "reconstruction"
    document.provenance.notes = " ".join(filter(None, [
        f"Sliced from a photogrammetric reconstruction ({band_basis} band).",
        up.detail,
        profile.detail,
        anchor.detail if anchor else "",
    ]))
    # An orientation guess and a clutter-prone fallback band both make the
    # result less trustworthy, and the reader is entitled to see that in the
    # number rather than only in the prose.
    if document.provenance.confidence is not None:
        penalty = 1.0 if profile.ceiling_observed else 0.75
        document.provenance.confidence = round(
            document.provenance.confidence * penalty * (0.6 + 0.4 * up.confidence), 3
        )

    _classify_exterior_walls(document)
    return document


def _classify_exterior_walls(document) -> None:
    """Mark which walls are the shell and which are partitions.

    The outer boundary of the slice is the building's exterior; everything
    inside it divides rooms. FloorplanWall.interior already exists in the
    schema, so this sets a field rather than inventing one — and it is derived
    from the geometry rather than from a room count.

    Note this classifies THIS plan's walls. It does not make the reconstruction
    authoritative for the building outline: photogrammetric footprints run
    above 90% accurate where parcel.py's surveyed polygons are exact, so the
    exterior of record stays the parcel.
    """
    if not document.walls:
        return

    xs = [p[0] for wall in document.walls for p in (wall.start, wall.end)]
    ys = [p[1] for wall in document.walls for p in (wall.start, wall.end)]
    x_min, x_max, y_min, y_max = min(xs), max(xs), min(ys), max(ys)
    span = max(x_max - x_min, y_max - y_min)
    if span <= 0:
        return
    # Within this fraction of the bounding box edge counts as on the shell.
    margin = span * 0.04

    edges = (
        lambda px, py: abs(px - x_min) <= margin,     # west
        lambda px, py: abs(px - x_max) <= margin,     # east
        lambda px, py: abs(py - y_min) <= margin,     # north
        lambda px, py: abs(py - y_max) <= margin,     # south
    )
    for wall in document.walls:
        # Both ends must lie along the SAME boundary edge. Testing each end
        # against *any* edge marks a partition exterior, because a wall that
        # spans the building touches the west boundary at one end and the east
        # at the other — which is precisely what makes it a partition.
        wall.interior = not any(
            on_edge(*wall.start) and on_edge(*wall.end) for on_edge in edges
        )
