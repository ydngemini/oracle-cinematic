"""Choose which frames a reconstruction actually trains on.

A walkthrough video is the only capture an ordinary person will really do —
nobody is taking 350 deliberate photographs of their own house. But a phone
walking through a room produces frames that are not equally useful: the ones
taken mid-turn are motion-blurred, and a blurred frame does not merely add
nothing. COLMAP still detects features in it, matches them badly, and drags
the pose solution toward a wrong answer, so a blurry frame is worse for the
reconstruction than no frame at all.

Selection used to be blind twice over: ffmpeg sampled every half second
regardless of what was in the picture, and the result was then thinned by
taking every Nth. Both preserve coverage and neither looks at quality.

This keeps the coverage and adds the quality. The sequence is cut into as many
contiguous buckets as there are slots, and the sharpest frame in each bucket
wins. Buckets are what protect the coverage — taking the globally sharpest N
would happily return fifty views of the one well-lit wall the photographer
stood still in front of, and nothing of the hallway.

Sharpness is the variance of the Laplacian: the standard, cheap focus measure.
High variance means strong local intensity changes, which is what an in-focus
edge looks like; a blurred image smears those out and the variance collapses.
It is computed on a downscaled greyscale copy, because focus is a property of
the whole frame and reading every pixel of 150 full-size images to rank them
would cost more than it saves.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("oracle.frame_selection")

#: Longest edge the sharpness measure works on. Small enough to be quick,
#: large enough that real focus differences survive the downscale.
SHARPNESS_EDGE = 512


def sharpness(path: Path) -> Optional[float]:
    """Variance of the Laplacian, or None when the image cannot be read.

    None is not zero. An unreadable file must not be ranked as "very blurry"
    and silently dropped in favour of something worse — the caller keeps it and
    lets the reconstruction decide.
    """
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            im.draft("L", (SHARPNESS_EDGE, SHARPNESS_EDGE))  # cheap JPEG downscale
            grey = im.convert("L")
            grey.thumbnail((SHARPNESS_EDGE, SHARPNESS_EDGE))
            arr = np.asarray(grey, dtype=float)
    except Exception:  # noqa: BLE001 - an unreadable frame is a fact, not a crash
        return None

    if arr.ndim != 2 or min(arr.shape) < 3:
        return None

    # 4-neighbour Laplacian over the interior; no scipy dependency for one kernel.
    centre = arr[1:-1, 1:-1]
    lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
           - 4.0 * centre)
    return float(lap.var())


def select_sharpest(images: Sequence[Path], target: int) -> list[Path]:
    """Thin `images` to `target`, keeping the sharpest of each contiguous run.

    Order is preserved: a capture is a path through a building, and the
    matcher's sequential mode assumes neighbouring frames are neighbouring
    viewpoints. Returns the input unchanged when it already fits, or when
    nothing could be scored.
    """
    frames = list(images)
    if target <= 0 or len(frames) <= target:
        return frames

    scores = [sharpness(p) for p in frames]
    if all(s is None for s in scores):
        # Pillow or numpy missing, or every file unreadable. Fall back to even
        # spacing, which is what this replaced — degraded, not broken.
        log.info("No frame could be scored for sharpness; spacing evenly instead.")
        step = len(frames) / target
        return [frames[min(len(frames) - 1, int(i * step))] for i in range(target)]

    chosen: list[Path] = []
    n = len(frames)
    for slot in range(target):
        lo = (slot * n) // target
        hi = ((slot + 1) * n) // target
        if hi <= lo:
            hi = lo + 1
        best_index = lo
        best_score = -1.0
        for i in range(lo, min(hi, n)):
            # Unscorable frames rank below any real score but above nothing,
            # so a bucket of only-unreadable frames still contributes one.
            s = scores[i]
            value = -0.5 if s is None else s
            if value > best_score:
                best_score, best_index = value, i
        chosen.append(frames[best_index])

    kept = [s for s in (scores[frames.index(c)] for c in chosen) if s is not None]
    if kept:
        log.info(
            "Selected %d of %d frames by sharpness (median kept %.0f, "
            "median overall %.0f)",
            len(chosen), n, _median(kept),
            _median([s for s in scores if s is not None]),
        )
    return chosen


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
