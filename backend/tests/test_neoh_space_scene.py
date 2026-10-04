"""Neoh Space scene metadata v2: tilt, scale honesty, navigation, versioning.

Builds synthetic rooms (no GPU, no fixture files) and checks what the
manifest is allowed to claim about them.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import scene_manifest
from tests.test_scene_manifest import _apply, _cameras, _room, _rot


def _floor_tilt_degrees(manifest, points):
    canonical = _apply(manifest["canonicalTransform"], points)
    y = canonical[:, 1]
    floor = canonical[y < np.quantile(y, 0.2)]
    floor = floor[np.abs(floor[:, 1] - np.quantile(floor[:, 1], 0.5)) < 0.3]
    centred = floor - floor.mean(axis=0)
    normal = np.linalg.svd(centred, full_matrices=False)[2][2]
    return math.degrees(math.acos(min(1.0, abs(float(normal[1])))))


def test_the_built_space_ends_up_level_after_a_small_tilt():
    room, cams = _room(), _cameras()
    tilt = _rot([1, 0, 0], 7.0)
    points, cameras = room @ tilt.T, cams @ tilt.T
    m = scene_manifest.build(points, cameras)
    assert _floor_tilt_degrees(m, points) < 0.6
    assert "tiltCorrection" in m and "reason" in m["tiltCorrection"]


def test_fine_tilt_corrects_a_residual_within_its_bound():
    # Called on an already-canonical-ish frame with a residual the axis search
    # left behind, which is exactly the input it gets in `build`.
    points = _room() @ _rot([1, 0, 0], 3.0).T
    out = scene_manifest._fine_tilt(np, points, None)
    assert out["applied"] is True and out["reason"] == "corrected"
    assert out["degrees"] == pytest.approx(3.0, abs=0.4)
    fixed = points @ out["rotation"].T
    assert _floor_tilt_degrees({"canonicalTransform": np.eye(4).flatten()}, fixed) < 0.5


def test_fine_tilt_leaves_a_large_tilt_alone_and_says_why():
    # 12 degrees is a sloped floor or a bad fit; straightening the whole space
    # would make every wall lean.
    out = scene_manifest._fine_tilt(np, _room() @ _rot([1, 0, 0], 12.0).T, None)
    assert out["applied"] is False and out["reason"] == "exceeds_bound"


def test_fine_tilt_ignores_noise_on_a_level_room():
    out = scene_manifest._fine_tilt(np, _room(), None)
    assert out["applied"] is False and out["reason"] == "already_level"


def test_fine_tilt_is_vetoed_when_the_camera_path_disagrees():
    points = _room() @ _rot([1, 0, 0], 3.0).T
    cams = _cameras() @ _rot([0, 0, 1], 4.0).T  # a walk tilted the other way
    out = scene_manifest._fine_tilt(np, points, cams)
    assert out["applied"] is False and out["reason"] == "camera_path_disagrees"


def test_fine_tilt_needs_a_dense_floor():
    room = _room(n_floor=50)
    room = room[room[:, 1] > 0.2]  # no floor band at all
    out = scene_manifest._fine_tilt(np, room, None)
    assert out["applied"] is False


def test_without_a_measured_source_scale_is_never_metric():
    m = scene_manifest.build(_room(), _cameras(h=1.5))
    scale = m["scale"]
    assert scale["status"] == "estimated"
    assert scale["source"] == "capture_height_prior"
    assert scale["measurementsAllowed"] is False
    assert scale["metresPerUnit"] == pytest.approx(1.45 / 1.5, rel=0.05)
    assert "scale_unknown" in m["limitations"]
    # Navigation uses the capture's own height, in scene units.
    assert m["navigation"]["eyeHeight"] == pytest.approx(1.5, abs=0.05)


def test_no_cameras_means_unknown_scale_and_a_stated_limitation():
    m = scene_manifest.build(_room(), None)
    assert m["scale"]["status"] == "unknown"
    assert m["scale"]["metresPerUnit"] is None
    assert "camera_poses_missing" in m["limitations"]
    assert "entry_view_default" in m["limitations"]
    assert m["navigation"]["eyeHeight"] is None


def test_only_a_measured_source_makes_scale_metric():
    m = scene_manifest.build(_room(), _cameras(),
                             measured_scale={"metresPerUnit": 0.5, "source": "device_lidar"})
    assert m["scale"]["status"] == "metric"
    assert m["scale"]["measurementsAllowed"] is True
    assert "scale_unknown" not in m["limitations"]


def test_manifest_carries_identity_version_and_provenance():
    m = scene_manifest.build(_room(), _cameras(), asset_id="media-1",
                             pipeline_version="space-test", provenance={"provider": "x"})
    assert m["version"] == scene_manifest.SCHEMA_VERSION == 2
    assert m["assetId"] == "media-1"
    assert m["pipelineVersion"] == "space-test"
    assert m["generatedAt"]
    assert m["provenance"] == {"provider": "x"}
    assert m["rooms"] is None  # never guessed from a point cloud
    target = m["recommendedTarget"]
    lo, hi = m["denseBounds"]["min"], m["denseBounds"]["max"]
    assert all(lo[i] - 1e-6 <= target[i] <= hi[i] + 1e-6 for i in range(3))


def test_v1_manifests_are_read_as_legacy_not_reinterpreted():
    v1 = {"version": 1, "units": "reconstruction", "denseBounds": {"min": [0, 0, 0], "max": [1, 1, 1]}}
    out = scene_manifest.normalise(v1)
    assert out["legacy"] is True and out["sourceVersion"] == 1
    assert out["scale"]["status"] == "unknown"
    assert out["scale"]["measurementsAllowed"] is False
    assert scene_manifest.normalise({"version": 99}) is None
    assert scene_manifest.normalise("nope") is None


def test_curated_override_sets_entry_and_calibration_without_rewriting():
    m = scene_manifest.build(_room(), None)
    over = scene_manifest.apply_override(m, {
        "entryCamera": {"position": [0, 1.4, 0], "target": [1, 1.4, 0]},
        "scaleCalibration": {"metresPerUnit": 0.98, "basis": "door width 0.9 m"},
    })
    assert over["entryCameraSource"] == "curated"
    assert over["scale"]["status"] == "metric"
    assert over["scale"]["source"] == "operator_calibration"
    assert set(over["overrides"]) == {"entryCamera", "scaleCalibration"}
    # The computed manifest is untouched.
    assert m["scale"]["status"] == "unknown"
    # A malformed override changes nothing.
    assert scene_manifest.apply_override(m, {"entryCamera": {"position": "x"}}) == m


def test_full_bounds_are_a_valid_box_in_the_canonical_frame():
    # Regression from the first real v2 capture: rotating the source frame's
    # min/max corners gave bounds with min.y > max.y.
    from tests.test_scene_manifest import _apply

    room = _room() @ _rot([1, 0, 0], 90).T @ _rot([0, 1, 0], 33).T
    m = scene_manifest.build(room, None)
    lo, hi = m["bounds"]["min"], m["bounds"]["max"]
    assert all(lo[i] <= hi[i] for i in range(3))
    canonical = _apply(m["canonicalTransform"], room)
    assert np.allclose(canonical.min(axis=0), lo, atol=1e-4)
    assert np.allclose(canonical.max(axis=0), hi, atol=1e-4)
    dlo, dhi = m["denseBounds"]["min"], m["denseBounds"]["max"]
    assert all(lo[i] - 1e-6 <= dlo[i] <= dhi[i] <= hi[i] + 1e-6 for i in range(3))


def test_stray_geometry_is_reported_and_does_not_set_the_framing():
    room = _room()
    clean = scene_manifest.build(room, _cameras())
    assert "floaters_present" not in clean["limitations"]
    floaters = np.array([[400.0, 3.0, 0.0], [-60.0, 1.0, 80.0]] * 20)
    m = scene_manifest.build(np.concatenate([room, floaters]), _cameras())
    assert "floaters_present" in m["limitations"]
    # The viewer frames the dense box, which still describes the room.
    size = np.subtract(m["denseBounds"]["max"], m["denseBounds"]["min"])
    assert size.max() < 6.0


def _cluttered(room):
    """Furniture-height boxes over the floor: the band is no longer a plane."""
    rng = np.random.default_rng(3)
    boxes = np.stack([rng.uniform(-2, 2, 6000), rng.uniform(0.0, 0.25, 6000),
                      rng.uniform(-1.5, 1.5, 6000)], axis=1)
    return np.concatenate([room, boxes])


def test_a_cluttered_floor_falls_back_to_the_capture_walk():
    # The real capture's case (2026-10-03): the floor band was not planar, the
    # axis estimate left the space ~16 degrees off, and the level walk
    # measured it.
    room = _cluttered(_room())
    cams = _cameras(n=40) + np.random.default_rng(5).normal(0, 0.02, (40, 3))
    tilt = _rot([1, 0, 0], 12.0)
    out = scene_manifest._fine_tilt(np, room @ tilt.T, cams @ tilt.T)
    assert out["planarity"] > scene_manifest.FLOOR_PLANARITY_MAX
    assert out["applied"] is True and out["reason"] == "corrected_from_camera_path"
    assert out["source"] == "camera_path_plane"
    assert out["degrees"] == pytest.approx(12.0, abs=1.0)


def test_the_walk_correction_has_its_own_bound():
    room = _cluttered(_room())
    tilt = _rot([1, 0, 0], 30.0)
    out = scene_manifest._fine_tilt(np, room @ tilt.T, _cameras(n=40) @ tilt.T)
    assert out["applied"] is False
    assert out["reason"] == "camera_tilt_exceeds_bound"


def test_a_walk_that_is_not_a_plane_is_not_trusted():
    room = _cluttered(_room())
    t = np.linspace(0, 1, 40)
    stairs = np.stack([np.cos(t * 6), t * 2.5, np.sin(t * 6)], axis=1)  # climbing
    out = scene_manifest._fine_tilt(np, room @ _rot([1, 0, 0], 8.0).T, stairs)
    assert out["applied"] is False
