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

from . import segmentation
from .errors import DegenerateGeometry, MissingScale, UnsupportedInput
from .pointfeatures import LABEL_WALL

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

#: Points used to search for the up axis. The search is O(points x candidates);
#: a plane sampled every nth point is still a plane, so this caps the cost
#: without changing the answer.
UP_AXIS_SAMPLE = 120_000

#: Histogram resolution for the "is this direction a plane normal" measure.
PEAK_BINS = 96

#: A storey is at least this tall, in metres. Applied only when the caller
#: supplied a real scale — see the gate in `estimate_up_axis`.
MIN_STOREY_M = 1.9

#: How far the up axis may sit from the camera path's flat direction. Wide
#: enough to absorb a hand-held sweep's wobble, narrow enough to still pick a
#: single face of a near-cubic room.
CAMERA_AXIS_DEG = 20.0

#: A camera is confined along gravity — held at eye height — and spread along
#: every horizontal. This is the largest fraction of the cloud's own extent the
#: camera path may span before the direction stops looking like gravity.
#:
#: Physical, not tuned: a camera carried at eye height wobbles by 0.1-0.3 m
#: inside a 2.4-3.6 m storey, so the real value is 0.04-0.12 and even a
#: photographer who crouches stays near 0.2. It was set at 0.5, which is loose
#: enough to admit a capture that climbed a staircase — there, no direction is
#: gravity, but the narrow ACROSS-stairs direction scored 0.45 and got adopted,
#: dragging a 83-degree error out to 48 rather than leaving it alone.
CAMERA_CONFINEMENT = 0.3

#: How much better the winning direction must confine the cameras than its best
#: rival. Deliberately modest, because the absolute guard above does the real
#: work: a capture that climbed a staircase spans most of the height and is
#: thrown out by CAMERA_CONFINEMENT, not by this. Demanding a full 2x rejected
#: a 1.1 m stairwell whose true separation is 1.8x.
CAMERA_MARGIN = 0.7

#: How far a direction must sit from the camera-derived axis to count as a
#: rival to it. Wider than DISTINCT_AXIS_DEG on purpose: gravity's real
#: alternatives are the room's other faces, roughly 90 degrees away, while a
#: direction 28 degrees off is the same answer with error on it — and treating
#: that shoulder as a rival left the stairwell undecided.
CAMERA_RIVAL_DEG = 60.0

#: Ceiling on confidence when the camera path and the cloud's structure point
#: somewhere different. Neither source can be dismissed, so neither answer is
#: asserted strongly.
CONFLICT_CONFIDENCE = 0.45

#: Two candidate directions closer together than this are the same answer, not
#: rival ones. Confidence is the margin over the best genuinely different
#: direction, so this is what separates "decisive" from "a coin flip".
DISTINCT_AXIS_DEG = 25.0

#: Whitespace around the projected building, as a fraction of its extent.
#:
#: Two constraints, and the second is why this is not smaller. Without ANY
#: margin the exterior walls sit on the image border and detect_rooms discards
#: the building's interior as background. But detect_rooms only discards a
#: border-touching component when it exceeds 20% of the image, so the margin
#: must also be wide enough that the exterior ring clears that bar decisively.
#:
#: At 0.06 the ring is ~20.3% of the image — right on the threshold, so it was
#: counted as a *room* on roughly half of all plans, and that phantom room is
#: larger than the entire building. It caused every spurious-room error
#: measured: 1->2 on three houses and 2->3 on two more.
#:
#: At 0.12 the building occupies ~65% of the image and the ring ~35%, which is
#: unambiguous. The cost is resolution: the same RASTER_PX now spans a larger
#: area, so walls are slightly thinner in pixels.
RASTER_MARGIN = 0.12

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


def estimate_up_axis(xyz, *, camera_positions=None, metres_per_unit=None) -> UpAxis:
    """Find which way is up, and say how sure that is.

    A reconstruction's coordinate frame is arbitrary — structure-from-motion has
    no idea where gravity is — so slicing "horizontally" is meaningless until
    this is established. Slice on the wrong axis and you get a vertical section
    through the house rendered as a floor plan: plausible-looking, entirely wrong.

    Method: a building's mass concentrates on two parallel horizontal planes,
    the floor and the ceiling. Project onto each candidate axis and the correct
    one shows that as one very full histogram bin; the other two axes show mass
    spread across the width of the house.

    That measure alone is not sufficient, and the two priors that constrain it
    are the substance of this function — see the comments at each.

    The sign is then resolved EXACTLY when camera positions are available:
    cameras live between floor and ceiling, so whichever extreme is further
    from the camera centroid on the negative side is the floor. Without them a
    weaker rule is used and the confidence says so.
    """
    np = _require_numpy()

    if len(xyz) < 32:
        raise DegenerateGeometry("Too few points to establish an orientation.")

    centred = xyz - xyz.mean(axis=0)

    # Finding a DIRECTION does not need every point, and the search below costs
    # O(points x candidates). A deterministic stride keeps it bounded on the
    # multi-million-point clouds a real reconstruction produces, and changes no
    # answer: a plane sampled every nth point is still a plane.
    sample = centred
    if len(centred) > UP_AXIS_SAMPLE:
        sample = centred[:: len(centred) // UP_AXIS_SAMPLE][:UP_AXIS_SAMPLE]
    sample = np.ascontiguousarray(sample, dtype="float64")

    def _measure(directions):
        """Extent and fullest-bin share for each direction.

        Blocked rather than one direction at a time: `sample @ block.T` is a
        single BLAS call that answers 32 directions at once, and the histogram
        becomes a bincount over offset bin indices. The previous form evaluated
        the full matmul three times per direction — twice for the extent and
        once inside the histogram — which was ~830 passes over the cloud per
        call, doubled again by the "auto" default.
        """
        extents = np.empty(len(directions))
        peaks = np.empty(len(directions))
        for start in range(0, len(directions), 32):
            block = directions[start:start + 32]
            width = len(block)
            projected = sample @ block.T
            low = projected.min(axis=0)
            spread = projected.max(axis=0) - low
            extents[start:start + width] = spread
            safe = np.where(spread > 0, spread, 1.0)
            index = np.clip(
                ((projected - low) / safe * PEAK_BINS).astype("int64"), 0, PEAK_BINS - 1
            )
            index = index + np.arange(width) * PEAK_BINS
            counts = np.bincount(
                index.ravel(), minlength=width * PEAK_BINS
            ).reshape(width, PEAK_BINS)
            peaks[start:start + width] = np.where(
                spread > 0, counts.max(axis=1) / len(sample), 0.0
            )
        return extents, peaks

    # A Fibonacci hemisphere: even coverage, no clustering at the poles.
    count = 128
    index = np.arange(count) + 0.5
    z = index / count                      # (0, 1] — upper hemisphere only;
    radius = np.sqrt(1.0 - z * z)          # the sign is resolved separately.
    theta = np.pi * (1 + 5 ** 0.5) * index
    candidates = np.stack(
        [radius * np.cos(theta), radius * np.sin(theta), z], axis=1
    )
    _, _, components = np.linalg.svd(sample, full_matrices=False)
    # The principal axes, but NOT their negatives. Every measure below is
    # exactly sign-invariant, so `-component` is the same candidate scored
    # twice; all it ever did was guarantee `best == runner_up` whenever a
    # principal axis won, which pinned the reported confidence to its floor.
    candidates = np.vstack([candidates, components])
    candidates /= np.linalg.norm(candidates, axis=1, keepdims=True)

    extents, peaks = _measure(candidates)

    # Two priors, and the ORDER matters.
    #
    # First the structural prior: a room is wider than it is tall, so the up
    # axis is found among the NARROWEST directions. This is what neither
    # earlier attempt used, and it is why both kept choosing the room's LENGTH:
    # a long room's two end walls score exactly like a floor and a ceiling on
    # any peak-based measure, and nothing in that measure knows 13 m cannot be
    # a ceiling height.
    #
    # Then the plane evidence breaks the tie among the narrow candidates, which
    # is what pins down the exact normal rather than merely the right family.
    admissible = np.ones(len(candidates), dtype=bool)
    gate = ""
    if metres_per_unit and metres_per_unit > 0:
        # A known scale upgrades the structural prior to a physical one, and it
        # has to, because "narrowest" is the wrong question in a narrow room.
        # The rule below measures every direction against the THINNEST one, and
        # in a corridor, a galley kitchen, a stairwell or a closet the thinnest
        # direction is the WIDTH. A 1.2 x 8 x 2.4 m corridor put its own 1.2 m
        # width in the reference slot, which placed the real 2.4 m ceiling
        # height outside the band and returned an up axis 90 degrees out —
        # four of eight ordinary room shapes failed this way, silently.
        #
        # Metres settle it: 1.2 m cannot be a storey. Nothing here caps the
        # upper end, because a two-storey capture is legitimately 5 m tall and
        # the relative rule below already excludes the long axis.
        tall_enough = extents * float(metres_per_unit) >= MIN_STOREY_M
        if tall_enough.any():
            admissible &= tall_enough
            gate = " Directions too short to be a storey were ruled out."

    # Cameras decide the AXIS, not just the sign.
    #
    # A walkthrough spreads across the floor and stays at eye height, so the
    # direction of LEAST variance in the camera path is gravity. That is a
    # direct measurement, and it is the only thing that resolves the shapes
    # geometry cannot: a near-cubic bathroom, a galley kitchen whose width and
    # ceiling height differ by 100 mm, a stairwell that is taller than it is
    # wide. None of those is decidable from the cloud alone — a rectangular box
    # maps any face-pair onto any other, so no scale-free measure separates
    # them — and this file previously spent the camera evidence on the sign
    # alone while the axis was left to a prior that cannot win.
    #
    # Trusted only when the path really does look like one held at eye height:
    # a capture that climbed a staircase, or one with too few poses, falls
    # through to the geometry rather than reporting the stairs as gravity.
    camera_axis = None
    if camera_positions is not None and len(camera_positions) >= 4:
        cameras = np.asarray(camera_positions, dtype="float64") - xyz.mean(axis=0)
        # CONFINEMENT, not the camera path's own shape. The measure is the
        # camera spread along a direction as a fraction of the CLOUD's extent
        # along it: a photographer covers most of the room horizontally but
        # holds the camera at one height, so gravity is where that fraction
        # collapses. Reading the path's shape alone fails exactly where the help
        # is needed — in a 1.1 m wide stairwell the horizontal spread of the
        # walk is no larger than the wobble in its height.
        span = np.ptp(cameras @ candidates.T, axis=0)
        # Minimise the RAW span, then validate with the ratio — not the other
        # way round. Span-over-extent has no sharp minimum at gravity: tilting
        # off it toward a long axis inflates the cloud's extent faster than the
        # camera path's spread, so in the stairwell the ratio was flat to within
        # 0.0001 across a 28-degree cone and the argmin sat off-axis. The raw
        # span has no such problem — tilting immediately picks up the length of
        # the walk.
        tightest = int(span.argmin())
        apart = np.abs(candidates @ candidates[tightest]) < math.cos(
            math.radians(CAMERA_RIVAL_DEG)
        )
        # Locate with the raw span, DECIDE with the ratio. Each measure is
        # good at one job and bad at the other: the raw span has a sharp
        # minimum at gravity but goes undecided in a narrow room, where the
        # walk is nearly as confined across the space as it is in height
        # (0.256 m against 0.266 m in a 1.1 m stairwell). Dividing by the
        # cloud's own extent is what separates those — 0.25 m of wobble inside
        # a 3.6 m storey is confinement; 0.37 m across a 1.1 m width is just a
        # narrow room.
        confinement = span / np.maximum(extents, 1e-9)
        rival = float(confinement[apart].min()) if apart.any() else math.inf
        confined = float(confinement[tightest])
        # Two conditions, and both matter. The ratio says this looks like eye
        # height inside a storey rather than a path that climbed; the margin
        # says no genuinely different direction confines the cameras as well —
        # which is what rules out a straight corridor walk, where the path is
        # equally narrow across the corridor and along gravity.
        if confined <= CAMERA_CONFINEMENT and confined <= rival * CAMERA_MARGIN:
            camera_axis = candidates[tightest] / np.linalg.norm(candidates[tightest])

    # Keep what geometry alone would have said, so the two can be compared.
    geometric = admissible.copy()

    if camera_axis is not None:
        aligned = np.abs(candidates @ camera_axis)
        near = aligned >= math.cos(math.radians(CAMERA_AXIS_DEG))
        if near.any():
            admissible = near
            gate = " Axis measured from the camera path."

    def _choose(mask):
        """Winner, its coarse score, and the band used — from one admissible set.

        Factored out because the camera answer and the geometry-only answer have
        to be computed the SAME way to be comparable. Deriving the band once,
        after the camera restriction, and then re-using it to ask "what would
        geometry have said" gives the wrong answer to that question: the band is
        part of the decision, not a fixed backdrop.
        """
        # The reference extent comes from directions that are actually PLANAR.
        #
        # It used to be the smallest admissible extent over all 131 candidates,
        # which in a room with no clearly thin axis is a TILTED direction
        # corresponding to no surface at all. The 1.35x band was then measured
        # around a number the building does not have, and it excluded the real
        # ceiling: a 2.0 x 2.2 x 2.4 m bathroom and a 2.4 x 4.5 x 2.5 m galley
        # kitchen both came back 90 degrees out on that alone. A direction that
        # is not a plane normal smears its planes across bins and scores low, so
        # filtering on the peak first keeps the reference on a real surface.
        #
        # It is not a cure-all. A stairwell is taller than it is wide, which
        # defeats the whole "a room is wider than it is tall" prior underneath
        # this, and no reference extent rescues that — only the cameras do.
        pool = peaks[mask]
        planar = mask & (peaks >= float(np.percentile(pool, 75)) * 0.5)
        span_of = extents[planar if planar.any() else mask]
        reference = float(span_of.min())
        band = extents <= reference * 1.35
        scored = np.where(mask & band, peaks, -1.0)
        if not (scored > 0).any():             # every candidate was ruled out
            scored = np.where(mask, peaks, -1.0)
        index = int(scored.argmax())
        return index, float(peaks[index]), reference, scored

    winner, coarse_best, reference, scores = _choose(admissible)
    axis = candidates[winner] / np.linalg.norm(candidates[winner])
    best_score = coarse_best

    # Refine: walk a finer set around the winner, so the answer is not limited
    # by the coarse sampling. A degree of tilt here is metres of drift across a
    # long wall. One generator for all three passes — re-seeding it inside the
    # loop, as this did, redrew the SAME 48 offsets at each scale, making the
    # refinement a line search down 48 fixed rays instead of a widening search.
    rng = np.random.default_rng(0)
    for scale in (0.25, 0.08, 0.025):
        local = axis + scale * rng.normal(size=(48, 3))
        local /= np.linalg.norm(local, axis=1, keepdims=True)
        # Refinement keeps both constraints: without them the walk drifts
        # straight back out to the long axis, which scores better on peaks.
        local_extents, local_peaks = _measure(local)
        allowed = local_extents <= reference * 1.35
        if metres_per_unit and metres_per_unit > 0:
            allowed &= local_extents * float(metres_per_unit) >= MIN_STOREY_M
        local_scores = np.where(allowed, local_peaks, -1.0)
        if local_scores.max() > best_score:
            best_score = float(local_scores.max())
            axis = local[int(local_scores.argmax())]

    # Confidence is the margin over the best GENUINELY DIFFERENT direction.
    #
    # It used to be the margin over the raw runner-up. On a candidate set this
    # dense the runner-up is always an immediate neighbour of the winner
    # pointing essentially the same way, so the two scores were near-identical
    # by construction and almost every capture reported the 0.25 floor —
    # including ones whose axis was accurate to a third of a degree. A rival
    # only deserves the name if choosing it would produce a different plan.
    rival = 0.0
    considered = scores > 0
    if considered.any():
        aligned = np.abs(candidates[considered] @ axis)
        distinct = aligned < math.cos(math.radians(DISTINCT_AXIS_DEG))
        if distinct.any():
            rival = float(scores[considered][distinct].max())
    margin = (coarse_best - rival) / max(coarse_best, 1e-9)
    concentration = 0.25 + 0.75 * min(1.0, max(0.0, margin * 1.5))

    # Two independent sources that disagree is not a confident answer, whichever
    # one is right.
    #
    # A camera path cannot tell "walked along a level floor" from "walked up a
    # staircase" — the two are the same shape, and only gravity separates them.
    # So a capture taken while climbing hands this function a path whose
    # tightest direction is perpendicular to the stairs rather than to the
    # ground, and no test on the path alone can catch it. What CAN be seen is
    # that the cameras and the cloud are pointing somewhere different, and that
    # is worth saying out loud rather than resolving silently in favour of
    # either one.
    disagreement = ""
    if camera_axis is not None and geometric.any():
        without = candidates[_choose(geometric)[0]]
        apart_deg = math.degrees(math.acos(min(1.0, abs(float(without @ axis)))))
        if apart_deg > DISTINCT_AXIS_DEG:
            concentration = min(concentration, CONFLICT_CONFIDENCE)
            disagreement = (
                f" The camera path and the cloud's own structure disagree by "
                f"{apart_deg:.0f} degrees; the cameras were preferred, but treat "
                f"this orientation as unconfirmed."
            )

    heights = centred @ axis
    # How the AXIS was found. The sign is resolved below and appends to this
    # rather than replacing it — overwriting threw away both the note that the
    # cameras had chosen the axis and the warning that they disagreed with the
    # cloud, which are the two things a reader most needs.
    orientation = "Up axis from the narrowest strongly planar direction." + gate
    confidence = min(0.95, max(0.05, concentration))

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
        sign = " Sign resolved against camera positions (exact)."
    else:
        # Without cameras, the sign comes from where the CONTENTS are: chairs,
        # tables, worktops and boxes stand on the floor, so the band just above
        # the lower plane holds far more than the band just below the upper one.
        #
        # The previous rule — "the floor is sampled more densely than the
        # ceiling" — is not reliable: a synthetic bare box samples both equally
        # and it coin-flips, and an inverted sign puts the ceiling-adjacent slice
        # down at floor level, cutting through exactly the furniture the band
        # exists to avoid.
        low, high = float(heights.min()), float(heights.max())
        span = max(high - low, 1e-9)
        # Skip the outer tenth so the floor and ceiling planes themselves,
        # which are symmetric, do not dominate the comparison.
        above_low = float(((heights > low + span * 0.10) & (heights < low + span * 0.45)).sum())
        below_high = float(((heights > high - span * 0.45) & (heights < high - span * 0.10)).sum())
        if above_low < below_high:
            axis = -axis
            heights = -heights
        confidence = min(0.6, confidence)
        sign = (
            " Sign inferred from where the contents sit; no camera poses were "
            "supplied, so it is a heuristic."
        )
        if abs(above_low - below_high) <= 0.05 * max(above_low, below_high, 1.0):
            # A bare symmetric room genuinely has no up. Say so rather than
            # letting a coin-flip pass as a measurement.
            confidence = min(confidence, 0.25)
            sign = (
                " Its SIGN is ambiguous: the space is close to symmetric about "
                "its mid-height, so nothing distinguishes floor from ceiling. "
                "Supply camera positions to resolve it."
            )

    return UpAxis(
        vector=tuple(float(v) for v in axis),
        confidence=confidence,
        detail=orientation + sign + disagreement,
    )


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
    # "segmented walls" projects the full height of every wall, so a doorway
    # is covered by the wall above it and is not a gap either — the same reason
    # the ceiling band can reach far.
    reach = 0.08 if band_basis in ("ceiling-adjacent", "segmented walls") else 0.02
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

def _offset_polygon(np, polygon, distance: float):
    """Push a simple polygon outward by `distance`, mitred at the corners.

    Vertex-bisector offsetting rather than a raster dilate: it keeps the exact
    corner coordinates for the rectilinear rooms this pipeline produces, where
    a rasterised offset would round every corner and lose a little area back.
    """
    count = len(polygon)
    if count < 3:
        return polygon

    points = np.asarray(polygon, dtype="float64")
    # Winding decides which way is out. Shoelace: positive is counter-clockwise.
    area2 = float(np.sum(
        points[:, 0] * np.roll(points[:, 1], -1)
        - np.roll(points[:, 0], -1) * points[:, 1]
    ))
    if area2 == 0:
        return polygon
    orientation = 1.0 if area2 > 0 else -1.0

    out = []
    for index in range(count):
        previous = points[index - 1]
        current = points[index]
        following = points[(index + 1) % count]

        def _normal(a, b):
            edge = b - a
            length = float(np.hypot(*edge))
            if length < 1e-12:
                return None
            # Outward normal for this winding.
            return np.array([edge[1], -edge[0]]) / length * orientation

        n1 = _normal(previous, current)
        n2 = _normal(current, following)
        if n1 is None or n2 is None:
            out.append(tuple(current))
            continue

        bisector = n1 + n2
        norm = float(np.hypot(*bisector))
        if norm < 1e-9:                       # a spike; step straight out
            out.append(tuple(current + n1 * distance))
            continue
        bisector /= norm
        # Mitre length grows as the corner sharpens; cap it so a near-degenerate
        # vertex cannot fling a corner across the room.
        scale = min(4.0, 1.0 / max(float(np.dot(bisector, n1)), 0.25))
        out.append(tuple(current + bisector * distance * scale))
    return out


def _expand_rooms_to_the_captured_surface(np, document, metres_per_pixel=None) -> None:
    """Undo a double inset that made every room read ~5.5% small.

    A reconstruction sees the INSIDE FACES of walls — that is the surface the
    camera looked at — so the stroke this pipeline rasterises already sits at
    the face, not on a centreline. detect_rooms then takes the region *inside*
    that stroke, insetting a second time by roughly half its width, and the
    room comes back smaller than the room actually is.

    Measured over 100 rooms before this: signed error -5.61% with a standard
    deviation of only 1.65%. A bias that tight is a geometry mistake, not noise,
    and correcting it moves area accuracy from 94.5% to about 98.7%.

    Note this is the opposite of correct for a DRAWN floor plan, where the
    printed stroke represents the wall's full thickness and measuring inside it
    is exactly right. That is why this lives here and not in raster.py, which
    the image path shares.
    """
    if not document.rooms or not document.walls:
        return
    thickness = float(np.median([wall.thickness for wall in document.walls]))
    if thickness <= 0:
        return
    inset = thickness / 2.0
    if metres_per_pixel:
        # detect_rooms dilates the sealed wall mask by a 3x3 kernel before
        # inverting it — "thicken walls slightly so hairline gaps don't leak" —
        # which insets every room by one more pixel that the recorded wall
        # thickness does not include. Small, but it is a known constant rather
        # than a guess, so it is corrected rather than absorbed.
        inset += float(metres_per_pixel)
    for room in document.rooms:
        room.polygon = [
            (round(x, 4), round(y, 4))
            for x, y in _offset_polygon(np, room.polygon, inset)
        ]


def _recalibrate_to_known_total(document, known_total_sqft: float) -> None:
    """Restore the total the caller asserted, after the outward offset.

    `raster.resolve_scale` solves metres-per-pixel precisely so the rooms sum to
    `known_total_sqft`. `_expand_rooms_to_the_captured_surface` then pushes every
    room polygon outward by half a wall thickness, which is correct geometry but
    breaks that equality: a caller passing 1800 sqft got roughly 1900 back. On
    this path there is no other anchor to re-derive from, so the inflation was
    pure error against a number the caller supplied as ground truth.

    Rescaling the whole plan about its own centroid is exactly equivalent to
    having solved a slightly different metres-per-pixel, so rooms, walls and
    openings stay consistent with each other and with the asserted total.
    """
    from .raster import SQFT_PER_M2

    area = document.total_area_m2
    if area <= 0:
        return
    factor = math.sqrt((known_total_sqft / SQFT_PER_M2) / area)
    if not math.isfinite(factor) or factor <= 0 or abs(factor - 1.0) < 1e-9:
        return

    points = [p for room in document.rooms for p in room.polygon]
    if not points:
        return
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)

    def _about_centre(point):
        return (round(cx + (point[0] - cx) * factor, 4),
                round(cy + (point[1] - cy) * factor, 4))

    for room in document.rooms:
        room.polygon = [_about_centre(p) for p in room.polygon]
    for wall in document.walls:
        wall.start, wall.end = _about_centre(wall.start), _about_centre(wall.end)
        wall.thickness = round(wall.thickness * factor, 4)
        wall.height = round(wall.height * factor, 4)
    for opening in document.openings:
        opening.width = round(opening.width * factor, 4)
        opening.height = round(opening.height * factor, 4)


#: A plan's rooms should tile its own footprint. Less than this and something
#: leaked; the interior escaped through an unclosed corner, merged with the
#: exterior background and was discarded as such.
MIN_PLAUSIBLE_COVERAGE = 0.88

#: Rooms cannot cover more than the footprint. Above this and a phantom region
#: — usually the exterior ring — is being counted as a room.
MAX_PLAUSIBLE_COVERAGE = 1.02

#: What a correct plan looks like: rooms fill the footprint minus wall thickness.
#:
#: Measured, not assumed, and re-measured after `_expand_rooms_to_the_captured_
#: surface` began inflating every room polygon. Over 100 plans whose room count
#: was exactly right, coverage ran 0.946 to 0.994 with a median of 0.980; the
#: catastrophic cases — a room lost through an unclosed corner — sat at 0.49,
#: 0.70, 0.72 and 0.85. These three numbers were tuned before the expansion
#: existed and were never revisited, so the selector was aiming at 0.90: a
#: coverage no correct plan produces any more, which made
#: `min(pool, key=abs(cov - TARGET))` systematically prefer whichever candidate
#: had LOST geometry.
TARGET_COVERAGE = 0.97


def _coverage(document) -> Optional[float]:
    """Room area as a fraction of the plan's own bounding box.

    A self-check that needs no ground truth, which is what makes it usable at
    run time. The two failure modes this pipeline actually has are both visible
    in it: losing a room to an unclosed corner halves the coverage, and counting
    the exterior ring as a room pushes it past one.
    """
    if not document.walls or not document.rooms:
        return None
    xs = [p[0] for wall in document.walls for p in (wall.start, wall.end)]
    ys = [p[1] for wall in document.walls for p in (wall.start, wall.end)]
    footprint = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if footprint <= 0:
        return None
    return document.total_area_m2 / footprint


def extract_from_reconstruction(ply_bytes: bytes, *, use_segmenter="auto", **kwargs):
    """A FloorplanDocument from a reconstruction of the property.

    `use_segmenter` is "auto" by default: run both the geometric and the
    segmented path and keep whichever produces the more self-consistent plan.

    Running both is worth its cost because the two fail on DIFFERENT houses.
    Measured over 100 random rooms they score the same on average — 93.0% area
    accuracy each, three catastrophic cases each — but not the same three. The
    segmenter recovered every case where geometry lost a whole room to an
    unclosed corner (one returned 38.4 m² of a true 73.7); geometry got cases
    the segmenter did not.

    Neither can be preferred a priori, so this measures instead. Coverage —
    room area over the plan's own footprint — detects both failure modes
    without ground truth, so the choice is made on evidence rather than on a
    default that is wrong half the time.
    """
    if use_segmenter in (True, False):
        return _extract_once(ply_bytes, use_segmenter=use_segmenter, **kwargs)

    # Only run the segmented attempt when there is actually a model to run.
    # Without one `segment()` returns None, `wall_points` stays None, and
    # `_extract_once(use_segmenter=True)` takes the identical geometric branch —
    # so every reconstruction paid for two full parse + up-axis + raster passes
    # to produce two identical documents. segmentation.py is explicit that the
    # model is optional and often absent, which makes that the common case.
    installed, _ = segmentation.available()
    modes = (False, True) if installed else (False,)

    attempts = []
    for segmented in modes:
        try:
            document = _extract_once(ply_bytes, use_segmenter=segmented, **kwargs)
        except (DegenerateGeometry, UnsupportedInput) as exc:
            attempts.append((None, None, exc))
            continue
        except Exception as exc:  # noqa: BLE001
            # The segmented attempt is the experimental one and it is never
            # load-bearing. Anything it raises that the geometric attempt did
            # not must not destroy a document that already succeeded — the
            # previous form let it propagate, discarding a good plan.
            if not segmented:
                raise
            log.exception("Segmented attempt failed; keeping the geometric plan")
            attempts.append((None, None, exc))
            continue
        attempts.append((document, _coverage(document), None))

    scored = [(doc, cov, segmented)
              for (doc, cov, exc), segmented in zip(attempts, modes)
              if doc is not None]
    if not scored:
        # Both paths refused. Re-raise the geometric path's reason: it is the
        # one that has been verified, so its diagnosis is the more trustworthy.
        raise next(exc for _, _, exc in attempts if exc is not None)

    plausible = [
        item for item in scored
        if item[1] is not None and MIN_PLAUSIBLE_COVERAGE <= item[1] <= MAX_PLAUSIBLE_COVERAGE
    ]
    pool = plausible or scored

    # Coverage decides which candidates are PLAUSIBLE. It does not decide
    # between two that both are.
    #
    # That filter is what earns this mode its keep: it catches the plan that
    # leaked a room through an unclosed corner, which lands near 0.5 or 0.7
    # while a correct plan sits at 0.95-0.99. But once both candidates are
    # inside that band their coverages differ by a couple of points of nothing,
    # and picking the one nearer TARGET_COVERAGE is a coin flip dressed as a
    # measurement — measured over 80 houses it threw away enough correct
    # segmented plans to score 95.0% where the segmenter alone scored 96.2%.
    #
    # So among plausible candidates, prefer the segmented one, on the same
    # evidence: 96.2% correct room counts against geometry's 92.5%, and 98.92%
    # area against 98.21%. That is a claim about the CURRENT model, so it is
    # re-measurable — scripts/eval_floorplan.py prints all three modes, and if a
    # future model loses to geometry there, this preference is what to revisit.
    preferred = [item for item in pool if item[2]] or pool
    best, coverage, chosen_segmenter = min(
        preferred,
        key=lambda item: abs((item[1] if item[1] is not None else 0.0) - TARGET_COVERAGE),
    )
    if coverage is not None:
        best.provenance.notes = " ".join(filter(None, [
            best.provenance.notes,
            f"Chosen by self-consistency ({'segmented' if chosen_segmenter else 'geometric'}): "
            f"rooms cover {coverage:.0%} of the footprint.",
        ]))
    return best


def _extract_once(
    ply_bytes: bytes,
    *,
    metres_per_unit: Optional[float] = None,
    parcel_footprint_m2: Optional[float] = None,
    known_total_sqft: Optional[float] = None,
    camera_positions=None,
    min_opacity: float = MIN_OPACITY,
    use_segmenter: bool = False,
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

    up = estimate_up_axis(
        xyz, camera_positions=camera_positions, metres_per_unit=metres_per_unit
    )
    axis = np.asarray(up.vector, dtype="float64")
    heights = xyz @ axis
    right, forward = _ground_basis(np, axis)
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)
    profile = vertical_profile(heights, planar)

    # Use the model only when asked.
    #
    # It defaults OFF on measured evidence, which reverses an earlier claim in
    # this file. The segmenter looked like it rescued captures with no ceiling,
    # where the height band raised DegenerateGeometry. It did not: the real
    # cause was a broken up-axis estimate, and the model merely took a
    # different code path that happened to survive it. With the up-axis fixed,
    # geometry alone scores 99.44% dimension accuracy with zero failures over
    # 40 random rooms, against the segmenter's 99.27% and one failure.
    #
    # The capability is kept because clutter classification should help a
    # heavily furnished REAL capture, which no synthetic test here exercises.
    # But it stays off until it demonstrates that, rather than being on because
    # it sounds like it should help.
    #
    # The band is a proxy: it assumes anything high up is a wall, which is why
    # it needs a ceiling to aim below and why a tall wardrobe still fools it.
    # When the segmenter is installed, wall points are known, so the whole
    # height of every wall can be projected — which also gives markedly better
    # corner coverage than a thin slab.
    #
    # It is a preference and not a requirement. Everything below works without
    # a model, and the geometric path is the one verified against known
    # dimensions.
    wall_points = None
    if use_segmenter:
        labels = segmentation.segment(
            xyz, up.vector, floor=profile.floor, ceiling=profile.ceiling
        )
        if labels is not None:
            chosen = xyz[labels == LABEL_WALL]
            if len(chosen) >= 256:
                wall_points = chosen
            else:
                log.warning(
                    "Segmenter labelled only %d points as wall; using the height band.",
                    len(chosen),
                )

    if wall_points is not None:
        band_basis = "segmented walls"
        wall_heights = wall_points @ axis
        lo, hi = float(wall_heights.min()), float(wall_heights.max())
        image_bytes, units_per_pixel = occupancy_raster(
            wall_points, up, lo, hi, band_basis=band_basis
        )
    else:
        lo, hi, band_basis = choose_slice_band(profile, heights)
        image_bytes, units_per_pixel = occupancy_raster(
            xyz, up, lo, hi, band_basis=band_basis
        )

    # Scale is measured from the GEOMETRIC band in both paths, deliberately.
    #
    # `lo`/`hi` above are a thin ceiling-adjacent slab without the segmenter and
    # the full floor-to-ceiling wall range with it, and those same bounds used
    # to be handed to `footprint_area_units2`. The hulls differ — the wide band
    # sweeps in floor, ceiling and any photogrammetric floaters outside the
    # building — so `metres_per_unit = sqrt(parcel_footprint_m2 / footprint)`
    # came out different depending on whether a model happened to be installed.
    # That is a learned scale by the back door: every length and area in the
    # document multiplied by a different constant, which segmentation.py's own
    # docstring names as the failure that "looks entirely correct". Worse,
    # `_coverage` is area over area and therefore scale-invariant, so the
    # self-consistency selector is structurally blind to it.
    footprint_units2 = None
    if parcel_footprint_m2:
        try:
            scale_lo, scale_hi, _ = choose_slice_band(profile, heights)
        except DegenerateGeometry:
            scale_lo, scale_hi = lo, hi
        footprint_units2 = footprint_area_units2(xyz, up, scale_lo, scale_hi)

    anchor = resolve_scale_anchor(
        metres_per_unit=metres_per_unit,
        parcel_footprint_m2=parcel_footprint_m2,
        footprint_units2=footprint_units2,
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

    if band_basis in ("ceiling-adjacent", "segmented walls") and document.openings:
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

    _expand_rooms_to_the_captured_surface(np, document, metres_per_pixel)
    if anchor is None and known_total_sqft:
        _recalibrate_to_known_total(document, known_total_sqft)
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
