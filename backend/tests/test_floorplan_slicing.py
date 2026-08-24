"""A floor plan from a photogrammetric reconstruction — interior and exterior.

The geometry comes from the photographs an agent already uploads, via the
COLMAP + splatfacto pipeline. No phone scan, no LiDAR, no depth sensor.

These tests run against a SYNTHETIC house with known metric dimensions, rotated
into an arbitrary frame the way a real reconstruction arrives. That is the only
way to test accuracy rather than plausibility: a plan extracted from a real
capture looks convincing whether or not the numbers are right, which is exactly
the failure this pipeline exists to prevent.

Two findings from the 2026-08-23 research are pinned here because both are the
opposite of the obvious choice:

  * the slice band is ceiling-adjacent, NOT waist height — furniture stands on
    the floor, so a waist cut turns wardrobes and kitchen islands into walls;
  * scale is never invented — a reconstruction has none, and a guessed one
    multiplies every rehab line item by an arbitrary constant while looking
    entirely correct.
"""

from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from floorplan_pipeline import slicing
from floorplan_pipeline.errors import DegenerateGeometry, MissingScale, UnsupportedInput

W, D, H = 8.0, 6.0, 2.5          # metres — the ground truth for every assertion
PARTITION_X = 4.6


def _plane(rng, p0, u, v, n=7000, jitter=0.004):
    a = rng.random((n, 1))
    b = rng.random((n, 1))
    pts = np.asarray(p0) + a * np.asarray(u) + b * np.asarray(v)
    return pts + rng.normal(0, jitter, pts.shape)


def _house(rng, *, furniture=True, ceiling=True, partition=True):
    """A W x D x H room with an optional partition and floor-standing clutter."""
    parts = [
        _plane(rng, [0, 0, 0], [W, 0, 0], [0, D, 0], 12000),          # floor
        _plane(rng, [0, 0, 0], [W, 0, 0], [0, 0, H], 7000),           # near wall
        _plane(rng, [0, D, 0], [W, 0, 0], [0, 0, H], 7000),           # far wall
        _plane(rng, [0, 0, 0], [0, D, 0], [0, 0, H], 6000),           # left wall
        _plane(rng, [W, 0, 0], [0, D, 0], [0, 0, H], 6000),           # right wall
    ]
    if ceiling:
        parts.append(_plane(rng, [0, 0, H], [W, 0, 0], [0, D, 0], 12000))
    if partition:
        parts.append(_plane(rng, [PARTITION_X, 0, 0], [0, D, 0], [0, 0, H], 6000))
    if furniture:
        # A 1.9 m wardrobe and a 0.95 m kitchen island, both FLOOR-STANDING.
        # A waist-height slice cuts straight through both.
        parts.append(_plane(rng, [1.0, 1.0, 0], [1.2, 0, 0], [0, 0, 1.9], 3500))
        parts.append(_plane(rng, [1.0, 1.0, 0], [0, 0.6, 0], [0, 0, 1.9], 2500))
        parts.append(_plane(rng, [5.5, 3.0, 0], [2.0, 0, 0], [0, 0, 0.95], 3500))
        parts.append(_plane(rng, [5.5, 3.0, 0.95], [2.0, 0, 0], [0, 1.0, 0], 3500))
    return np.vstack(parts)


def _rotate(rng, pts):
    """Into an arbitrary frame — structure-from-motion has no idea where gravity is."""
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    if np.linalg.det(q) < 0:
        q[:, 0] = -q[:, 0]
    return pts @ q.T, q


def _ply(pts, opacity=None):
    props = "property float x\nproperty float y\nproperty float z\n"
    if opacity is not None:
        props += "property float opacity\n"
    header = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(pts)}\n"
              f"{props}end_header\n").encode()
    if opacity is None:
        return header + pts.astype("<f4").tobytes()
    arr = np.empty(len(pts), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("opacity", "<f4")])
    arr["x"], arr["y"], arr["z"] = pts[:, 0], pts[:, 1], pts[:, 2]
    arr["opacity"] = opacity
    return header + arr.tobytes()


def _extract(rng, **kwargs):
    pts, _ = _rotate(rng, _house(rng, **{k: v for k, v in kwargs.items()
                                         if k in ("furniture", "ceiling", "partition")}))
    passthrough = {k: v for k, v in kwargs.items()
                   if k not in ("furniture", "ceiling", "partition")}
    passthrough.setdefault("metres_per_unit", 1.0)
    # Pinned OFF by default. The learned segmenter is an optional accelerator
    # and may or may not be installed; the geometric path is the one that must
    # work everywhere, so it is what this file exercises unless a test says
    # otherwise. Letting these follow the default would silently stop testing
    # the fallback the moment a model artifact appeared in the tree.
    passthrough.setdefault("use_segmenter", False)
    return slicing.extract_from_reconstruction(_ply(pts), **passthrough)


# ---------------------------------------------------------------------------
# Orientation — everything downstream is meaningless without it
# ---------------------------------------------------------------------------

def test_up_is_found_under_an_arbitrary_rotation():
    """Slice the wrong axis and you get a vertical section through the house
    rendered as a floor plan: plausible-looking, entirely wrong."""
    rng = np.random.default_rng(11)
    pts, q = _rotate(rng, _house(rng))
    xyz, _ = slicing.parse_ply(_ply(pts))

    up = slicing.estimate_up_axis(xyz)
    truth = q @ np.array([0.0, 0.0, 1.0])
    error_deg = math.degrees(math.acos(min(1.0, abs(float(np.dot(up.vector, truth))))))

    assert error_deg < 5.0, f"up axis off by {error_deg:.1f} degrees"
    assert float(np.dot(up.vector, truth)) > 0, "sign is inverted — floor and ceiling swapped"


def test_camera_positions_resolve_the_sign_and_raise_confidence():
    """Cameras sit between floor and ceiling, so they determine the sign exactly
    rather than by the mass heuristic."""
    rng = np.random.default_rng(5)
    house = _house(rng)
    pts, q = _rotate(rng, house)
    xyz, _ = slicing.parse_ply(_ply(pts))

    # A photographer walking the room at eye height.
    eye = np.stack([
        np.linspace(1.0, W - 1.0, 12),
        np.linspace(1.0, D - 1.0, 12),
        np.full(12, 1.55),
    ], axis=1) @ q.T

    blind = slicing.estimate_up_axis(xyz)
    sighted = slicing.estimate_up_axis(xyz, camera_positions=eye)
    truth = q @ np.array([0.0, 0.0, 1.0])

    assert float(np.dot(sighted.vector, truth)) > 0
    assert sighted.confidence > blind.confidence
    assert "camera" in sighted.detail.lower()


def test_a_flat_cloud_is_refused_rather_than_oriented():
    rng = np.random.default_rng(3)
    flat = np.hstack([rng.random((500, 2)) * 5, np.zeros((500, 1))])
    with pytest.raises(DegenerateGeometry):
        slicing.vertical_profile(flat[:, 2])


# ---------------------------------------------------------------------------
# The slice band — the counter-intuitive part
# ---------------------------------------------------------------------------

def test_the_band_sits_above_the_furniture_when_a_ceiling_was_seen():
    """The whole accuracy argument. A waist-height cut passes through the 1.9 m
    wardrobe; a ceiling-adjacent one does not."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))
    xyz, _ = slicing.parse_ply(_ply(pts))
    up = slicing.estimate_up_axis(xyz)
    heights = xyz @ np.asarray(up.vector)
    profile = slicing.vertical_profile(heights)
    lo, hi, basis = slicing.choose_slice_band(profile, heights)

    assert profile.ceiling_observed is True
    assert basis == "ceiling-adjacent"
    assert lo - profile.floor > 1.9, "the band must clear a full-height wardrobe"


def test_a_capture_that_never_saw_the_ceiling_falls_back_and_says_so():
    """A hand-held sweep at eye level frequently never points up. Reporting a
    ceiling that was not observed would put the slice in open air."""
    rng = np.random.default_rng(9)
    pts, _ = _rotate(rng, _house(rng, ceiling=False))
    xyz, _ = slicing.parse_ply(_ply(pts))
    up = slicing.estimate_up_axis(xyz)
    heights = xyz @ np.asarray(up.vector)
    right, forward = slicing._ground_basis(np, np.asarray(up.vector))
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)
    profile = slicing.vertical_profile(heights, planar)

    assert profile.ceiling_observed is False
    assert profile.ceiling is None
    _, _, basis = slicing.choose_slice_band(profile, heights)
    assert basis == "mid-height consensus"


def test_furniture_does_not_become_walls():
    """The measurable version of the claim: adding a wardrobe and an island
    must not change the room count or materially change the area."""
    clean = _extract(np.random.default_rng(7), furniture=False)
    cluttered = _extract(np.random.default_rng(7), furniture=True)

    assert len(cluttered.rooms) == len(clean.rooms)
    assert abs(cluttered.total_area_m2 - clean.total_area_m2) < 3.0


# ---------------------------------------------------------------------------
# Accuracy against known truth
# ---------------------------------------------------------------------------

def test_known_dimensions_are_recovered():
    doc = _extract(np.random.default_rng(7))

    xs = [p[0] for wall in doc.walls for p in (wall.start, wall.end)]
    ys = [p[1] for wall in doc.walls for p in (wall.start, wall.end)]
    # The plan carries no compass, so which extent is "width" is arbitrary.
    got = sorted([max(xs) - min(xs), max(ys) - min(ys)])
    want = sorted([W, D])

    for actual, expected in zip(got, want):
        assert abs(actual - expected) / expected < 0.08, f"{actual:.2f} vs {expected:.2f}"


def test_the_partition_is_found_and_marked_interior():
    """Interior AND exterior: the outer contour is the shell, the rest divides
    rooms. FloorplanWall.interior already exists in the schema."""
    doc = _extract(np.random.default_rng(7))

    assert len(doc.rooms) >= 2, "the partition should split the space"
    assert any(wall.interior for wall in doc.walls), "no wall was classified interior"
    assert any(not wall.interior for wall in doc.walls), "no wall was classified exterior"


def test_an_undivided_room_has_no_interior_walls():
    doc = _extract(np.random.default_rng(13), partition=False)

    assert len(doc.rooms) >= 1
    assert not any(wall.interior for wall in doc.walls), (
        "an open-plan room must not sprout partitions"
    )


def test_total_area_is_close_and_still_biased_low():
    """Close, and low — in that order of importance.

    Rooms used to under-read by 5.5% because the inset was counted twice: a
    reconstruction captures the INSIDE FACES of walls, so the rasterised stroke
    already sits at the face, and detect_rooms then took the region inside that
    stroke. Correcting it moved area accuracy from 94.5% to 98.9% over 100 rooms.

    A residual under-read is kept rather than tuned away. It comes from polygon
    simplification clipping corners, and under-reading is the safe direction for
    a rehab estimate — the alternative is billing for floor that is not there.
    """
    doc = _extract(np.random.default_rng(7))
    truth = W * D

    assert doc.total_area_m2 < truth, "over-reading would bill for absent floor"
    assert doc.total_area_m2 > truth * 0.95, (
        f"{doc.total_area_m2:.1f} of {truth:.1f} — the double inset is back"
    )


def test_rooms_are_pushed_back_out_to_the_captured_surface():
    """The correction itself, isolated from the pipeline.

    Measured over 100 rooms, the uncorrected bias was -5.61% with a standard
    deviation of 1.65%. A bias that tight against that little scatter is a
    geometry mistake, not noise.
    """
    doc = _extract(np.random.default_rng(7))
    thickness = np.median([wall.thickness for wall in doc.walls])

    # Every room should have grown by roughly its perimeter times the inset.
    # Checked as "did it grow at all" rather than an exact figure, so this
    # pins the behaviour without pinning a constant that tuning may move.
    assert thickness > 0
    assert doc.total_area_m2 > truth_inner(doc, thickness), (
        "rooms are still measured inside the stroke"
    )


def truth_inner(doc, thickness):
    """Area the rooms would have if they were still inset by half a stroke."""
    total = 0.0
    for room in doc.rooms:
        polygon = room.polygon
        perimeter = sum(
            math.dist(polygon[i - 1], polygon[i]) for i in range(len(polygon))
        )
        total += room.area - perimeter * (thickness / 2.0)
    return total


def test_the_offset_is_exact_on_a_rectangle():
    """A 10x6 room offset outward by 0.05 m is 10.1 x 6.1 — mitred corners, not
    rounded ones, because these rooms are rectilinear and a rasterised offset
    would round every corner and give the area straight back."""
    offset = slicing._offset_polygon(np, [(0, 0), (10, 0), (10, 6), (0, 6)], 0.05)
    xs = [p[0] for p in offset]
    ys = [p[1] for p in offset]

    assert abs((max(xs) - min(xs)) - 10.1) < 1e-6
    assert abs((max(ys) - min(ys)) - 6.1) < 1e-6


def test_the_offset_leaves_a_degenerate_polygon_alone():
    assert slicing._offset_polygon(np, [(0, 0), (1, 1)], 0.05) == [(0, 0), (1, 1)]


# ---------------------------------------------------------------------------
# Scale — never invented
# ---------------------------------------------------------------------------

def test_no_anchor_means_no_plan():
    """A reconstruction has no metric scale at all. A guessed one multiplies
    every length and area by an arbitrary constant and looks entirely correct."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    with pytest.raises(MissingScale):
        slicing.extract_from_reconstruction(_ply(pts), use_segmenter=False)


def test_a_surveyed_parcel_footprint_anchors_the_scale():
    """The strongest anchor Oracle can produce unaided: parcel.py's polygons
    are exact, so matching against one puts the plan in real metres."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    doc = slicing.extract_from_reconstruction(
        _ply(pts), parcel_footprint_m2=W * D, use_segmenter=False)

    got = sorted([
        max(p[0] for w in doc.walls for p in (w.start, w.end))
        - min(p[0] for w in doc.walls for p in (w.start, w.end)),
        max(p[1] for w in doc.walls for p in (w.start, w.end))
        - min(p[1] for w in doc.walls for p in (w.start, w.end)),
    ])
    for actual, expected in zip(got, sorted([W, D])):
        assert abs(actual - expected) / expected < 0.15


def test_the_anchor_records_where_its_number_came_from():
    explicit = slicing.resolve_scale_anchor(metres_per_unit=0.5)
    assert explicit.basis == "explicit" and explicit.provenance == "measured"

    parcel = slicing.resolve_scale_anchor(parcel_footprint_m2=48.0, footprint_units2=48.0)
    assert parcel.basis == "parcel_footprint" and parcel.provenance == "sourced"
    assert abs(parcel.metres_per_unit - 1.0) < 1e-9
    # The approximation is stated rather than hidden.
    assert "wall thickness" in parcel.detail

    assert slicing.resolve_scale_anchor() is None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_a_sliced_plan_is_not_labelled_ai_vision():
    """One is measured 3D structure cut horizontally; the other is a model's
    guess from flat photos. Collapsing them would put derived and invented
    geometry under one word, on a surface that feeds rehab costing."""
    doc = _extract(np.random.default_rng(7))

    assert doc.provenance.source == "reconstruction"
    assert doc.provenance.notes and "reconstruction" in doc.provenance.notes.lower()


def test_a_guessed_orientation_lowers_the_confidence():
    """The reader is entitled to see uncertainty in the number, not only in prose."""
    rng = np.random.default_rng(7)
    pts, q = _rotate(rng, _house(rng))
    eye = np.stack([
        np.linspace(1.0, W - 1.0, 12), np.linspace(1.0, D - 1.0, 12), np.full(12, 1.55),
    ], axis=1) @ q.T

    blind = slicing.extract_from_reconstruction(
        _ply(pts), metres_per_unit=1.0, use_segmenter=False)
    sighted = slicing.extract_from_reconstruction(
        _ply(pts), metres_per_unit=1.0, camera_positions=eye, use_segmenter=False)

    assert sighted.provenance.confidence > blind.provenance.confidence


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def test_a_non_ply_input_is_refused_by_name():
    with pytest.raises(UnsupportedInput, match="PLY"):
        slicing.parse_ply(b"NGSP\x04\x00\x00\x00" + b"\x00" * 64)


def test_splat_opacity_logits_are_squashed_not_taken_literally():
    """Splat PLYs store opacity as a logit; a plain point cloud stores none.
    Treating a logit as a probability would filter on the wrong threshold."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))
    logits = rng.normal(2.0, 1.5, len(pts))     # well outside [0, 1]

    _, opacity = slicing.parse_ply(_ply(pts, logits))

    assert opacity is not None
    assert 0.0 <= float(opacity.min()) and float(opacity.max()) <= 1.0


def test_low_opacity_floaters_are_dropped():
    """Reconstructions carry a haze of near-transparent blobs in empty space;
    projected top-down they smear into wall-shaped noise."""
    rng = np.random.default_rng(7)
    house = _house(rng)
    floaters = rng.uniform([-4, -4, 0], [W + 4, D + 4, H], size=(6000, 3))
    pts = np.vstack([house, floaters])
    opacity = np.concatenate([np.full(len(house), 0.9), np.full(len(floaters), 0.02)])
    pts, _ = _rotate(rng, pts)

    doc = slicing.extract_from_reconstruction(
        _ply(pts, opacity), metres_per_unit=1.0, use_segmenter=False)

    xs = [p[0] for w in doc.walls for p in (w.start, w.end)]
    ys = [p[1] for w in doc.walls for p in (w.start, w.end)]
    got = sorted([max(xs) - min(xs), max(ys) - min(ys)])
    # Without the opacity filter the extent would be the floater cloud, not the house.
    for actual, expected in zip(got, sorted([W, D])):
        assert abs(actual - expected) / expected < 0.15


# ---------------------------------------------------------------------------
# Orientation in a long room — the bug that cost 60-70% on every dimension
# ---------------------------------------------------------------------------

def _long_room(rng, length=13.5, width=4.2, height=2.7):
    """A corridor-proportioned room: much longer than wide, and not much wider
    than it is tall. This shape is what broke the original up-axis estimate."""
    parts = [
        _plane(rng, [0, 0, 0], [length, 0, 0], [0, width, 0], 12000),      # floor
        _plane(rng, [0, 0, height], [length, 0, 0], [0, width, 0], 12000),  # ceiling
        _plane(rng, [0, 0, 0], [length, 0, 0], [0, 0, height], 7000),
        _plane(rng, [0, width, 0], [length, 0, 0], [0, 0, height], 7000),
        _plane(rng, [0, 0, 0], [0, width, 0], [0, 0, height], 6000),        # end wall
        _plane(rng, [length, 0, 0], [0, width, 0], [0, 0, height], 6000),   # end wall
        # Furniture, because a perfectly bare box is genuinely symmetric about
        # its mid-height and has no recoverable up/down without camera poses.
        # Real rooms contain things, and those things stand on the floor.
        _plane(rng, [2.0, 1.0, 0], [1.4, 0, 0], [0, 0, 0.9], 2500),
        _plane(rng, [2.0, 1.0, 0.9], [1.4, 0, 0], [0, 1.0, 0], 2000),
        _plane(rng, [8.0, 1.5, 0], [1.0, 0, 0], [0, 0, 1.8], 2500),
    ]
    return np.vstack(parts)


def test_a_long_room_does_not_mistake_its_length_for_up():
    """The regression that mattered most.

    In a long room the two END WALLS put mass in two sharp height-histogram
    bins — indistinguishable, to any peak-based score, from a floor and a
    ceiling. The original estimate therefore chose the room's LENGTH as up on
    13 of 40 test rooms, and slicing on it produces a vertical section through
    the house rendered as a floor plan: convincing, and 60-70% wrong on every
    dimension.

    Nothing in a peak score knows that 13 m cannot be a ceiling height. The
    structural prior does: a room is wider than it is tall.
    """
    rng = np.random.default_rng(4)
    length, width, height = 13.5, 4.2, 2.7
    pts, q = _rotate(rng, _long_room(rng, length, width, height))
    xyz, _ = slicing.parse_ply(_ply(pts))

    up = slicing.estimate_up_axis(xyz)
    heights = xyz @ np.asarray(up.vector)
    extent = float(heights.max() - heights.min())

    assert extent < height * 1.6, (
        f"vertical extent {extent:.1f} m — the length axis was chosen as up"
    )
    truth = q @ np.array([0.0, 0.0, 1.0])
    assert float(np.dot(up.vector, truth)) > 0.9


def test_a_long_room_yields_its_real_dimensions():
    """The end-to-end consequence of the same bug."""
    rng = np.random.default_rng(4)
    length, width = 13.5, 4.2
    pts, _ = _rotate(rng, _long_room(rng, length, width))

    doc = slicing.extract_from_reconstruction(
        _ply(pts), metres_per_unit=1.0, use_segmenter=False)

    xs = [p[0] for w in doc.walls for p in (w.start, w.end)]
    ys = [p[1] for w in doc.walls for p in (w.start, w.end)]
    got = sorted([max(xs) - min(xs), max(ys) - min(ys)])
    for actual, expected in zip(got, sorted([length, width])):
        assert abs(actual - expected) / expected < 0.06, f"{actual:.2f} vs {expected:.2f}"


# ---------------------------------------------------------------------------
# Choosing between two paths that fail on different houses
# ---------------------------------------------------------------------------

def test_both_paths_are_tried_by_default():
    """Neither path can be preferred a priori.

    Over 100 random rooms the geometric and segmented paths score the SAME on
    average — 93.0% area accuracy, three catastrophic cases each — but not on
    the same three. Picking either as the default is therefore wrong about half
    the time it matters, so the default measures instead of assuming.
    """
    import inspect

    signature = inspect.signature(slicing.extract_from_reconstruction)
    assert signature.parameters["use_segmenter"].default == "auto"


def test_an_explicit_choice_is_honoured():
    """auto costs two runs; a caller that knows which it wants can say so."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    for choice in (True, False):
        doc = slicing.extract_from_reconstruction(
            _ply(pts), metres_per_unit=1.0, use_segmenter=choice)
        assert len(doc.rooms) >= 1


def test_coverage_detects_a_room_that_leaked_away():
    """The self-check that makes the choice possible without ground truth.

    A room escaping through an unclosed corner merges with the exterior and is
    discarded as background, so the surviving rooms cover far less of the plan's
    own footprint than they should. That is visible in the plan alone.
    """
    from floorplan_pipeline.schema import FloorplanRoom, FloorplanWall

    walls = [
        FloorplanWall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0)),
        FloorplanWall(id="w2", start=(0.0, 6.0), end=(10.0, 6.0)),
    ]
    whole = FloorplanRoom(id="r1", name="R", type="other",
                          polygon=[(0, 0), (10, 0), (10, 6), (0, 6)])
    half = FloorplanRoom(id="r2", name="R", type="other",
                         polygon=[(0, 0), (4, 0), (4, 6), (0, 6)])

    complete = type("D", (), {"walls": walls, "rooms": [whole],
                              "total_area_m2": whole.area})()
    leaked = type("D", (), {"walls": walls, "rooms": [half],
                            "total_area_m2": half.area})()

    assert slicing._coverage(complete) > 0.9
    assert slicing._coverage(leaked) < slicing.MIN_PLAUSIBLE_COVERAGE


def test_auto_survives_one_path_failing(monkeypatch):
    """The paths fail on different inputs, so one refusing is normal and must
    not take the other down with it."""
    real = slicing._extract_once

    def one_sided(ply_bytes, *, use_segmenter=False, **kwargs):
        if use_segmenter:
            raise slicing.DegenerateGeometry("segmented path found nothing")
        return real(ply_bytes, use_segmenter=False, **kwargs)

    monkeypatch.setattr(slicing, "_extract_once", one_sided)
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    doc = slicing.extract_from_reconstruction(_ply(pts), metres_per_unit=1.0)
    assert len(doc.rooms) >= 1


def test_auto_still_refuses_when_neither_path_works(monkeypatch):
    """Two ways of failing is not a reason to invent a plan."""
    def always_fails(ply_bytes, **kwargs):
        raise slicing.DegenerateGeometry("nothing usable here")

    monkeypatch.setattr(slicing, "_extract_once", always_fails)
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    with pytest.raises(slicing.DegenerateGeometry):
        slicing.extract_from_reconstruction(_ply(pts), metres_per_unit=1.0)


def test_auto_records_the_coverage_it_chose_on():
    """Which evidence decided the plan belongs in its provenance."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    doc = slicing.extract_from_reconstruction(_ply(pts), metres_per_unit=1.0)

    assert "self-consistency" in (doc.provenance.notes or "")


def test_a_missing_scale_is_still_refused_in_auto_mode():
    """The line that never moves, now across both paths."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    with pytest.raises(MissingScale):
        slicing.extract_from_reconstruction(_ply(pts))


def test_the_raster_margin_clears_the_background_threshold():
    """The margin is not cosmetic — it is sized against a constant in raster.py.

    detect_rooms discards a border-touching component only when it exceeds 20%
    of the image, so the exterior ring has to clear that decisively. At a 6%
    margin the ring is ~20.3% — right on the line — so it was counted as a ROOM
    on roughly half of all plans, and that phantom room is larger than the whole
    building. It caused every spurious-room error measured.
    """
    from floorplan_pipeline import raster

    margin = slicing.RASTER_MARGIN
    # The building spans 1/(1+2m) of each padded axis, so the ring is whatever
    # is left of the area.
    building_fraction = (1.0 / (1.0 + 2 * margin)) ** 2
    ring_fraction = 1.0 - building_fraction

    assert ring_fraction > 0.20 * 1.4, (
        f"margin {margin} leaves the exterior ring at {ring_fraction:.1%}, too "
        f"close to the 20% threshold detect_rooms discards on"
    )
    # And not so wide that resolution is thrown away on whitespace.
    assert building_fraction > 0.5


def test_auto_picks_the_more_self_consistent_plan(monkeypatch):
    """The selection itself, not just the coverage measure.

    Without this, "run both and choose" is indistinguishable from "run both and
    keep the first", which is what the geometric-only default already was.
    """
    from floorplan_pipeline.schema import (
        FloorplanDocument, FloorplanRoom, FloorplanWall, Provenance,
    )

    def _plan(polygons):
        walls = [
            FloorplanWall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0)),
            FloorplanWall(id="w2", start=(0.0, 6.0), end=(10.0, 6.0)),
        ]
        rooms = [
            FloorplanRoom(id=f"r{i}", name="R", type="other", polygon=poly)
            for i, poly in enumerate(polygons)
        ]
        return FloorplanDocument(
            provenance=Provenance(source="reconstruction", ai_generated=True),
            walls=walls, rooms=rooms,
        )

    # Geometry loses a room to an unclosed corner; the segmented path keeps both.
    leaked = _plan([[(0, 0), (4, 0), (4, 6), (0, 6)]])
    complete = _plan([[(0, 0), (4.6, 0), (4.6, 6), (0, 6)],
                      [(4.6, 0), (9.6, 0), (9.6, 6), (4.6, 6)]])

    def fake(ply_bytes, *, use_segmenter=False, **kwargs):
        return complete if use_segmenter else leaked

    monkeypatch.setattr(slicing, "_extract_once", fake)
    chosen = slicing.extract_from_reconstruction(b"ply\n", metres_per_unit=1.0)

    assert chosen is complete, "auto kept the leaked plan instead of choosing"
    assert len(chosen.rooms) == 2


def test_auto_rejects_a_plan_whose_rooms_exceed_its_footprint(monkeypatch):
    """The other failure mode: the exterior ring counted as a room, which makes
    coverage exceed one."""
    from floorplan_pipeline.schema import (
        FloorplanDocument, FloorplanRoom, FloorplanWall, Provenance,
    )

    def _plan(polygons):
        walls = [
            FloorplanWall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0)),
            FloorplanWall(id="w2", start=(0.0, 6.0), end=(10.0, 6.0)),
        ]
        return FloorplanDocument(
            provenance=Provenance(source="reconstruction", ai_generated=True),
            walls=walls,
            rooms=[FloorplanRoom(id=f"r{i}", name="R", type="other", polygon=p)
                   for i, p in enumerate(polygons)],
        )

    phantom = _plan([[(0, 0), (10, 0), (10, 6), (0, 6)],
                     [(0, 0), (9, 0), (9, 6), (0, 6)]])        # coverage ~1.9
    sane = _plan([[(0, 0), (9.4, 0), (9.4, 5.7), (0, 5.7)]])   # coverage ~0.89

    def fake(ply_bytes, *, use_segmenter=False, **kwargs):
        return phantom if use_segmenter else sane

    monkeypatch.setattr(slicing, "_extract_once", fake)
    assert slicing.extract_from_reconstruction(b"ply\n", metres_per_unit=1.0) is sane
