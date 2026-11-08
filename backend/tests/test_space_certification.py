"""The certification policy: when may a 3D space be called verified? (T12.09)

"Ready to view" was the only state a space had, and every reader derived a
quality claim from it: a published capture with no failing caveat was labelled
as if its quality had been checked (audit A03). This file pins the policy that
replaces that inference:

* `verified` needs four measured checks — held-out image quality, camera
  registration, room connectivity and real-device tour performance — every one
  passed, on evidence that covers the newest capture media;
* a missing or invalid metric is missing evidence (CONTRACT C6), never a pass;
* a measured failure, a coverage gap or a quality caveat is said as such,
  ahead of what was not measured;
* nothing the platform stores today can reach `verified` — no producer of
  room-connectivity or device-run evidence exists yet — and that is pinned.

Pure: no database, no network.
"""

from __future__ import annotations

import itertools

import pytest

import space_certification as sc

INF = float("inf")
NAN = float("nan")

# ── one input per check status, as stored in diagnostics.quality ────────────
HELD_OUT = {
    "passed": {"held_out_psnr": 28.8, "held_out_ssim": 0.89, "held_out_lpips": 0.086},
    "acceptable": {"held_out_psnr": 24.0, "held_out_ssim": 0.80, "held_out_lpips": 0.20},
    "limited": {"held_out_psnr": 20.5, "held_out_ssim": 0.80, "held_out_lpips": 0.20},
    "failed": {"held_out_psnr": 13.0, "held_out_ssim": 0.80, "held_out_lpips": 0.40},
    "missing": {},
    "invalid": {"held_out_psnr": 28.8, "held_out_ssim": 7.0, "held_out_lpips": 0.086},
}
REGISTRATION = {
    "passed": {"registration_ratio": 0.95, "mean_reprojection_error_px": 0.5, "input_images": 150},
    "limited": {"registration_ratio": 0.95, "mean_reprojection_error_px": 2.0, "input_images": 150},
    "coverage_gap": {"registration_ratio": 0.45, "mean_reprojection_error_px": 0.5, "input_images": 150},
    "failed": {"registration_ratio": 0.10, "mean_reprojection_error_px": 0.5, "input_images": 150},
    "missing": {},
    "invalid": {"registration_ratio": 3.5, "mean_reprojection_error_px": 0.5, "input_images": 150},
}
ROOMS = {
    "passed": {"rooms": 3, "components": 1, "edges": 2, "verified_edges": 2, "expected_rooms": 3},
    "coverage_gap": {"rooms": 2, "components": 2, "edges": 1, "verified_edges": 0, "expected_rooms": 2},
    "missing": None,
    "invalid": {"rooms": 2, "components": 0, "edges": 1, "verified_edges": 1},
}
#: The published space being certified: a device run counts only for the
#: space (property_media id) it measured.
SPACE = "dddddddd-0000-4000-8000-000000000001"
OTHER_SPACE = "dddddddd-0000-4000-8000-000000000002"


def _run(platform, fps, **extra):
    return {"platform": platform, "real_device": True, "steady_fps": fps, "media_id": SPACE,
            **extra}


DEVICES = {
    "passed": [_run("ios_safari", 41.0), _run("android_chrome", 34.0)],
    "limited": [_run("ios_safari", 41.0), _run("android_chrome", 22.0)],
    "missing": None,
    "invalid": [_run("ios_safari", INF), _run("android_chrome", 34.0)],
}


def _quality(held="passed", reg="passed", rooms="passed"):
    out = {**HELD_OUT[held], **REGISTRATION[reg]}
    if ROOMS[rooms] is not None:
        out["room_graph"] = ROOMS[rooms]
    return out


def _certify(held="passed", reg="passed", rooms="passed", device="passed", *,
             fresh=True, job_status="succeeded", limitations=(), unread=0):
    return sc.certify(job_found=True, job_status=job_status, limitations=list(limitations),
                      quality=_quality(held, reg, rooms), fresh=fresh,
                      device_runs=DEVICES[device], unread=unread, media_id=SPACE)


# ── the one rule that matters ────────────────────────────────────────────────
def test_verified_if_and_only_if_every_check_passes_on_fresh_evidence():
    seen = 0
    for held, reg, rooms, device, fresh, status in itertools.product(
            HELD_OUT, REGISTRATION, ROOMS, DEVICES, (True, False, None),
            ("succeeded", "failed_quality_gate")):
        cert = _certify(held, reg, rooms, device, fresh=fresh, job_status=status)
        # The fixtures produce the status they are named for.
        assert cert.checks[sc.HELD_OUT].status == held
        assert cert.checks[sc.REGISTRATION].status == reg
        assert cert.checks[sc.CONNECTIVITY].status == rooms
        assert cert.checks[sc.DEVICE].status == device
        everything = (held, reg, rooms, device) == ("passed",) * 4
        expected = everything and fresh is True and status == "succeeded"
        assert (cert.state == sc.VERIFIED) is expected, (held, reg, rooms, device, fresh, status)
        assert cert.state in sc.STATES
        seen += 1
    assert seen == 6 * 6 * 4 * 4 * 3 * 2


def test_the_required_checks_are_the_four_t12_09_names():
    assert sc.REQUIRED_CHECKS == ("held_out_quality", "registration",
                                  "room_connectivity", "device_performance")
    assert sc.STATES == ("pending", "incomplete", "degraded", "rejected", "verified")


# ── precedence: measured negatives first, then missing evidence ─────────────
@pytest.mark.parametrize("kwargs, state, reason", [
    (dict(job_found=False), "pending", "no_quality_evidence"),
    (dict(job_found=True, job_status="running"), "pending", "build_unfinished"),
    (dict(job_found=True, job_status="queued"), "pending", "build_unfinished"),
    (dict(job_found=True, job_status="failed"), "pending", "build_failed"),
    (dict(job_found=True, job_status="failed_quality_gate"), "rejected", "quality_gate_failed"),
    (dict(job_found=True, job_status="succeeded", quality={**_quality(), "blocked": True}),
     "rejected", "quality_gate_failed"),
    (dict(job_found=True, job_status="succeeded", limitations=["quality_unverified"]),
     "pending", "image_quality_not_measured"),
    (dict(job_found=True, job_status="succeeded", limitations=[]),
     "pending", "image_quality_not_measured"),
])
def test_precedence_without_measured_quality(kwargs, state, reason):
    cert = sc.certify(**kwargs)
    assert cert.state == state
    assert reason in cert.reasons


def test_a_measured_failure_is_rejected():
    assert _certify(held="failed").state == "rejected"
    assert _certify(reg="failed").state == "rejected"
    assert "registration_failed" in _certify(reg="failed").reasons


def test_parts_that_could_not_be_placed_make_it_incomplete_and_say_so():
    cert = _certify(reg="coverage_gap")
    assert cert.state == "incomplete" and cert.coverage_gap
    assert "registration_partial" in cert.reasons
    assert cert.label == "Parts of the home may be missing"


def test_disconnected_rooms_are_incomplete():
    cert = _certify(rooms="coverage_gap")
    assert cert.state == "incomplete" and cert.coverage_gap
    assert "rooms_disconnected" in cert.reasons
    one_unverified_edge = {"rooms": 3, "components": 1, "edges": 2, "verified_edges": 1,
                           "expected_rooms": 3}
    cert = sc.certify(job_found=True, job_status="succeeded", fresh=True,
                      quality={**_quality(), "room_graph": one_unverified_edge},
                      device_runs=DEVICES["passed"], media_id=SPACE)
    assert cert.state == "incomplete" and "rooms_disconnected" in cert.reasons


def _rooms(**graph):
    return sc.certify(job_found=True, job_status="succeeded", fresh=True,
                      quality={**_quality(), "room_graph": graph}, device_runs=DEVICES["passed"], media_id=SPACE)


def test_one_room_of_a_larger_home_is_partial_not_whole_home():
    """A capture of the living room alone is one connected component with no
    unverified edge; without comparing it with the home it would pass."""
    cert = _rooms(rooms=1, components=1, edges=0, verified_edges=0, expected_rooms=4)
    assert cert.checks[sc.CONNECTIVITY].status == "coverage_gap"
    assert cert.state == "incomplete" and cert.coverage_gap
    assert "rooms_disconnected" in cert.reasons
    # Fewer rooms than the home has, however well they connect.
    cert = _rooms(rooms=3, components=1, edges=2, verified_edges=2, expected_rooms=5)
    assert cert.checks[sc.CONNECTIVITY].status == "coverage_gap"


def test_whole_home_coverage_that_was_not_compared_is_not_a_pass():
    """No room count for the home to compare with: missing evidence (C6)."""
    cert = _rooms(rooms=3, components=1, edges=2, verified_edges=2)
    assert cert.checks[sc.CONNECTIVITY].status == "missing"
    assert cert.state == "incomplete" and not cert.coverage_gap
    assert "rooms_not_checked" in cert.reasons


@pytest.mark.parametrize("graph", [
    # Three rooms in one component need at least two edges.
    {"rooms": 3, "components": 1, "edges": 0, "verified_edges": 0, "expected_rooms": 3},
    {"rooms": 4, "components": 2, "edges": 1, "verified_edges": 1, "expected_rooms": 4},
    {"rooms": 3, "components": 1, "edges": 2, "verified_edges": 2, "expected_rooms": 0},
    {"rooms": 3, "components": 1, "edges": 2, "verified_edges": 2, "expected_rooms": True},
    {"rooms": 3, "components": 1, "edges": 2, "verified_edges": 2, "expected_rooms": "3"},
])
def test_an_impossible_room_graph_is_invalid(graph):
    cert = _rooms(**graph)
    assert cert.checks[sc.CONNECTIVITY].status == "invalid"
    assert cert.state != "verified"


def test_a_one_room_home_fully_captured_can_pass():
    """A studio is one room: nothing to connect, and nothing left out."""
    cert = _rooms(rooms=1, components=1, edges=0, verified_edges=0, expected_rooms=1)
    assert cert.checks[sc.CONNECTIVITY].status == "passed"
    assert cert.state == "verified"


def test_capture_the_build_could_not_read_is_never_verified():
    """A photo whose bytes failed to load is in the build's input set but was
    never used, so its measurements say nothing about it (AC12.06)."""
    cert = _certify(unread=2)
    assert cert.state == "incomplete" and not cert.coverage_gap
    assert "capture_unread" in cert.reasons
    assert cert.operator()["unread_inputs"] == 2
    # Said beside a measured problem too.
    assert "capture_unread" in _certify(reg="coverage_gap", unread=1).reasons
    assert _certify(unread=0).state == "verified"


def test_a_quality_caveat_is_degraded():
    assert _certify(held="limited").state == "degraded"
    assert _certify(reg="limited").state == "degraded"
    assert _certify(device="limited").state == "degraded"


def test_a_coverage_gap_outranks_a_caveat():
    assert _certify(held="limited", reg="coverage_gap").state == "incomplete"


def test_stale_evidence_is_pending_and_says_why():
    cert = _certify(fresh=False)
    assert cert.state == "pending"
    assert cert.reasons[0] == "evidence_stale"
    # A measured problem with the built space stays true, and so does staleness.
    stale_partial = _certify(reg="coverage_gap", fresh=False)
    assert stale_partial.state == "incomplete"
    assert {"registration_partial", "evidence_stale"} <= set(stale_partial.reasons)


def test_unknown_freshness_is_not_fresh():
    cert = _certify(fresh=None)
    assert cert.state == "incomplete" and "evidence_age_unknown" in cert.reasons


def test_good_numbers_without_connectivity_or_device_runs_are_incomplete():
    cert = _certify(rooms="missing", device="missing")
    assert cert.state == "incomplete"
    assert {"rooms_not_checked", "device_not_tested"} <= set(cert.reasons)


def test_below_the_pilot_target_is_not_a_pass():
    cert = _certify(held="acceptable")
    assert cert.checks[sc.HELD_OUT].status == "acceptable"
    assert cert.state == "incomplete" and "image_quality_below_target" in cert.reasons
    no_lpips = {**_quality(), "held_out_lpips": None}
    del no_lpips["held_out_lpips"]
    cert = sc.certify(job_found=True, job_status="succeeded", fresh=True, quality=no_lpips,
                      device_runs=DEVICES["passed"], media_id=SPACE)
    assert cert.checks[sc.HELD_OUT].status != "passed", "a missing metric is missing evidence"
    assert cert.state != "verified"


def test_limitations_are_a_floor_when_numbers_are_absent():
    """A verdict's caveat is measured evidence even when the numbers behind it
    were not kept (records from before the metrics were stored)."""
    base = dict(job_found=True, job_status="succeeded", quality={}, fresh=True)
    assert sc.certify(**base, limitations=["quality_low"]).state == "degraded"
    assert sc.certify(**base, limitations=["pose_error_high"]).state == "degraded"
    assert sc.certify(**base, limitations=["compression_loss"]).state == "degraded"
    partial = sc.certify(**base, limitations=["partial_registration"])
    assert partial.state == "incomplete" and partial.coverage_gap
    assert sc.certify(**base, limitations=["partial_registration", "quality_low"]).state == "incomplete"
    # And a caveat lowers a passing number, never the other way round.
    good = sc.certify(job_found=True, job_status="succeeded", quality=_quality(), fresh=True,
                      device_runs=DEVICES["passed"], limitations=["quality_low"], media_id=SPACE)
    assert good.state == "degraded"


# ── invalid numbers are never measurements ───────────────────────────────────
@pytest.mark.parametrize("field, value", [
    ("held_out_psnr", INF), ("held_out_psnr", -INF), ("held_out_psnr", NAN), ("held_out_psnr", -3.0),
    ("held_out_psnr", 250.0), ("held_out_ssim", 7.0), ("held_out_ssim", NAN),
    ("held_out_lpips", -0.1), ("held_out_lpips", INF), ("held_out_psnr", "28.8 dB"),
    # A boolean or a numeric string is not a measurement: true is not a 1.0.
    ("held_out_ssim", "0.95"), ("held_out_psnr", True), ("held_out_lpips", False),
])
def test_an_invalid_image_metric_is_invalid_and_never_verified(field, value):
    quality = {**_quality(), field: value}
    cert = sc.certify(job_found=True, job_status="succeeded", quality=quality, fresh=True,
                      device_runs=DEVICES["passed"], media_id=SPACE)
    assert cert.checks[sc.HELD_OUT].status == "invalid"
    assert cert.state != "verified"


@pytest.mark.parametrize("field, value", [
    ("registration_ratio", INF), ("registration_ratio", 3.5), ("registration_ratio", -0.2),
    ("mean_reprojection_error_px", -1.0), ("mean_reprojection_error_px", INF),
    ("input_images", 10), ("input_images", -5), ("input_images", NAN),
    ("registration_ratio", True), ("registration_ratio", "0.95"), ("input_images", "150"),
])
def test_an_invalid_registration_metric_is_invalid_and_never_verified(field, value):
    quality = {**_quality(), field: value}
    cert = sc.certify(job_found=True, job_status="succeeded", quality=quality, fresh=True,
                      device_runs=DEVICES["passed"], media_id=SPACE)
    assert cert.checks[sc.REGISTRATION].status == "invalid"
    assert cert.state != "verified"


def test_a_metric_the_gate_flagged_invalid_stays_invalid():
    quality = {**_quality(), "invalid": ["held_out_ssim"]}
    cert = sc.certify(job_found=True, job_status="succeeded", quality=quality, fresh=True,
                      device_runs=DEVICES["passed"], media_id=SPACE)
    assert cert.checks[sc.HELD_OUT].status == "invalid" and cert.state != "verified"


def test_unregistered_captures_never_verify():
    for reg in ("missing", "failed", "invalid", "coverage_gap"):
        assert _certify(reg=reg).state != "verified", reg


def test_emulated_or_one_platform_device_runs_do_not_count():
    emulated = [{**run, "real_device": False} for run in DEVICES["passed"]]
    assert _certify(device="missing").checks[sc.DEVICE].status == "missing"
    cert = sc.certify(job_found=True, job_status="succeeded", quality=_quality(), fresh=True,
                      device_runs=emulated, media_id=SPACE)
    assert cert.checks[sc.DEVICE].status == "missing" and cert.state != "verified"
    cert = sc.certify(job_found=True, job_status="succeeded", quality=_quality(), fresh=True,
                      device_runs=DEVICES["passed"][:1], media_id=SPACE)
    assert cert.checks[sc.DEVICE].status == "missing" and cert.state != "verified"


@pytest.mark.parametrize("fps", ["41", "41.0", True, b"41", None, [41.0], {"fps": 41.0}, -1.0, NAN])
def test_a_device_run_that_reports_no_number_is_invalid(fps):
    """A frame rate is a number an instrument reported. "41" is not one,
    however float() reads it, and true is not a frame rate (as
    recon_quality.valid_measurement for the image metrics)."""
    cert = sc.certify(job_found=True, job_status="succeeded", quality=_quality(), fresh=True,
                      media_id=SPACE,
                      device_runs=[_run("ios_safari", fps), _run("android_chrome", 34.0)])
    assert cert.checks[sc.DEVICE].status == "invalid"
    assert "ios_safari_steady_fps" not in cert.checks[sc.DEVICE].values
    assert cert.state != "verified"


def test_an_integer_frame_rate_is_a_measurement():
    cert = sc.certify(job_found=True, job_status="succeeded", quality=_quality(), fresh=True,
                      media_id=SPACE,
                      device_runs=[_run("ios_safari", 41), _run("android_chrome", 34)])
    assert cert.checks[sc.DEVICE].status == "passed" and cert.state == "verified"


def test_device_runs_count_only_for_the_space_they_measured():
    """A run measured on an older build says nothing about the space a newer
    build published: each run must name the media it measured."""
    base = dict(job_found=True, job_status="succeeded", quality=_quality(), fresh=True)
    unnamed = [{k: v for k, v in run.items() if k != "media_id"} for run in DEVICES["passed"]]
    older = [{**run, "media_id": OTHER_SPACE} for run in DEVICES["passed"]]
    blank = [{**run, "media_id": None} for run in DEVICES["passed"]]

    for runs in (unnamed, older, blank):
        cert = sc.certify(**base, media_id=SPACE, device_runs=runs)
        assert cert.checks[sc.DEVICE].status == "missing", runs
        assert cert.state != "verified" and "device_not_tested" in cert.reasons
    # Without the space's own id no run can be tied to it.
    cert = sc.certify(**base, device_runs=DEVICES["passed"])
    assert cert.checks[sc.DEVICE].status == "missing" and cert.state != "verified"
    # Another space's runs, slow or broken, neither help nor hurt this one.
    mixed = [*DEVICES["passed"], {**_run("android_chrome", 12.0), "media_id": OTHER_SPACE},
             {**_run("ios_safari", "fast"), "media_id": OTHER_SPACE}]
    cert = sc.certify(**base, media_id=SPACE, device_runs=mixed)
    assert cert.checks[sc.DEVICE].status == "passed" and cert.state == "verified"
    assert cert.checks[sc.DEVICE].values == {"ios_safari_steady_fps": 41.0,
                                             "android_chrome_steady_fps": 34.0}
    # An id is compared as an id, whichever type the reader returns it as.
    import uuid

    cert = sc.certify(**base, media_id=uuid.UUID(SPACE), device_runs=DEVICES["passed"])
    assert cert.checks[sc.DEVICE].status == "passed"


# ── what is stored today cannot reach verified ───────────────────────────────
def test_the_best_evidence_stored_today_is_not_verified():
    """No producer writes room connectivity or device runs yet, so the best a
    tour row can carry is excellent numbers on fresh evidence."""
    row = {
        "evidence_job_id": "j1", "evidence_job_status": "succeeded",
        "quality_limitations": [], "evidence_fresh": True,
        "quality_metrics": {**HELD_OUT["passed"], **REGISTRATION["passed"]},
    }
    cert = sc.from_tour_row(row)
    assert cert.state == "incomplete"
    assert {"rooms_not_checked", "device_not_tested"} <= set(cert.reasons)


def test_a_tour_row_reads_jsonb_as_text_too():
    import json

    row = {"evidence_job_id": "j1", "evidence_job_status": "succeeded",
           "quality_limitations": json.dumps(["partial_registration"]),
           "quality_metrics": json.dumps({**HELD_OUT["passed"], **REGISTRATION["coverage_gap"]}),
           "evidence_fresh": True}
    cert = sc.from_tour_row(row)
    assert cert.state == "incomplete" and cert.coverage_gap


def test_a_space_no_build_produced_is_pending():
    """A phone-scan upload (property_view_api ingest) has no job and no
    measurements: it can never be certified."""
    for row in ({}, {"quality_limitations": None, "quality_metrics": None,
                     "evidence_job_id": None, "evidence_fresh": None}):
        cert = sc.from_tour_row(row)
        assert cert.state == "pending" and cert.reasons == ("no_quality_evidence",)


def test_a_job_row_is_never_verified_because_freshness_is_not_read_there():
    row = {"status": "succeeded", "stage": "ready",
           "diagnostics": {"limitations": [], "quality": _quality()}}
    cert = sc.from_job_row(row)
    assert cert.fresh is None and cert.state != "verified"


def test_a_job_row_does_not_claim_freshness_cannot_be_told():
    """The build view never reads the property's media, so it must not say
    "we can't tell" about media the tour does compare."""
    row = {"status": "succeeded", "stage": "ready",
           "diagnostics": {"limitations": [], "quality": _quality()}}
    assert "evidence_age_unknown" not in sc.from_job_row(row).reasons
    # With every check passed it is still not verified, and says why neutrally.
    cert = sc.certify(job_found=True, job_status="succeeded", quality=_quality(), fresh=None,
                      device_runs=DEVICES["passed"], freshness_read=False, media_id=SPACE)
    assert cert.state == "incomplete"
    assert cert.reasons == ("evidence_age_not_read",)


def test_a_job_row_reports_capture_it_could_not_read():
    row = {"status": "succeeded", "stage": "ready",
           "diagnostics": {"limitations": [], "quality": _quality(),
                           "capture": {"input_media_ids": ["a", "b"], "skipped_media_ids": ["b"]}}}
    cert = sc.from_job_row(row)
    assert cert.unread == 1 and "capture_unread" in cert.reasons


def test_a_tour_row_reports_capture_the_build_could_not_read():
    row = {"evidence_job_id": "j1", "evidence_job_status": "succeeded",
           "quality_limitations": [], "evidence_fresh": True, "evidence_unread": 3,
           "quality_metrics": {**HELD_OUT["passed"], **REGISTRATION["passed"]}}
    cert = sc.from_tour_row(row)
    assert cert.unread == 3 and "capture_unread" in cert.reasons


# ── words ────────────────────────────────────────────────────────────────────
TECHNICAL = ("psnr", "ssim", "lpips", "colmap", "gaussian", "splat", "gpu", "fps", "reprojection", "%")


def test_every_reason_and_state_has_a_plain_label():
    for state in sc.STATES:
        assert sc.CERT_LABELS[state]
    for key, label in {**sc.REASON_LABELS, **sc.CERT_LABELS,
                       "coverage": sc.COVERAGE_LABEL}.items():
        for word in TECHNICAL:
            assert word not in label.lower(), (key, label)


def test_only_the_verified_label_claims_verification():
    for state, label in {**sc.CERT_LABELS, "coverage": sc.COVERAGE_LABEL}.items():
        if state == sc.VERIFIED:
            continue
        lowered = label.lower().replace("not verified", "")
        for word in ("verified", "full", "professional", "certified"):
            assert word not in lowered, (state, label)
    for key, label in sc.REASON_LABELS.items():
        lowered = label.lower().replace("not verified", "").replace("hasn't been verified", "")
        for word in ("full", "professional", "certified"):
            assert word not in lowered, (key, label)


def test_public_form_carries_no_check_detail_and_operator_form_does():
    cert = _certify(rooms="missing", device="missing")
    public = cert.public()
    assert set(public) == {"state", "label", "reasons"}
    assert all(isinstance(r, str) and r for r in public["reasons"])
    operator = cert.operator()
    assert set(operator["checks"]) == set(sc.REQUIRED_CHECKS)
    held = operator["checks"]["held_out_quality"]
    assert held["status"] == "passed"
    assert held["values"]["psnr"] == 28.8 and held["thresholds"]["target_psnr_db"] == 27.0
    assert operator["fresh"] is True


def test_the_operator_form_is_json_safe_with_invalid_numbers():
    """jsonb refuses Infinity and NaN; the operator view must still serialise."""
    import json

    quality = {**_quality(), "held_out_psnr": INF, "registration_ratio": NAN}
    cert = sc.certify(job_found=True, job_status="succeeded", quality=quality, fresh=True)
    json.dumps(cert.operator(), allow_nan=False)   # raises on a non-finite number
