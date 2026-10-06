"""capture_quality.py — the gate a capture passes before a GPU is rented.

Every past failure of a real capture was discovered at the END of a paid GPU
run: COLMAP registered 18-37% of a blurry walkthrough, or a set of thumbnails
produced a speck. Most of those captures were visibly bad from their pixels
alone, and saying so costs milliseconds instead of dollars.

This module looks at the frames that are about to be submitted and answers
three questions, deterministically and without a model:

1. **Can this capture support a reconstruction at all?** If not, the job is
   refused with `QualityVerdict.refused` and concrete recapture guidance —
   "walk slower", not "COLMAP failed".
2. **Which frames are useless?** Exact/near duplicates (the phone stood still)
   and frames far blurrier than the rest of the capture are dropped, bounded so
   the gate can never thin a capture below what the provider needs.
3. **What should the result carry as a caveat?** Deterministic warnings, not a
   score: "Capture had motion blur", "Some frames were dark". A 93/100 nobody
   calibrated is worse than a sentence that is true.

It never inspects image *content* beyond luminance statistics and a 16x16
thumbnail — nothing about faces, documents or belongings is computed or kept.
Counts and ratios are what leave this module.

Cheap by construction: every image is decoded through Pillow's JPEG draft mode
at <= 512 px, and at most MAX_ANALYSED frames are looked at.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger("oracle.capture_quality")

#: Never analyse more than this many frames. A capture is <= 300 photos plus
#: <= 240 sampled frames per video; beyond this the marginal frame tells the
#: gate nothing new and costs a decode.
MAX_ANALYSED = 900
#: Edge the statistics are computed at (matches frame_selection.SHARPNESS_EDGE
#: so the two measures agree on what "sharp" means).
ANALYSIS_EDGE = 512
#: Below this many usable frames no provider can register a space. Mirrors the
#: providers' own MIN_CAPTURE_IMAGES so the gate refuses what they would refuse
#: anyway — before a pod is rented rather than after.
MIN_USABLE_FRAMES = 8
#: Below this many the space is likely to have holes. A warning, not a refusal.
THIN_CAPTURE_FRAMES = 40
#: Short edge in pixels. Thumbnails (<= 360) cannot be reconstructed usefully;
#: below 720 the result is soft.
MIN_SHORT_EDGE = 360
SOFT_SHORT_EDGE = 720
#: Mean luminance (0-255) below which a frame is effectively black.
DARK_LUMA = 22.0
#: Laplacian variance at ANALYSIS_EDGE below which a frame is blurred beyond
#: use regardless of the rest of the capture (a measured floor: sharp phone
#: frames at 512 px score in the hundreds, a whip-pan scores single digits).
BLUR_ABSOLUTE = 12.0
#: A frame this much blurrier than the capture's median is dropped when the
#: capture can afford it — a relative test, so a uniformly soft capture is not
#: emptied by an absolute threshold tuned for a sharp one.
BLUR_RELATIVE = 0.18
#: Mean absolute difference (0-255) between consecutive 16x16 thumbnails below
#: which two frames are the same view.
DUPLICATE_DIFF = 1.6

#: Refusal thresholds, as fractions of readable frames.
REFUSE_DARK_SHARE = 0.6
REFUSE_BLUR_SHARE = 0.7
REFUSE_DUPLICATE_SHARE = 0.8
#: Warning thresholds.
WARN_DARK_SHARE = 0.15
WARN_BLUR_SHARE = 0.2
WARN_DUPLICATE_SHARE = 0.3

#: Concise, customer-language recapture guidance, keyed by finding. Never
#: names a tool, an algorithm or a GPU — the person holding the phone needs to
#: know what to do differently, not why the solver failed.
GUIDANCE: dict[str, str] = {
    "no_media": "Upload photos or a walkthrough video of the property first.",
    "too_few_frames": "Capture more of the space — walk every room. Aim for 120+ photos or a 2–4 minute video per floor.",
    "thin_capture": "More coverage helps: finish a full loop of each room before moving on.",
    "low_resolution": "Use the phone's main camera at full resolution — not screenshots or thumbnails.",
    "soft_resolution": "Shoot at the camera's full resolution for a sharper space.",
    "dark": "Turn on every light and open the blinds — dark frames come out blurred.",
    "blurry": "Walk slower and hold the phone steady; avoid quick turns.",
    "duplicates": "Keep moving while you capture — standing still only repeats the same view.",
    "mixed_orientation": "Keep the phone in one orientation for the whole capture.",
    "unreadable": "Some files could not be read. Re-upload them as JPEG/PNG photos or MP4/MOV video.",
    "keep_level": "Hold the phone level at chest height as you walk.",
    "cover_transitions": "Walk slowly through doorways so rooms connect, and cover each transition.",
    "still_scene": "Keep people and pets out of the shot, and turn off TVs and screens while you capture.",
}

#: Customer-facing caveat for each warning that survives into a READY space.
WARNING_LABELS: dict[str, str] = {
    "thin_capture": "Low coverage — some areas may be incomplete",
    "soft_resolution": "Captured at low resolution",
    "dark": "Some frames were dark",
    "blurry": "Capture had motion blur",
    "duplicates": "Capture paused in places",
    "mixed_orientation": "Mixed phone orientation",
    "unreadable": "Some files could not be read",
}


@dataclass
class FrameStats:
    path: Path
    width: int = 0
    height: int = 0
    luma: Optional[float] = None
    sharpness: Optional[float] = None
    thumb: Optional[Any] = None   # 16x16 float array, never persisted
    readable: bool = True


@dataclass
class QualityVerdict:
    """What the gate decided. `kept` is what goes to reconstruction."""

    refused: bool
    reason: Optional[str]                 # finding code that refused, if any
    kept: list[Path]
    dropped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def guidance(self) -> list[str]:
        codes = ([self.reason] if self.reason else []) + list(self.warnings)
        out: list[str] = []
        for code in codes:
            tip = GUIDANCE.get(code)
            if tip and tip not in out:
                out.append(tip)
        # A refusal on coverage almost always also means transitions were
        # missed; one extra line is worth it there and nowhere else.
        if self.reason in ("too_few_frames", "blurry") and GUIDANCE["cover_transitions"] not in out:
            out.append(GUIDANCE["cover_transitions"])
        return out[:4]

    def as_diagnostics(self) -> dict[str, Any]:
        return {
            "refused": self.refused,
            "reason": self.reason,
            "kept": len(self.kept),
            "dropped": dict(self.dropped),
            "warnings": list(self.warnings),
            **self.metrics,
        }


def _analyse(path: Path) -> FrameStats:
    stats = FrameStats(path=path)
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            stats.width, stats.height = im.size
            im.draft("L", (ANALYSIS_EDGE, ANALYSIS_EDGE))
            grey = im.convert("L")
            grey.thumbnail((ANALYSIS_EDGE, ANALYSIS_EDGE))
            arr = np.asarray(grey, dtype=float)
            thumb = np.asarray(grey.resize((16, 16)), dtype=float)
    except Exception:  # noqa: BLE001 — an unreadable frame is a finding, not a crash
        stats.readable = False
        return stats
    if arr.ndim != 2 or min(arr.shape) < 3:
        stats.readable = False
        return stats
    stats.luma = float(arr.mean())
    centre = arr[1:-1, 1:-1]
    lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:] - 4.0 * centre)
    stats.sharpness = float(lap.var())
    stats.thumb = thumb
    return stats


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def assess(images: Sequence[Path], *, minimum: int = MIN_USABLE_FRAMES,
           analyse=_analyse) -> QualityVerdict:
    """Assess a capture and choose the frames worth reconstructing.

    Order is preserved (a capture is a walk; sequential matching depends on
    it). Dropping is bounded: the gate never removes a frame if doing so would
    leave fewer than `minimum` — a borderline capture gets its chance on the
    GPU with an honest warning rather than a refusal the gate manufactured.

    `analyse` is injectable so tests can describe frames without encoding
    images.
    """
    frames = list(images)
    if not frames:
        return QualityVerdict(refused=True, reason="no_media", kept=[],
                              metrics={"submitted": 0})

    if len(frames) > MAX_ANALYSED:
        # Even spacing, never truncation: the end of a walk is as much of the
        # house as the beginning.
        step = len(frames) / MAX_ANALYSED
        frames = [frames[min(len(frames) - 1, int(i * step))] for i in range(MAX_ANALYSED)]

    stats = [analyse(p) for p in frames]
    readable = [s for s in stats if s.readable]
    unreadable = len(stats) - len(readable)

    metrics: dict[str, Any] = {
        "submitted": len(images),
        "analysed": len(stats),
        "readable": len(readable),
        "unreadable": unreadable,
    }
    warnings: list[str] = []
    if unreadable:
        warnings.append("unreadable")

    if len(readable) < minimum:
        return QualityVerdict(refused=True, reason="too_few_frames",
                              kept=[s.path for s in readable], warnings=warnings,
                              dropped={"unreadable": unreadable}, metrics=metrics)

    # ── resolution ──────────────────────────────────────────────────────
    short_edges = [min(s.width, s.height) for s in readable if s.width and s.height]
    median_short = _median(short_edges) if short_edges else 0.0
    metrics["median_short_edge"] = int(median_short)
    portrait = sum(1 for s in readable if s.height > s.width)
    landscape = sum(1 for s in readable if s.width > s.height)
    metrics["portrait_frames"] = portrait
    metrics["landscape_frames"] = landscape
    if median_short and median_short < MIN_SHORT_EDGE:
        return QualityVerdict(refused=True, reason="low_resolution",
                              kept=[s.path for s in readable], warnings=warnings,
                              metrics=metrics)
    if median_short and median_short < SOFT_SHORT_EDGE:
        warnings.append("soft_resolution")
    if min(portrait, landscape) > 0.15 * len(readable):
        warnings.append("mixed_orientation")

    # ── darkness ────────────────────────────────────────────────────────
    dark = [s for s in readable if s.luma is not None and s.luma < DARK_LUMA]
    dark_share = len(dark) / len(readable)
    metrics["dark_frames"] = len(dark)
    metrics["median_luma"] = round(_median([s.luma for s in readable if s.luma is not None]), 1)
    if dark_share >= REFUSE_DARK_SHARE:
        return QualityVerdict(refused=True, reason="dark", kept=[s.path for s in readable],
                              warnings=warnings, metrics=metrics)
    if dark_share >= WARN_DARK_SHARE:
        warnings.append("dark")

    # ── duplicates (consecutive near-identical views) ───────────────────
    duplicate_ids: set[int] = set()
    previous = None
    for s in readable:
        if previous is not None and s.thumb is not None and previous.thumb is not None:
            try:
                diff = float(abs(s.thumb - previous.thumb).mean())
            except Exception:  # noqa: BLE001
                diff = 255.0
            if diff < DUPLICATE_DIFF:
                duplicate_ids.add(id(s))
                continue   # compare the next frame against the one we kept
        previous = s
    dup_share = len(duplicate_ids) / len(readable)
    metrics["duplicate_frames"] = len(duplicate_ids)
    if dup_share >= REFUSE_DUPLICATE_SHARE:
        return QualityVerdict(refused=True, reason="duplicates",
                              kept=[s.path for s in readable if id(s) not in duplicate_ids],
                              warnings=warnings, metrics=metrics)
    if dup_share >= WARN_DUPLICATE_SHARE:
        warnings.append("duplicates")

    # ── blur ────────────────────────────────────────────────────────────
    scored = [s.sharpness for s in readable if s.sharpness is not None]
    median_sharp = _median(scored) if scored else 0.0
    metrics["median_sharpness"] = round(median_sharp, 1)
    blur_cut = max(BLUR_ABSOLUTE, median_sharp * BLUR_RELATIVE)
    blurred_ids = {id(s) for s in readable
                   if s.sharpness is not None and s.sharpness < blur_cut}
    hopeless = [s for s in readable if s.sharpness is not None and s.sharpness < BLUR_ABSOLUTE]
    metrics["blurred_frames"] = len(blurred_ids)
    if len(hopeless) / len(readable) >= REFUSE_BLUR_SHARE:
        return QualityVerdict(refused=True, reason="blurry",
                              kept=[s.path for s in readable], warnings=warnings,
                              metrics=metrics)
    if len(blurred_ids) / len(readable) >= WARN_BLUR_SHARE:
        warnings.append("blurry")

    # ── selection, bounded ──────────────────────────────────────────────
    # Duplicates first (pure redundancy), then the blurriest — and only while
    # the capture stays above `minimum`.
    drop_order = [s for s in readable if id(s) in duplicate_ids] + sorted(
        (s for s in readable if id(s) in blurred_ids and id(s) not in duplicate_ids),
        key=lambda s: s.sharpness or 0.0,
    )
    budget = max(0, len(readable) - minimum)
    dropping = {id(s) for s in drop_order[:budget]}
    kept = [s.path for s in readable if id(s) not in dropping]
    dropped = {
        "unreadable": unreadable,
        "duplicate": sum(1 for s in readable if id(s) in dropping and id(s) in duplicate_ids),
        "blurred": sum(1 for s in readable if id(s) in dropping and id(s) not in duplicate_ids),
    }
    if len(kept) < THIN_CAPTURE_FRAMES:
        warnings.append("thin_capture")
    metrics["kept"] = len(kept)
    return QualityVerdict(refused=False, reason=None, kept=kept, dropped=dropped,
                          warnings=warnings, metrics=metrics)
