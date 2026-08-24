"""Where the cameras were, carried alongside the geometry they produced.

A reconstruction knows something the point cloud alone cannot express: where the
photographer stood. `floorplan_pipeline.slicing.estimate_up_axis` takes exactly
that and uses it to settle the orientations geometry cannot — a near-cubic
bathroom, a galley kitchen whose width and ceiling height differ by 100 mm, a
stairwell taller than it is wide. A rectangular box maps any face-pair onto any
other, so no scale-free measure separates them; a camera carried at eye height
does, because the direction it is confined along is gravity.

COLMAP computes those positions on the way to the splat and they were thrown
away at the end of the job, which left the one parameter that resolves those
rooms with no way to ever be supplied in production.

**The frame is the whole correctness argument.** Camera centres are only useful
if they are in the same coordinate frame as the geometry they will be compared
against, and a trainer that recentres or rescales the scene silently breaks
that. Mixing a raw COLMAP centre with a normalised cloud does not raise — it
returns a confident, wrong up axis, which is the failure this pipeline exists to
prevent. So the frame is recorded in the file and checked on the way out.

Deliberately stdlib-only and free-standing: the writer lives in
`reconstruction_providers`, the reader in the floor plan path, and neither
should have to import the other to agree on a file name.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger("oracle.capture_poses")

#: Appended to the artifact's full name, not swapped for its suffix:
#: `model.sog` -> `model.sog.cameras.json`. Keeping the artifact name whole
#: means one glob finds both, and two artifacts that differ only by extension
#: cannot collide on a single sidecar.
CAMERA_SIDECAR_SUFFIX = ".cameras.json"

#: The frame the positions are expressed in.
#:
#: `trained` is the frame of the delivered splat — the only one a consumer can
#: use without knowing what the trainer did. gsplat's Parser normalises the
#: scene (recentre + rescale) before training, so COLMAP's own centres are NOT
#: interchangeable with it. `colmap` is accepted and recorded so a file that
#: predates that understanding is refused rather than silently misread.
FRAME_TRAINED = "trained"
FRAME_COLMAP = "colmap"

SCHEMA_VERSION = 1

#: A capture with fewer than this tells you nothing about gravity, and
#: `estimate_up_axis` needs four before it will use them for the axis at all.
MIN_USABLE_POSES = 4


def sidecar_for(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + CAMERA_SIDECAR_SUFFIX)


def write(artifact: Path, positions: Sequence[Sequence[float]], *,
          frame: str = FRAME_TRAINED, source: str = "colmap") -> Optional[Path]:
    """Record camera centres beside `artifact`. Returns the path, or None.

    Never raises. These positions are an accelerator for one estimate, and a
    reconstruction that succeeded must not be failed by trouble writing a
    sidecar to it — the same rule the point segmenter follows.
    """
    try:
        cleaned = [
            [float(p[0]), float(p[1]), float(p[2])]
            for p in positions
            if p is not None and len(p) >= 3
        ]
        if len(cleaned) < MIN_USABLE_POSES:
            log.info(
                "Not recording camera poses: %d is below the %d that could "
                "resolve an axis.", len(cleaned), MIN_USABLE_POSES,
            )
            return None
        path = sidecar_for(artifact)
        path.write_text(json.dumps({
            "version": SCHEMA_VERSION,
            "frame": frame,
            "source": source,
            "count": len(cleaned),
            "positions": cleaned,
        }))
        return path
    except Exception:  # noqa: BLE001
        log.exception("Could not write camera poses beside %s", artifact)
        return None


def read(artifact: Path, *, frame: str = FRAME_TRAINED) -> Optional[list[list[float]]]:
    """Camera centres recorded beside `artifact`, or None.

    None is a normal answer — most artifacts have no sidecar, and every caller
    already handles "no camera positions" because that was the only case until
    now.

    A sidecar in the wrong frame is refused rather than returned. It would not
    look wrong: the numbers are the right shape and the right order of
    magnitude, and using them produces a confident up axis pointing somewhere
    else entirely.
    """
    try:
        path = sidecar_for(artifact)
        if not path.is_file():
            return None
        payload: Any = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError("sidecar is not an object")
        if int(payload.get("version", 0)) != SCHEMA_VERSION:
            log.warning(
                "Camera poses beside %s are schema v%s; this build reads v%d — ignoring.",
                artifact.name, payload.get("version"), SCHEMA_VERSION,
            )
            return None
        recorded = payload.get("frame")
        if recorded != frame:
            log.warning(
                "Camera poses beside %s are in the %r frame, but the %r frame was "
                "asked for. Using them would return a confident, wrong axis — ignoring.",
                artifact.name, recorded, frame,
            )
            return None
        positions = payload.get("positions") or []
        cleaned = [
            [float(p[0]), float(p[1]), float(p[2])]
            for p in positions
            if isinstance(p, (list, tuple)) and len(p) >= 3
        ]
        if len(cleaned) < MIN_USABLE_POSES:
            return None
        return cleaned
    except Exception:  # noqa: BLE001
        log.exception("Could not read camera poses beside %s", artifact)
        return None
