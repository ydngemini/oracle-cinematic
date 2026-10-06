"""The post-reconstruction quality gate (recon_quality.assess)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recon_quality as rq  # noqa: E402
import space_status  # noqa: E402

GOOD = {
    "input_images": 150,
    "colmap": {"registration_ratio": 0.93, "mean_reprojection_error_px": 0.6},
    "held_out": {"psnr": 27.1, "ssim": 0.88, "lpips": 0.15, "num_GS": 2_100_000, "step": 30000},
    "training": {"antialiased": True, "bilateral_grid": True, "masks": True},
    "roundtrip": {"mean": {"sog_vs_pruned": 34.0, "pruned_vs_raw": 41.0}},
    "prune": {"before": 2_100_000, "after": 2_000_000, "accepted": True},
    "masks": {"frames_masked": 6, "mean_masked_fraction": 0.02},
    "lens_groups": {"groups": [{"key": "a", "frames": 150}]},
}


def _with(**changes):
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in GOOD.items()}
    for path, value in changes.items():
        section, _, key = path.partition("__")
        if key:
            out[section] = {**out.get(section, {}), key: value}
        else:
            out[section] = value
    return out


def test_a_good_run_passes_with_its_numbers_recorded():
    v = rq.assess(GOOD)
    assert not v.blocked and v.limitations == []
    assert v.render_model == "antialiased"
    assert v.metrics["held_out_psnr"] == 27.1
    assert v.metrics["floaters_removed"] == 100_000
    assert v.metrics["lens_groups"] == 1 and v.metrics["frames_masked"] == 6


def test_no_report_is_unverified_not_passed():
    for missing in (None, {}, "garbage"):
        v = rq.assess(missing)
        assert not v.blocked and v.limitations == ["quality_unverified"]
        assert v.render_model == "classic"


def test_a_report_without_held_out_metrics_is_unverified():
    v = rq.assess(_with(held_out={}))
    assert "quality_unverified" in v.limitations and not v.blocked


def test_renders_far_from_the_photographs_are_blocked():
    v = rq.assess(_with(held_out__psnr=13.0))
    assert v.blocked and "PSNR" in v.reasons[0]
    v = rq.assess(_with(held_out__ssim=0.4))
    assert v.blocked and "SSIM" in v.reasons[0]


def test_an_exposure_mismatch_alone_does_not_block():
    """With the exposure model, raw colours may differ from a held-out photo
    whose exposure was never seen; the colour-corrected figure decides blocking."""
    v = rq.assess(_with(held_out__psnr=15.0, held_out__cc_psnr=24.0, held_out__cc_ssim=0.85))
    assert not v.blocked
    assert "quality_low" in v.limitations, "the viewer still shows the raw colours"


def test_soft_results_are_a_caveat():
    v = rq.assess(_with(held_out__psnr=20.5))
    assert not v.blocked and v.limitations == ["quality_low"]


def test_registration_thresholds():
    assert rq.assess(_with(colmap__registration_ratio=0.15)).blocked
    partial = rq.assess(_with(colmap__registration_ratio=0.45))
    assert not partial.blocked and partial.limitations == ["partial_registration"]
    tiny = rq.assess(_with(input_images=10, colmap__registration_ratio=0.1))
    assert not tiny.blocked, "too few frames for a ratio to mean anything"


def test_reprojection_thresholds():
    assert rq.assess(_with(colmap__mean_reprojection_error_px=5.0)).blocked
    assert rq.assess(_with(colmap__mean_reprojection_error_px=2.0)).limitations == ["pose_error_high"]


def test_compression_loss_is_a_caveat():
    v = rq.assess(_with(roundtrip={"mean": {"sog_vs_pruned": 24.0, "pruned_vs_raw": 40.0}}))
    assert v.limitations == ["compression_loss"] and not v.blocked


def test_not_antialiased_means_classic():
    assert rq.assess(_with(training__antialiased=False)).render_model == "classic"


def test_nan_is_not_a_measurement():
    v = rq.assess(_with(held_out={"psnr": float("nan"), "ssim": float("nan")}))
    assert "quality_unverified" in v.limitations and not v.blocked


def test_every_limitation_has_a_customer_label():
    keys = set()
    for q in (None, _with(held_out__psnr=20.0), _with(colmap__registration_ratio=0.4),
              _with(colmap__mean_reprojection_error_px=2.0),
              _with(roundtrip={"mean": {"sog_vs_pruned": 20.0}})):
        keys |= set(rq.assess(q).limitations)
    assert keys <= set(space_status.LIMITATION_LABELS), keys - set(space_status.LIMITATION_LABELS)
    for key in keys:
        label = space_status.LIMITATION_LABELS[key]
        for word in ("PSNR", "SSIM", "COLMAP", "Gaussian", "splat", "GPU"):
            assert word.lower() not in label.lower(), label
