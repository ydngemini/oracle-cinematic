"""Camera poses, from the reconstruction that computed them to the plan that needs them.

`estimate_up_axis` settles a near-cubic bathroom or a stairwell only when it is
told where the photographer stood — a rectangular box maps any face-pair onto
any other, so no scale-free measure separates them. COLMAP computes exactly
those positions on the way to the splat, and the job used to discard them, which
left that parameter with no way to be supplied in production.

The property defended here is the FRAME. Positions in the wrong frame do not
raise; they return a confident up axis pointing somewhere else.
"""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

import capture_sidecars


def _positions(n=8):
    return [[float(i), float(i) * 0.5, 1.55] for i in range(n)]


def test_a_round_trip_returns_what_was_written(tmp_path):
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"not really a splat")

    written = capture_sidecars.write(artifact, _positions())
    assert written is not None and written.is_file()
    assert written.name == "model.sog.cameras.json", (
        "the sidecar keeps the artifact's whole name, so two artifacts differing "
        "only by extension cannot collide on one file"
    )
    assert capture_sidecars.read(artifact) == _positions()


def test_poses_in_the_wrong_frame_are_refused(tmp_path):
    """gsplat's Parser recentres and rescales the scene before training, so
    COLMAP's own centres are not interchangeable with the delivered model. Using
    them would look like it worked."""
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"x")
    capture_sidecars.write(artifact, _positions(), frame=capture_sidecars.FRAME_COLMAP)

    assert capture_sidecars.read(artifact, frame=capture_sidecars.FRAME_TRAINED) is None
    assert capture_sidecars.read(artifact, frame=capture_sidecars.FRAME_COLMAP) == _positions()


def test_a_future_schema_is_ignored_rather_than_guessed(tmp_path):
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"x")
    capture_sidecars.sidecar_for(artifact).write_text(json.dumps({
        "version": capture_sidecars.SCHEMA_VERSION + 1,
        "frame": capture_sidecars.FRAME_TRAINED,
        "positions": _positions(),
    }))

    assert capture_sidecars.read(artifact) is None


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b'{"positions": "x"}'])
def test_an_unreadable_sidecar_is_not_an_incident(tmp_path, payload):
    """Every caller already handles having no camera positions — that was the
    only case until now — so a damaged file degrades to that rather than taking
    the plan down with it."""
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"x")
    capture_sidecars.sidecar_for(artifact).write_bytes(payload)

    assert capture_sidecars.read(artifact) is None


def test_too_few_poses_are_not_recorded(tmp_path):
    """Below what estimate_up_axis will use for the axis, a sidecar is a
    promise the file cannot keep."""
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"x")

    assert capture_sidecars.write(artifact, _positions(2)) is None
    assert not capture_sidecars.sidecar_for(artifact).exists()


def test_a_missing_sidecar_reads_as_none(tmp_path):
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"x")
    assert capture_sidecars.read(artifact) is None


# ---------------------------------------------------------------------------
# The consumer end
# ---------------------------------------------------------------------------

def test_the_plan_path_picks_up_recorded_poses(tmp_path, monkeypatch):
    """The bytes-in entry point never sees a path and so can never find a
    sidecar. That is why `camera_positions` was a parameter no caller passed."""
    pytest.importorskip("cv2")
    from floorplan_pipeline import slicing

    artifact = tmp_path / "model.ply"
    artifact.write_bytes(b"stand-in")
    capture_sidecars.write(artifact, _positions())

    seen = {}

    def _fake(ply_bytes, **kwargs):
        seen.update(kwargs)
        return "document"

    monkeypatch.setattr(slicing, "extract_from_reconstruction", _fake)
    assert slicing.extract_from_reconstruction_file(artifact) == "document"
    assert seen["camera_positions"] == _positions()


def test_an_explicit_argument_beats_the_sidecar(tmp_path, monkeypatch):
    """A caller who measured the positions means it."""
    pytest.importorskip("cv2")
    from floorplan_pipeline import slicing

    artifact = tmp_path / "model.ply"
    artifact.write_bytes(b"stand-in")
    capture_sidecars.write(artifact, _positions())
    measured = [[9.0, 9.0, 9.0]] * 5

    seen = {}
    monkeypatch.setattr(slicing, "extract_from_reconstruction",
                        lambda ply_bytes, **kw: seen.update(kw) or "document")
    slicing.extract_from_reconstruction_file(artifact, camera_positions=measured)

    assert seen["camera_positions"] == measured


def test_recorded_poses_actually_change_the_answer(tmp_path):
    """The point of the whole chain: a near-cubic room is undecidable from the
    cloud, and the recorded poses decide it."""
    pytest.importorskip("cv2")
    from floorplan_pipeline import slicing

    rng = np.random.default_rng(3)

    def face(origin, u, v, density=320.0):
        area = float(np.linalg.norm(np.cross(u, v)))
        n = max(200, int(area * density))
        a, b = rng.random((n, 1)), rng.random((n, 1))
        return (np.asarray(origin, float) + a * np.asarray(u, float)
                + b * np.asarray(v, float) + rng.normal(0, 0.004, (n, 3)))

    w, d, h = 2.0, 2.2, 2.4          # a bathroom: no distinguishing extent
    xyz = np.vstack([
        face([0, 0, 0], [w, 0, 0], [0, d, 0]), face([0, 0, h], [w, 0, 0], [0, d, 0]),
        face([0, 0, 0], [w, 0, 0], [0, 0, h]), face([0, d, 0], [w, 0, 0], [0, 0, h]),
        face([0, 0, 0], [0, d, 0], [0, 0, h]), face([w, 0, 0], [0, d, 0], [0, 0, h]),
    ])
    cameras = np.stack([
        rng.uniform(0.35, w - 0.35, 14),
        rng.uniform(0.35, d - 0.35, 14),
        1.55 + rng.normal(0, 0.06, 14),
    ], axis=1)

    blind = slicing.estimate_up_axis(xyz, metres_per_unit=1.0)
    artifact = tmp_path / "model.ply"
    artifact.write_bytes(b"stand-in")
    capture_sidecars.write(artifact, cameras.tolist())
    recorded = capture_sidecars.read(artifact)
    assert recorded is not None, "the sidecar is the whole point of this test"

    sighted = slicing.estimate_up_axis(
        xyz, metres_per_unit=1.0, camera_positions=recorded
    )

    truth = np.array([0.0, 0.0, 1.0])
    assert abs(float(np.dot(sighted.vector, truth))) > 0.99
    assert sighted.confidence > blind.confidence


# ---------------------------------------------------------------------------
# The worker: poses have to survive conversion and storage
# ---------------------------------------------------------------------------

def test_poses_follow_the_file_that_gets_delivered(tmp_path):
    """The sidecar is written next to the provider's RAW output, and conversion
    produces a differently-named file. Without carrying it across, the poses are
    left behind in a temp directory that is deleted at the end of the job — the
    same way COLMAP's poses were lost before, one step further along."""
    import reconstruction_worker

    raw = tmp_path / "model.ply"
    raw.write_bytes(b"raw")
    delivered = tmp_path / "abc123.sog"
    delivered.write_bytes(b"converted")
    capture_sidecars.write(raw, _positions())

    reconstruction_worker._carry_companions(raw, delivered)

    assert capture_sidecars.read(delivered) == _positions()


def test_carrying_poses_is_a_no_op_when_there_are_none(tmp_path):
    import reconstruction_worker

    raw = tmp_path / "model.ply"
    raw.write_bytes(b"raw")
    delivered = tmp_path / "abc123.sog"
    delivered.write_bytes(b"converted")

    reconstruction_worker._carry_companions(raw, delivered)      # must not raise

    assert capture_sidecars.read(delivered) is None


def test_a_passthrough_artifact_keeps_its_own_sidecar(tmp_path):
    """The pod already returns .sog, so conversion returns the same path. The
    sidecar is already in the right place and must not be disturbed."""
    import reconstruction_worker

    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"sog")
    capture_sidecars.write(artifact, _positions())

    reconstruction_worker._carry_companions(artifact, artifact)

    assert capture_sidecars.read(artifact) == _positions()



# ---------------------------------------------------------------------------
# The geometry, which delivery format cannot provide
# ---------------------------------------------------------------------------

def test_a_ply_artifact_is_its_own_geometry(tmp_path):
    artifact = tmp_path / "model.ply"
    artifact.write_bytes(b"ply\n")
    assert capture_sidecars.geometry_for(artifact) == artifact


def test_a_sog_needs_the_points_cloud_beside_it(tmp_path):
    """Delivery is .sog because the viewer renders it and it is an order of
    magnitude smaller. `parse_ply` cannot read a byte of it, so without the
    points cloud the plan path has geometry it cannot open."""
    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"compressed")

    assert capture_sidecars.geometry_for(artifact) is None

    cloud = capture_sidecars.points_sidecar_for(artifact)
    assert cloud.name == "model.sog.points.ply"
    cloud.write_bytes(b"ply\n")
    assert capture_sidecars.geometry_for(artifact) == cloud


def test_the_plan_path_refuses_a_sog_it_cannot_measure(tmp_path):
    pytest.importorskip("cv2")
    from floorplan_pipeline import slicing
    from floorplan_pipeline.errors import UnsupportedInput

    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"compressed")

    with pytest.raises(UnsupportedInput) as caught:
        slicing.extract_from_reconstruction_file(artifact)

    assert "viewer" in str(caught.value), "the reason has to name the format mismatch"


def test_the_plan_path_reads_the_points_cloud_beside_a_sog(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    from floorplan_pipeline import slicing

    artifact = tmp_path / "model.sog"
    artifact.write_bytes(b"compressed")
    capture_sidecars.points_sidecar_for(artifact).write_bytes(b"PLY-BYTES")
    capture_sidecars.write(artifact, _positions())

    seen = {}
    monkeypatch.setattr(slicing, "extract_from_reconstruction",
                        lambda ply_bytes, **kw: seen.update(kw, body=ply_bytes) or "doc")
    slicing.extract_from_reconstruction_file(artifact)

    assert seen["body"] == b"PLY-BYTES", "it must measure the cloud, not the .sog"
    assert seen["camera_positions"] == _positions()


def test_both_companions_follow_the_delivered_file(tmp_path):
    import reconstruction_worker

    raw = tmp_path / "model.ply"
    raw.write_bytes(b"raw")
    delivered = tmp_path / "abc123.sog"
    delivered.write_bytes(b"converted")
    capture_sidecars.write(raw, _positions())
    capture_sidecars.points_sidecar_for(raw).write_bytes(b"cloud")

    reconstruction_worker._carry_companions(raw, delivered)

    assert capture_sidecars.read(delivered) == _positions()
    assert capture_sidecars.points_sidecar_for(delivered).read_bytes() == b"cloud"
