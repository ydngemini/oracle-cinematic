"""The post-reconstruction quality gate (recon_quality.assess)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


# ── invalid numbers are not measurements (T12.09, AC12.06) ──────────────────
INF = float("inf")


def test_infinite_or_out_of_range_numbers_are_not_measurements():
    """A broken report must not read as a clean one: before, an infinite PSNR,
    an SSIM of 7 and a registration ratio of 3.5 gave no caveat at all."""
    v = rq.assess({"held_out": {"psnr": INF, "ssim": 7.0},
                   "colmap": {"registration_ratio": 3.5}, "input_images": 150})
    assert not v.blocked
    assert "quality_unverified" in v.limitations, "nothing valid was measured"
    assert set(v.metrics["invalid"]) == {"held_out_psnr", "held_out_ssim", "registration_ratio"}
    for name in ("held_out_psnr", "held_out_ssim", "registration_ratio"):
        assert name not in v.metrics, "an invalid number is never recorded as a value"


def test_the_recorded_metrics_are_json_safe():
    """jsonb refuses Infinity: one infinite number used to lose the whole
    quality stage from the job's diagnostics."""
    import json

    v = rq.assess(_with(held_out__lpips=INF, roundtrip={"mean": {"sog_vs_pruned": INF}}))
    json.dumps(v.metrics, allow_nan=False)
    assert v.metrics["invalid"] == ["held_out_lpips"]


@pytest.mark.parametrize("path, name", [
    ("held_out__psnr=-2.0", "held_out_psnr"), ("held_out__psnr=180.0", "held_out_psnr"),
    ("held_out__ssim=1.5", "held_out_ssim"), ("held_out__lpips=-0.3", "held_out_lpips"),
    ("colmap__registration_ratio=-0.1", "registration_ratio"),
    ("colmap__mean_reprojection_error_px=-1.0", "mean_reprojection_error_px"),
    ("input_images=-4", "input_images"),
])
def test_out_of_range_values_are_flagged_invalid(path, name):
    key, _, value = path.partition("=")
    v = rq.assess(_with(**{key: float(value)}))
    assert name in v.metrics["invalid"]
    assert name not in {k for k in v.metrics if k != "invalid"}


@pytest.mark.parametrize("key, value, name", [
    ("colmap__registration_ratio", True, "registration_ratio"),
    ("colmap__registration_ratio", "0.95", "registration_ratio"),
    ("held_out__ssim", "0.95", "held_out_ssim"),
    ("held_out__psnr", True, "held_out_psnr"),
    ("input_images", "150", "input_images"),
])
def test_booleans_and_strings_are_not_measurements(key, value, name):
    """float(True) is 1.0 and float("0.95") is 0.95: neither is a number an
    instrument reported, so neither may pass as a perfect ratio or a target."""
    v = rq.assess(_with(**{key: value}))
    assert name in v.metrics["invalid"]
    assert name not in {k for k in v.metrics if k != "invalid"}
    assert rq.valid_measurement(name, value) == (None, True)


def test_valid_numbers_still_gate_exactly_as_before():
    assert rq.assess(_with(held_out__psnr=13.0)).blocked
    assert rq.assess(_with(colmap__registration_ratio=0.15)).blocked
    assert "invalid" not in rq.assess(GOOD).metrics


def test_the_frame_count_is_recorded_beside_the_ratio():
    """A ratio means nothing without the number of frames it is a ratio of."""
    assert rq.assess(GOOD).metrics["input_images"] == 150
