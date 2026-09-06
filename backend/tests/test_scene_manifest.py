"""scene.json: one canonical frame, computed once, honest about its confidence.

A reconstruction arrives in whatever frame the solver chose. These tests build
a synthetic room, knock it over, and check the manifest stands it back up —
and that everything downstream can read the same answer.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

import scene_manifest

RNG = np.random.default_rng(7)


def _room(n_floor=6000, n_ceiling=6000, n_walls=6000, w=4.0, d=3.0, h=2.5):
    """A box: dense floor + ceiling planes and four walls, y up, floor at y=0."""
    fx = RNG.uniform(-w / 2, w / 2, n_floor); fz = RNG.uniform(-d / 2, d / 2, n_floor)
    floor = np.stack([fx, np.zeros(n_floor), fz], axis=1)
    cx = RNG.uniform(-w / 2, w / 2, n_ceiling); cz = RNG.uniform(-d / 2, d / 2, n_ceiling)
    ceiling = np.stack([cx, np.full(n_ceiling, h), cz], axis=1)
    k = n_walls // 4
    wy = RNG.uniform(0, h, k)
    walls = np.concatenate([
        np.stack([np.full(k, -w / 2), wy, RNG.uniform(-d / 2, d / 2, k)], axis=1),
        np.stack([np.full(k, w / 2), wy, RNG.uniform(-d / 2, d / 2, k)], axis=1),
        np.stack([RNG.uniform(-w / 2, w / 2, k), wy, np.full(k, -d / 2)], axis=1),
        np.stack([RNG.uniform(-w / 2, w / 2, k), wy, np.full(k, d / 2)], axis=1),
    ])
    return np.concatenate([floor, ceiling, walls])


def _rot(axis, degrees):
    a = np.asarray(axis, float); a /= np.linalg.norm(a)
    t = math.radians(degrees); c, s = math.cos(t), math.sin(t)
    x, y, z = a
    return np.array([
        [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
        [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
        [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
    ])


def _cameras(n=40, h=1.5, r=1.0):
    """A ring of camera positions at standing height inside the room."""
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.stack([r * np.cos(t), np.full(n, h), r * np.sin(t)], axis=1)


def _apply(m, points):
    m = np.asarray(m).reshape(4, 4)
    return points @ m[:3, :3].T + m[:3, 3]


@pytest.fixture
def knocked_over():
    """The room, rotated so its floor normal is nowhere near +Y, then shifted."""
    room = _room(); cams = _cameras()
    r = _rot([1, 0, 0], 90) @ _rot([0, 0, 1], 25)
    shift = np.array([3.0, -2.0, 5.0])
    return room @ r.T + shift, cams @ r.T + shift


def test_it_stands_the_room_back_up(knocked_over):
    points, cams = knocked_over
    m = scene_manifest.build(points, cams)
    canonical = _apply(m["canonicalTransform"], points)
    # Floor and ceiling are horizontal again: their spread in y is tiny
    # compared with their spread in x/z.
    y = canonical[:, 1]
    lo, hi = np.quantile(y, 0.02), np.quantile(y, 0.98)
    assert hi - lo == pytest.approx(2.5, abs=0.15)
    assert m["worldUp"] == [0.0, 1.0, 0.0]


def test_the_floor_sits_at_zero_and_the_room_over_the_origin(knocked_over):
    points, cams = knocked_over
    m = scene_manifest.build(points, cams)
    d = m["denseBounds"]
    assert d["min"][1] == pytest.approx(0.0, abs=1e-6)
    assert m["floorHeight"] == 0.0
    # Centred in the horizontal plane.
    assert (d["min"][0] + d["max"][0]) / 2 == pytest.approx(0.0, abs=1e-6)
    assert (d["min"][2] + d["max"][2]) / 2 == pytest.approx(0.0, abs=1e-6)


def test_the_transform_is_rigid(knocked_over):
    points, cams = knocked_over
    m = np.asarray(scene_manifest.build(points, cams)["canonicalTransform"]).reshape(4, 4)
    r = m[:3, :3]
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-6)
    assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-6)  # no reflection
    assert list(m[3]) == [0.0, 0.0, 0.0, 1.0]


def test_the_opening_camera_is_a_real_one_standing_in_the_room(knocked_over):
    points, cams = knocked_over
    m = scene_manifest.build(points, cams)
    entry = m["entryCamera"]
    assert entry is not None
    idx = entry["index"]
    assert 0 <= idx < len(cams)
    # It is the chosen registered position, carried into the canonical frame.
    expected = _apply(m["canonicalTransform"], cams[idx:idx + 1])[0]
    assert np.allclose(entry["position"], expected, atol=1e-5)
    d = m["denseBounds"]
    for axis in range(3):
        assert d["min"][axis] - 0.2 <= entry["position"][axis] <= d["max"][axis] + 0.2
    # Standing height, not on the floor, not in the ceiling.
    assert 0.5 < entry["position"][1] < 2.0
    assert entry["fov"] == 65
    # It says the direction was derived, because poses carry no orientation yet.
    assert m["entryCameraSource"] == "registered_position_derived_direction"


def test_it_never_picks_the_first_frame_just_because_it_is_first(knocked_over):
    points, cams = knocked_over
    idx = scene_manifest.build(points, cams)["entryCamera"]["index"]
    # Camera 0 is at the sequence's edge; the scorer prefers the middle.
    assert idx != 0


def test_without_cameras_there_is_no_entry_camera_and_it_says_so():
    m = scene_manifest.build(_room(), None)
    assert m["entryCamera"] is None
    assert m["entryCameraSource"] == "none"
    # The frame is still canonical; the viewer will frame the bounds.
    assert m["worldUp"] == [0.0, 1.0, 0.0]


def test_it_records_where_up_came_from_and_how_sure_it_is(knocked_over):
    points, cams = knocked_over
    m = scene_manifest.build(points, cams)
    assert m["worldUpSource"] in ("floor_plane", "assumed")
    assert 0.0 <= m["worldUpConfidence"] <= 1.0
    assert isinstance(m["worldUpDetail"], str) and m["worldUpDetail"]
    assert m["units"] == "reconstruction"  # NOT metres; nobody may measure with this


def test_write_and_read_round_trip(tmp_path, knocked_over):
    points, cams = knocked_over
    m = scene_manifest.build(points, cams)
    art = tmp_path / "capture.sog"; art.write_bytes(b"PK\x03\x04")
    path = scene_manifest.write(art, m)
    assert path == tmp_path / "capture.sog.scene.json"
    assert scene_manifest.read(art) == json.loads(path.read_text())


def test_a_future_schema_is_ignored_rather_than_misread(tmp_path):
    art = tmp_path / "capture.sog"; art.write_bytes(b"PK\x03\x04")
    scene_manifest.manifest_for(art).write_text(json.dumps({"version": 99}))
    assert scene_manifest.read(art) is None


def test_garbage_points_are_refused():
    with pytest.raises(ValueError):
        scene_manifest.build(np.full((10, 3), np.nan), None)
