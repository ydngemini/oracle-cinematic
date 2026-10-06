"""Reconstruction quality gate: does the finished Space look like the room?

Until this existed, a reconstruction was published as soon as training wrote a
non-empty .sog — "file exists" stood in for "looks real". The pod now measures
the run (backend/recon_pod_tools.py → quality.json) and this module turns those
measurements into one of three outcomes:

* **block** — the result is so far from the photographs that publishing it
  would mislead. The job fails at the quality gate with recapture guidance; the
  raw output is preserved, so nothing paid for is lost.
* **limitation** — usable, with a true, specific caveat shown to the customer
  (space_status.LIMITATION_LABELS).
* **pass** — nothing to say.

THE THRESHOLDS ARE PROVISIONAL. They rest on a handful of real runs (the
golden capture: PSNR 23.16 / SSIM 0.807 at 7k steps; real phone walkthroughs
registering 18–37% of frames), which is not a sample anyone should trust as a
calibration. So the BLOCK lines are deliberately far out — results nobody would
publish — and everything in between is a caveat plus a recorded number. Tighten
them only against more measured runs (docs/neoh-space-quality.md).

Missing measurements are never treated as passing or failing: a run with no
report is `quality_unverified`, said plainly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# ── provisional thresholds (see module docstring) ───────────────────────────

#: Held-out PSNR (dB) of the render vs the photo it never trained on. With the
#: exposure model on, the colour-corrected figure is used for BLOCKING (an
#: exposure mismatch is not a broken room) and the raw figure for the caveat
#: (the viewer shows the raw colours).
BLOCK_PSNR_DB = 16.0
SOFT_PSNR_DB = 22.0
BLOCK_SSIM = 0.55
SOFT_SSIM = 0.72

#: Share of the capture COLMAP could place. Below BLOCK most of the house is
#: simply absent; below PARTIAL, parts of it are.
BLOCK_REGISTRATION = 0.20
PARTIAL_REGISTRATION = 0.60
#: Too few frames for a ratio to mean anything.
MIN_FRAMES_FOR_RATIO = 20

#: Mean reprojection error (px). Sub-pixel is a clean solve.
SOFT_REPROJECTION_PX = 1.5
BLOCK_REPROJECTION_PX = 4.0

#: Render of the delivered .sog vs the PLY it was converted from, on held-out
#: views. Below this, compression is visibly softening detail.
COMPRESSION_PSNR_DB = 28.0


@dataclass
class QualityVerdict:
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)       # why it was blocked
    limitations: list[str] = field(default_factory=list)   # LIMITATION_LABELS keys
    render_model: str = "classic"                           # how the viewer must draw it
    metrics: dict[str, Any] = field(default_factory=dict)   # the numbers, for diagnostics

    def add(self, limitation: str) -> None:
        if limitation not in self.limitations:
            self.limitations.append(limitation)


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN is "not measured"


def assess(quality: Optional[Mapping[str, Any]]) -> QualityVerdict:
    """Judge one run's quality.json (as parsed). Pure; never raises."""
    verdict = QualityVerdict()
    if not isinstance(quality, Mapping) or not quality:
        verdict.add("quality_unverified")
        return verdict

    training = quality.get("training") if isinstance(quality.get("training"), Mapping) else {}
    if training.get("antialiased") is True:
        verdict.render_model = "antialiased"

    held = quality.get("held_out") if isinstance(quality.get("held_out"), Mapping) else {}
    psnr, ssim = _num(held.get("psnr")), _num(held.get("ssim"))
    cc_psnr, cc_ssim = _num(held.get("cc_psnr")), _num(held.get("cc_ssim"))
    geometric_psnr = cc_psnr if cc_psnr is not None else psnr
    geometric_ssim = cc_ssim if cc_ssim is not None else ssim
    verdict.metrics.update({k: v for k, v in {
        "held_out_psnr": psnr, "held_out_ssim": ssim, "held_out_lpips": _num(held.get("lpips")),
        "held_out_cc_psnr": cc_psnr, "held_out_cc_ssim": cc_ssim,
        "gaussians": _num(held.get("num_GS")), "steps": _num(held.get("step")),
    }.items() if v is not None})
    if psnr is None and ssim is None:
        verdict.add("quality_unverified")
    else:
        if geometric_psnr is not None and geometric_psnr < BLOCK_PSNR_DB:
            verdict.blocked = True
            verdict.reasons.append(
                f"renders differ badly from the photographs (held-out PSNR {geometric_psnr:.1f} dB)")
        if geometric_ssim is not None and geometric_ssim < BLOCK_SSIM:
            verdict.blocked = True
            verdict.reasons.append(
                f"renders lose the structure of the photographs (held-out SSIM {geometric_ssim:.2f})")
        if (psnr is not None and psnr < SOFT_PSNR_DB) or (ssim is not None and ssim < SOFT_SSIM):
            verdict.add("quality_low")

    colmap = quality.get("colmap") if isinstance(quality.get("colmap"), Mapping) else {}
    ratio = _num(colmap.get("registration_ratio"))
    frames = _num(quality.get("input_images"))
    reprojection = _num(colmap.get("mean_reprojection_error_px"))
    if ratio is not None:
        verdict.metrics["registration_ratio"] = ratio
        if frames is not None and frames >= MIN_FRAMES_FOR_RATIO and ratio < BLOCK_REGISTRATION:
            verdict.blocked = True
            verdict.reasons.append(
                f"only {ratio:.0%} of the capture could be placed in 3D")
        elif ratio < PARTIAL_REGISTRATION:
            verdict.add("partial_registration")
    if reprojection is not None:
        verdict.metrics["mean_reprojection_error_px"] = reprojection
        if reprojection > BLOCK_REPROJECTION_PX:
            verdict.blocked = True
            verdict.reasons.append(
                f"camera positions disagree with the photographs ({reprojection:.1f} px)")
        elif reprojection > SOFT_REPROJECTION_PX:
            verdict.add("pose_error_high")

    roundtrip = quality.get("roundtrip") if isinstance(quality.get("roundtrip"), Mapping) else {}
    mean = roundtrip.get("mean") if isinstance(roundtrip.get("mean"), Mapping) else {}
    compression = _num(mean.get("sog_vs_pruned"))
    if compression is not None:
        verdict.metrics["compression_psnr_db"] = compression
        verdict.metrics["pruning_psnr_db"] = _num(mean.get("pruned_vs_raw"))
        if compression < COMPRESSION_PSNR_DB:
            verdict.add("compression_loss")

    masks = quality.get("masks") if isinstance(quality.get("masks"), Mapping) else {}
    if _num(masks.get("frames_masked")):
        verdict.metrics["frames_masked"] = _num(masks.get("frames_masked"))
        verdict.metrics["mean_masked_fraction"] = _num(masks.get("mean_masked_fraction"))
    prune = quality.get("prune") if isinstance(quality.get("prune"), Mapping) else {}
    before, after = _num(prune.get("before")), _num(prune.get("after"))
    if before and after is not None and prune.get("accepted") is True:
        verdict.metrics["floaters_removed"] = int(before - after)
    lens = quality.get("lens_groups") if isinstance(quality.get("lens_groups"), Mapping) else {}
    if isinstance(lens.get("groups"), list):
        verdict.metrics["lens_groups"] = len(lens["groups"])
    return verdict
