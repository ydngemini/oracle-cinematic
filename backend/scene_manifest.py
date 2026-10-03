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

* `estimate_up_axis` searches principal and refined directions from the
  cloud's mass (and the camera path), which fixes the catastrophic case — a
  frame 90 degrees out — and most of a small tilt. What it leaves (clutter
  on the floor, wall feet in the floor band) is refined by `_fine_tilt`: a
  plane fit to the floor band, applied only when it is planar, between
  MIN_ and MAX_FINE_TILT_DEG, and not contradicted by the camera path.
  Anything larger is left alone and recorded — a sloped floor is
  architecture, not an error to straighten.
* Nothing here knows gravity from the device. Phone IMU / ARKit / ARCore would
  be a better source than floor-plane mass, and `worldUpSource` says which was
  used so a future capture carrying real gravity can be told apart.
* `units` is `"reconstruction"`. These are not metres. `scale` says what is
  known: `unknown`, `estimated` (from the capture-height prior, never good
  enough to measure with), or `metric` (only from a measured source — device
  AR/LiDAR metadata or an operator calibration). `scale.measurementsAllowed`
  is True only for `metric`, and every dimension tool must respect it.

Versioned. v2 added scale, tilt, navigation, provenance and pipeline version.
v1 manifests are still read, explicitly marked `legacy`, with scale reported
as `unknown` — never silently reinterpreted.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional, Sequence

import capture_sidecars

log = logging.getLogger("oracle.scene_manifest")

SCHEMA_VERSION = 2
#: Versions `read()` and the resolver accept. v1 is upgraded explicitly (see
#: `normalise`), never treated as if it carried v2's fields.
SUPPORTED_VERSIONS = (1, 2)
SUFFIX = ".scene.json"

#: Largest residual tilt the floor fit may correct. Beyond this the floor is
#: either genuinely sloped or the fit is wrong, and either way rotating the
#: whole space would make the walls lean.
MAX_FINE_TILT_DEG = 5.0
#: Below this the space is level enough; correcting would only chase the
#: noise of wall feet and clutter in the floor band (measured ~0.2-0.3 deg on
#: a clean synthetic room).
MIN_FINE_TILT_DEG = 0.5
#: Camera-path and floor-fit tilts must agree within this when both exist.
TILT_AGREEMENT_DEG = 3.0
#: The capture walk may correct more than the floor fit: it measures gravity
#: directly and is blind to sloped architecture. Beyond this the walk itself
#: is suspect (stairs, a capture held at wildly varying heights).
MAX_CAMERA_TILT_DEG = 20.0
#: The walk counts as a level plane only when its thinnest direction is this
#: small relative to its middle one (measured 0.26 on a real handheld capture).
CAMERA_PLANE_MAX_PLANARITY = 0.3
CAMERA_PLANE_MIN_POSES = 20
#: Full extent beyond this multiple of the dense extent (any axis) means the
#: space carries visible stray geometry, and the result says so.
FLOATER_EXTENT_RATIO = 2.0
#: Floor band half-thickness, as a fraction of the dense height.
FLOOR_BAND_FRACTION = 0.025
#: The floor band must be this planar (smallest / middle singular value).
FLOOR_PLANARITY_MAX = 0.12
#: A handheld capture is usually taken around this height. Used ONLY to give
#: navigation a plausible feel when nothing measured exists — never to measure.
CAPTURE_HEIGHT_PRIOR_M = 1.45
CAPTURE_HEIGHT_PRIOR_CONFIDENCE = 0.3

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
    asset_id: Optional[str] = None,
    pipeline_version: Optional[str] = None,
    provenance: Optional[dict[str, Any]] = None,
    measured_scale: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compute the canonical frame and a deterministic opening viewpoint.

    `measured_scale` is a measured metres-per-unit from a source that can
    actually know it (device AR/LiDAR metadata, operator calibration):
    ``{"metresPerUnit": float, "source": str}``. Nothing else may make the
    scale `metric`.
    """
    import numpy as np

    points = np.asarray(xyz, dtype=float)
    points = points[np.isfinite(points).all(axis=1)]
    if points.size == 0:
        raise ValueError("no finite points to canonicalise")

    cameras = _clean_cameras(np, camera_positions)
    up, up_source, up_confidence, up_detail = _world_up(points, cameras)

    rotation = _rotation_taking(np, up, (0.0, 1.0, 0.0))
    tilt = _fine_tilt(np, points @ rotation.T,
                      None if cameras is None else cameras @ rotation.T)
    fine = tilt.pop("rotation", None)
    if tilt["applied"] and fine is not None:
        rotation = fine @ rotation
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

    # The full extent IN THE CANONICAL FRAME. Rotating the source frame's two
    # corner points does not give a box — on the first real v2 capture it
    # produced min.y > max.y — so the extent is taken from the rotated points.
    raw_min = rotated.min(axis=0)
    raw_max = rotated.max(axis=0)
    dense_lo = dense_min + translation
    dense_hi = dense_max + translation

    camera_height = _camera_height(np, cameras, rotation, translation, dense_lo, dense_hi)
    scale = _scale(camera_height, measured_scale)

    limitations: list[str] = []
    if cameras is None:
        limitations.append("camera_poses_missing")
    if up_source == "assumed" or float(up_confidence) < 0.5:
        limitations.append("orientation_uncertain")
    if scale["status"] != "metric":
        limitations.append("scale_unknown")
    # Floaters: geometry far outside the captured space (through windows,
    # behind the photographer, bad matches). Measured as the full extent
    # against the dense one; the first real capture was 2.7x on one axis.
    dense_size = np.maximum(dense_hi - dense_lo, 1e-9)
    full_size = raw_max - raw_min
    stray_ratio = float(np.max(full_size / dense_size))
    if stray_ratio > FLOATER_EXTENT_RATIO:
        limitations.append("floaters_present")

    eye = camera_height["median"] if camera_height else None
    target_height = eye if eye else float(dense_hi[1] - dense_lo[1]) * 0.4
    centre_canonical = (dense_lo + dense_hi) / 2.0

    manifest: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "assetId": asset_id,
        "pipelineVersion": pipeline_version,
        "generatedAt": _now_iso(),
        "units": "reconstruction",
        "primitiveCount": int(primitive_count) if primitive_count else int(len(points)),
        "worldUp": [0.0, 1.0, 0.0],
        "worldUpSource": up_source,
        "worldUpConfidence": round(float(up_confidence), 4),
        "worldUpDetail": up_detail,
        "worldUpInSourceFrame": [round(float(v), 6) for v in up],
        "tiltCorrection": tilt,
        # Row-major 4x4, canonical = M * source.
        "canonicalTransform": [round(float(v), 6) for v in transform.flatten()],
        "bounds": _box(np, raw_min + translation, raw_max + translation),
        "denseBounds": _box(np, dense_lo, dense_hi),
        "floorHeight": 0.0,
        "scale": scale,
        "navigation": {
            # Eye height in SCENE units: the height the capture was actually
            # taken at. Correct whatever the scale, which is why the viewer
            # uses it instead of assuming 1.6 "metres".
            "eyeHeight": eye,
            "eyeHeightSource": "capture_cameras" if eye else "none",
            "walkBounds": _box(np, dense_lo, dense_hi),
        },
        "recommendedTarget": [round(float(centre_canonical[0]), 6),
                              round(float(target_height), 6),
                              round(float(centre_canonical[2]), 6)],
        # Room semantics come from the floor plan when one exists and is
        # trusted; nothing here guesses room names from a point cloud.
        "rooms": None,
        "roomGraph": None,
        "entryCamera": None,
        "entryCameraSource": "none",
        "provenance": dict(provenance or {}),
        "strayExtentRatio": round(stray_ratio, 3),
        "limitations": limitations,
    }
    entry = _entry_camera(np, cameras, rotation, translation, dense_lo, dense_hi)
    if entry:
        manifest["entryCamera"] = entry["camera"]
        manifest["entryCameraSource"] = entry["source"]
    else:
        manifest["limitations"].append("entry_view_default")
    return manifest


def normalise(payload: Any) -> Optional[dict[str, Any]]:
    """A manifest in the current shape, or None when unsupported.

    v1 files predate scale and tilt. They are upgraded *explicitly*: the
    fields v1 never measured are reported as unknown and the result is marked
    `legacy`, so a reader can tell "not measured" from "measured as none".
    """
    if not isinstance(payload, dict):
        return None
    try:
        version = int(payload.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version not in SUPPORTED_VERSIONS:
        return None
    if version == SCHEMA_VERSION:
        return payload
    upgraded = dict(payload)
    upgraded["legacy"] = True
    upgraded["sourceVersion"] = version
    upgraded.setdefault("scale", {
        "status": "unknown", "source": "not_recorded", "metresPerUnit": None,
        "confidence": 0.0, "basis": "Recorded before scale was tracked.",
        "measurementsAllowed": False,
    })
    upgraded.setdefault("tiltCorrection", {"applied": False, "reason": "not_recorded"})
    upgraded.setdefault("navigation", {"eyeHeight": None, "eyeHeightSource": "none"})
    upgraded.setdefault("limitations", ["scale_unknown"])
    return upgraded


def apply_override(manifest: dict[str, Any], override: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Merge a curated override (entry view, operator scale calibration).

    The computed manifest is never rewritten; the override is applied on read
    and recorded under `overrides` so the source of each value stays visible.
    """
    if not override or not isinstance(override, dict):
        return manifest
    out = dict(manifest)
    applied: list[str] = []
    entry = override.get("entryCamera")
    if _valid_camera(entry):
        out["entryCamera"] = {
            "position": [float(v) for v in entry["position"][:3]],
            "target": [float(v) for v in entry["target"][:3]],
            "fov": float(entry.get("fov") or 65),
        }
        out["entryCameraSource"] = "curated"
        out["limitations"] = [x for x in out.get("limitations") or [] if x != "entry_view_default"]
        applied.append("entryCamera")
    calibration = override.get("scaleCalibration")
    if isinstance(calibration, dict):
        try:
            mpu = float(calibration.get("metresPerUnit"))
        except (TypeError, ValueError):
            mpu = 0.0
        if math.isfinite(mpu) and mpu > 0:
            out["scale"] = {
                "status": "metric",
                "source": "operator_calibration",
                "metresPerUnit": mpu,
                "confidence": 0.9,
                "basis": str(calibration.get("basis")
                             or "Calibrated by an operator against a known distance."),
                "measurementsAllowed": True,
            }
            out["limitations"] = [x for x in out.get("limitations") or [] if x != "scale_unknown"]
            applied.append("scaleCalibration")
    if applied:
        out["overrides"] = applied
    return out


def _valid_camera(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        pos = [float(v) for v in entry.get("position")[:3]]
        tgt = [float(v) for v in entry.get("target")[:3]]
    except (TypeError, ValueError, AttributeError):
        return False
    return len(pos) == 3 and len(tgt) == 3 and all(math.isfinite(v) for v in pos + tgt)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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
        normalised = normalise(payload)
        if normalised is None:
            log.warning(
                "Ignoring scene manifest beside %s: unsupported version (supported %s)",
                artifact.name, SUPPORTED_VERSIONS,
            )
        return normalised
    except Exception:  # noqa: BLE001
        log.exception("Could not read the scene manifest beside %s", artifact)
        return None


def build_for_artifact(artifact: Path, points_ply: Path, **metadata: Any) -> Optional[dict[str, Any]]:
    """Convenience: read the geometry and poses already stored beside a splat.

    `metadata` (asset_id, pipeline_version, provenance, measured_scale) is
    passed straight to `build`."""
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
        return build(xyz, cameras, **metadata)
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


def _camera_plane(np, rotated_cameras):
    """(normal, planarity) of a level capture walk, or None.

    A person holding a phone walks a near-horizontal plane at roughly eye
    height, so the plane through the camera centres is perpendicular to
    gravity — independent of furniture, clutter or sloped architecture. Only
    trusted when the path really is a plane (a capture climbing stairs is not).
    """
    if rotated_cameras is None or len(rotated_cameras) < CAMERA_PLANE_MIN_POSES:
        return None
    cam = rotated_cameras - rotated_cameras.mean(axis=0)
    _, cs, cvt = np.linalg.svd(cam, full_matrices=False)
    if not cs[1] > 0:
        return None
    planarity = float(cs[2] / cs[1])
    normal = cvt[2] if cvt[2][1] >= 0 else -cvt[2]
    return normal, planarity


def _tilt_of(np, normal) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, float(normal[1])))))


def _fine_tilt(np, rotated_points, rotated_cameras) -> dict[str, Any]:
    """Bounded residual-tilt correction, from the floor plane or the walk.

    The axis estimate leaves a space level only to within whatever angle the
    solver happened to choose. Two independent references can measure the
    residual:

    1. **The floor plane** — dense and horizontal when visible. Applied when
       the floor band is genuinely planar, the tilt is between MIN and
       MAX_FINE_TILT_DEG (larger means a sloped floor or a bad fit), and the
       camera path, when it is a level plane, agrees within
       TILT_AGREEMENT_DEG. If both are good and disagree, nothing is applied.
    2. **The capture walk** — used when the floor cannot be fitted (clutter,
       rugs, furniture: the normal case in a lived-in room). A level walk's
       plane is perpendicular to gravity whatever the architecture does, so
       it is allowed a larger bound (MAX_CAMERA_TILT_DEG). Measured on the
       first real v2 capture (mip-NeRF 360 `room`, 2026-10-03): the axis
       estimate was 15.9 deg off, the floor band was not planar (0.134), and
       the walk-plane correction made every vertical vertical.

    Every refusal is recorded with its reason.
    """
    out: dict[str, Any] = {"applied": False, "degrees": 0.0, "source": "floor_plane_fit"}
    try:
        cam = _camera_plane(np, rotated_cameras)
        cam_level = cam is not None and cam[1] <= CAMERA_PLANE_MAX_PLANARITY
        if cam is not None:
            out["cameraPlanarity"] = round(cam[1], 4)
            out["cameraTiltDeg"] = round(_tilt_of(np, cam[0]), 3)

        def _from_camera(reason: str) -> dict[str, Any]:
            """Fall back to the walk plane, within its own bound."""
            if not cam_level:
                out["reason"] = reason
                return out
            degrees = _tilt_of(np, cam[0])
            if degrees < MIN_FINE_TILT_DEG:
                out.update(reason="already_level", source="camera_path_plane",
                           degrees=round(degrees, 3))
                return out
            if degrees > MAX_CAMERA_TILT_DEG:
                out.update(reason="camera_tilt_exceeds_bound", source="camera_path_plane",
                           degrees=round(degrees, 3))
                return out
            out.update(applied=True, reason="corrected_from_camera_path",
                       source="camera_path_plane", degrees=round(degrees, 3),
                       rotation=_rotation_taking(np, cam[0], (0.0, 1.0, 0.0)),
                       floorRefusal=reason)
            return out

        lo, hi = _dense_extent(np, rotated_points)
        height = float(hi[1] - lo[1])
        if not height > 0:
            return _from_camera("flat_capture")
        band = FLOOR_BAND_FRACTION * height
        y = rotated_points[:, 1]
        floor = rotated_points[(y >= lo[1] - band) & (y <= lo[1] + band)]
        if len(floor) > SAMPLE_TARGET:
            floor = floor[:: max(1, len(floor) // SAMPLE_TARGET)]
        if len(floor) < 200:
            return _from_camera("floor_too_sparse")
        centred = floor - floor.mean(axis=0)
        _, singular, vt = np.linalg.svd(centred, full_matrices=False)
        planarity = float(singular[2] / (singular[1] or 1.0))
        out["planarity"] = round(planarity, 4)
        if planarity > FLOOR_PLANARITY_MAX:
            return _from_camera("floor_not_planar")
        normal = vt[2]
        if normal[1] < 0:
            normal = -normal
        degrees = _tilt_of(np, normal)
        out["degrees"] = round(degrees, 3)
        if cam_level:
            between = math.degrees(math.acos(
                max(-1.0, min(1.0, float(np.dot(cam[0], normal))))))
            out["cameraAgreementDeg"] = round(between, 3)
            if between > TILT_AGREEMENT_DEG:
                out["reason"] = "camera_path_disagrees"
                return out
        if degrees < MIN_FINE_TILT_DEG:
            out["reason"] = "already_level"
            return out
        if degrees > MAX_FINE_TILT_DEG:
            out["reason"] = "exceeds_bound"
            return out
        out["rotation"] = _rotation_taking(np, normal, (0.0, 1.0, 0.0))
        out["applied"] = True
        out["reason"] = "corrected"
        return out
    except Exception as exc:  # noqa: BLE001 — never fail a manifest on a refinement
        log.info("Fine tilt estimate skipped (%s)", exc)
        out.pop("rotation", None)
        out["applied"] = False
        out["reason"] = "estimate_failed"
        return out


def _camera_height(np, cameras, rotation, translation, dense_lo, dense_hi):
    """Median capture height above the floor, in scene units, if consistent."""
    if cameras is None or len(cameras) < capture_sidecars.MIN_USABLE_POSES:
        return None
    canonical = (cameras @ rotation.T) + translation
    heights = canonical[:, 1]
    ceiling = float(dense_hi[1] - dense_lo[1]) * 1.2
    inside = heights[(heights > 0) & (heights < ceiling)]
    if len(inside) < capture_sidecars.MIN_USABLE_POSES:
        return None
    median = float(np.median(inside))
    if not median > 0:
        return None
    spread = float(np.std(inside) / median)
    return {"median": round(median, 6), "spread": round(spread, 4), "count": int(len(inside))}


def _scale(camera_height, measured_scale) -> dict[str, Any]:
    """What is known about metres-per-unit — and nothing more."""
    if isinstance(measured_scale, dict):
        try:
            mpu = float(measured_scale.get("metresPerUnit"))
        except (TypeError, ValueError):
            mpu = 0.0
        if math.isfinite(mpu) and mpu > 0:
            return {
                "status": "metric",
                "source": str(measured_scale.get("source") or "measured"),
                "metresPerUnit": mpu,
                "confidence": float(measured_scale.get("confidence") or 0.9),
                "basis": str(measured_scale.get("basis") or "Measured during capture."),
                "measurementsAllowed": True,
            }
    if camera_height and camera_height["spread"] <= 0.25:
        return {
            "status": "estimated",
            "source": "capture_height_prior",
            "metresPerUnit": round(CAPTURE_HEIGHT_PRIOR_M / camera_height["median"], 6),
            "confidence": CAPTURE_HEIGHT_PRIOR_CONFIDENCE,
            "basis": (f"Assumes the phone was held about {CAPTURE_HEIGHT_PRIOR_M} m above "
                      "the floor. Good enough to move through the space at a natural "
                      "pace; not good enough to measure anything."),
            "measurementsAllowed": False,
        }
    return {
        "status": "unknown",
        "source": "none",
        "metresPerUnit": None,
        "confidence": 0.0,
        "basis": "No measured scale reference was captured.",
        "measurementsAllowed": False,
    }
