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


def test_total_area_is_close_and_biased_low():
    """Rooms are measured inside the wall strokes, so the result under-reads
    slightly. Under-reading is the safe direction for a rehab estimate."""
    doc = _extract(np.random.default_rng(7))
    truth = W * D

    assert doc.total_area_m2 < truth
    assert doc.total_area_m2 > truth * 0.85


# ---------------------------------------------------------------------------
# Scale — never invented
# ---------------------------------------------------------------------------

def test_no_anchor_means_no_plan():
    """A reconstruction has no metric scale at all. A guessed one multiplies
    every length and area by an arbitrary constant and looks entirely correct."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    with pytest.raises(MissingScale):
        slicing.extract_from_reconstruction(_ply(pts))


def test_a_surveyed_parcel_footprint_anchors_the_scale():
    """The strongest anchor Oracle can produce unaided: parcel.py's polygons
    are exact, so matching against one puts the plan in real metres."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    doc = slicing.extract_from_reconstruction(_ply(pts), parcel_footprint_m2=W * D)

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

    blind = slicing.extract_from_reconstruction(_ply(pts), metres_per_unit=1.0)
    sighted = slicing.extract_from_reconstruction(
        _ply(pts), metres_per_unit=1.0, camera_positions=eye)

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
        _ply(pts, opacity), metres_per_unit=1.0)

    xs = [p[0] for w in doc.walls for p in (w.start, w.end)]
    ys = [p[1] for w in doc.walls for p in (w.start, w.end)]
    got = sorted([max(xs) - min(xs), max(ys) - min(ys)])
    # Without the opacity filter the extent would be the floater cloud, not the house.
    for actual, expected in zip(got, sorted([W, D])):
        assert abs(actual - expected) / expected < 0.15
