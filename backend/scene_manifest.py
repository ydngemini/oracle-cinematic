"""scene.json — the canonical frame a reconstruction is read in.

Structure-from-motion has no idea where gravity is, so a finished capture
arrives in an arbitrary coordinate frame: "up" is whatever direction the solver
happened to choose, and the viewer that opens it is as likely to start inside a
wall as in the middle of the room. That is not a viewer problem. Every
consumer — the tour, the floor plan, room segmentation, anything structural
later — needs the SAME answer to "which way is up", and if each derives its own
they will disagree and the disagreement will be silent.

So the answer is computed once, here, and written beside the artifact as
`scene.json`. The viewer applies `canonicalTransform` to the scene root rather
than the bytes being rewritten: re-encoding a .sog to bake in a rotation would
cost a GPU round trip and lose the ability to revise the estimate later. What
matters is that there is one definition, on disk, that everybody reads.

WHAT THIS DOES NOT DO YET, stated plainly because a silent limit is worse than
a missing feature:

* `estimate_up_axis` chooses among the three coordinate axes. It corrects a
  frame that is 90 degrees out — the common and catastrophic case — but it
  cannot correct a few degrees of tilt, because it never proposes an
  off-axis vector. A capture solved at a slight angle stays at that angle.
* Nothing here knows gravity from the device. Phone IMU / ARKit / ARCore would
  be a better source than floor-plane mass, and `worldUpSource` says which was
  used so a future capture carrying real gravity can be told apart.
* `units` is `"reconstruction"`. Scale is unresolved; these are not metres, and
  nothing downstream may present them as measurements.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional, Sequence

import capture_sidecars

log = logging.getLogger("oracle.scene_manifest")

SCHEMA_VERSION = 1
SUFFIX = ".scene.json"

#: Trim per axis when describing where the capture actually is. Every real
#: reconstruction has strays — background through a window, floaters behind the
#: camera — and letting them set the extent makes the room a speck.
DENSE_TRIM = 0.02
#: Enough samples for stable percentiles without sorting millions of floats.
SAMPLE_TARGET = 120_000
#: A camera nearer than this fraction of the room's size is against a surface.
MIN_CLEARANCE_FRACTION = 0.04


def manifest_for(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + SUFFIX)


def build(
    xyz,
    camera_positions: Optional[Sequence[Sequence[float]]] = None,
    *,
    primitive_count: Optional[int] = None,
) -> dict[str, Any]:
    """Compute the canonical frame and a deterministic opening viewpoint."""
    import numpy as np

    points = np.asarray(xyz, dtype=float)
    points = points[np.isfinite(points).all(axis=1)]
    if points.size == 0:
        raise ValueError("no finite points to canonicalise")

    cameras = _clean_cameras(np, camera_positions)
    up, up_source, up_confidence, up_detail = _world_up(points, cameras)

    rotation = _rotation_taking(np, up, (0.0, 1.0, 0.0))
    rotated = points @ rotation.T
    dense_min, dense_max = _dense_extent(np, rotated)
    # Put the floor at y=0 and the capture over the origin, so "height" means
    # height above the floor for everyone who reads this.
    floor_y = float(dense_min[1])
    centre = (dense_min + dense_max) / 2.0
    translation = np.array([-centre[0], -floor_y, -centre[2]], dtype=float)

    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation

    raw_min = points.min(axis=0)
    raw_max = points.max(axis=0)

    manifest: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "units": "reconstruction",
        "primitiveCount": int(primitive_count) if primitive_count else int(len(points)),
        "worldUp": [0.0, 1.0, 0.0],
        "worldUpSource": up_source,
        "worldUpConfidence": round(float(up_confidence), 4),
        "worldUpDetail": up_detail,
        "worldUpInSourceFrame": [round(float(v), 6) for v in up],
        # Row-major 4x4, canonical = M * source.
        "canonicalTransform": [round(float(v), 6) for v in transform.flatten()],
        "bounds": _box(np, (rotation @ raw_min) + translation, (rotation @ raw_max) + translation),
        "denseBounds": _box(np, dense_min + translation, dense_max + translation),
        "floorHeight": 0.0,
        "entryCamera": None,
        "entryCameraSource": "none",
    }
    entry = _entry_camera(np, cameras, rotation, translation, dense_min + translation,
                          dense_max + translation)
    if entry:
        manifest["entryCamera"] = entry["camera"]
        manifest["entryCameraSource"] = entry["source"]
    return manifest


def write(artifact: Path, manifest: dict[str, Any]) -> Optional[Path]:
    """Write the manifest beside `artifact`. Never raises.

    A reconstruction that succeeded must not be failed by trouble writing a
    sidecar to it — the same rule the camera poses follow.
    """
    try:
        path = manifest_for(artifact)
        path.write_text(json.dumps(manifest, indent=2))
        return path
    except Exception:  # noqa: BLE001
        log.exception("Could not write a scene manifest beside %s", artifact)
        return None


def read(artifact: Path) -> Optional[dict[str, Any]]:
    """The manifest beside `artifact`, or None when absent or unreadable."""
    try:
        path = manifest_for(artifact)
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version", 0)) != SCHEMA_VERSION:
            log.warning(
                "Ignoring scene manifest beside %s: version %s, expected %s",
                artifact.name, payload.get("version"), SCHEMA_VERSION,
            )
            return None
        return payload
    except Exception:  # noqa: BLE001
        log.exception("Could not read the scene manifest beside %s", artifact)
        return None


def build_for_artifact(artifact: Path, points_ply: Path) -> Optional[dict[str, Any]]:
    """Convenience: read the geometry and poses already stored beside a splat."""
    from floorplan_pipeline.slicing import parse_ply

    try:
        # parse_ply returns (positions, opacity); only the positions matter for
        # a coordinate frame.
        xyz, _opacity = parse_ply(points_ply.read_bytes())
    except Exception:  # noqa: BLE001
        log.exception("Could not read %s for canonicalisation", points_ply)
        return None
    cameras = capture_sidecars.read(artifact)
    try:
        return build(xyz, cameras)
    except Exception:  # noqa: BLE001
        log.exception("Could not canonicalise %s", artifact.name)
        return None


# ---------------------------------------------------------------------------


def _clean_cameras(np, camera_positions):
    # `not array` is ambiguous for numpy; ask the question directly.
    if camera_positions is None or len(camera_positions) == 0:
        return None
    try:
        arr = np.asarray(camera_positions, dtype=float)
    except Exception:  # noqa: BLE001
        return None
    if arr.ndim != 2 or arr.shape[1] < 3:
        return None
    arr = arr[:, :3]
    arr = arr[np.isfinite(arr).all(axis=1)]
    return arr if len(arr) >= capture_sidecars.MIN_USABLE_POSES else None


def _world_up(points, cameras):
    """Up, by the best source available. Order matters — see the module note."""
    # 1. Device gravity would go here. No capture carries it yet; when one does,
    #    it belongs above the geometric estimate, not below it.
    # 2. Floor/ceiling mass, with camera positions resolving the sign.
    from floorplan_pipeline.slicing import estimate_up_axis

    try:
        axis = estimate_up_axis(points, camera_positions=cameras)
        return tuple(axis.vector), "floor_plane", axis.confidence, axis.detail
    except Exception as exc:  # noqa: BLE001
        log.warning("Up-axis estimate failed (%s); falling back to +Y", exc)
        # 4. Last resort. Named so a reader can tell a guess from a measurement.
        return (0.0, 1.0, 0.0), "assumed", 0.0, "no estimate could be made"


def _rotation_taking(np, source_up, target_up):
    """Rotation matrix taking `source_up` onto `target_up`."""
    a = np.asarray(source_up, dtype=float)
    b = np.asarray(target_up, dtype=float)
    a = a / (np.linalg.norm(a) or 1.0)
    b = b / (np.linalg.norm(b) or 1.0)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-9:
        # Parallel, or antiparallel: a half turn about any perpendicular axis.
        if c > 0:
            return np.eye(3)
        perp = np.array([1.0, 0.0, 0.0])
        if abs(a[0]) > 0.9:
            perp = np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, perp)
        axis = axis / (np.linalg.norm(axis) or 1.0)
        x, y, z = axis
        return np.array([
            [2 * x * x - 1, 2 * x * y, 2 * x * z],
            [2 * x * y, 2 * y * y - 1, 2 * y * z],
            [2 * x * z, 2 * y * z, 2 * z * z - 1],
        ])
    skew = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + skew + skew @ skew * (1.0 / (1.0 + c))


def _dense_extent(np, points):
    step = max(1, len(points) // SAMPLE_TARGET)
    sample = points[::step]
    lo = np.quantile(sample, DENSE_TRIM, axis=0)
    hi = np.quantile(sample, 1.0 - DENSE_TRIM, axis=0)
    return lo, hi


def _box(np, lo, hi):
    return {
        "min": [round(float(v), 6) for v in lo],
        "max": [round(float(v), 6) for v in hi],
    }


def _entry_camera(np, cameras, rotation, translation, dense_min, dense_max):
    """Pick a registered viewpoint to open at, and say why it was chosen.

    Not camera 0: the first frame of a walkthrough is usually the doorway the
    photographer backed into, or a wall. Candidates are scored for standing
    inside the captured space, having room in front of them, and sitting near
    the middle of the sequence — the part of a capture that is usually its
    best-covered.

    Only positions are available today: the pod exports camera centres and
    discards the rotation, so the direction here is derived (look at the middle
    of the space from where someone stood) rather than the direction they
    actually pointed. `entryCameraSource` records that, and the day poses carry
    orientation this becomes the real forward vector.
    """
    if cameras is None or len(cameras) == 0:
        return None

    canonical = (cameras @ rotation.T) + translation
    size = dense_max - dense_min
    extent = float(max(size)) or 1.0
    centre = (dense_min + dense_max) / 2.0
    clearance = extent * MIN_CLEARANCE_FRACTION

    best = None
    n = len(canonical)
    for index, position in enumerate(canonical):
        inside = bool(np.all(position >= dense_min - clearance)
                      and np.all(position <= dense_max + clearance))
        if not inside:
            continue
        to_centre = centre - position
        distance = float(np.linalg.norm(to_centre))
        if distance < clearance:
            continue  # standing on the middle of the room, nothing to look at
        # Nearer the middle of the sequence is better-covered than either end.
        sequence = 1.0 - abs((index / max(1, n - 1)) - 0.5) * 2.0
        # Something in front, but not pressed against the far wall either.
        room_ahead = min(1.0, distance / (extent * 0.5 or 1.0))
        score = room_ahead * 0.6 + sequence * 0.4
        if best is None or score > best[0]:
            best = (score, index, position, centre)

    if best is None:
        return None
    _, index, position, target = best
    return {
        "source": "registered_position_derived_direction",
        "camera": {
            "index": int(index),
            "position": [round(float(v), 6) for v in position],
            "target": [round(float(v), 6) for v in target],
            "fov": 65,
        },
    }
