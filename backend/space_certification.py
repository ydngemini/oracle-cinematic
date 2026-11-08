"""space_certification.py — may this 3D space be called verified? (T12.09)

"Ready to view" was the only state a published space had, and every reader
turned it into a quality claim: a capture with no failing caveat was labelled
as if its quality had been checked, and every captured space was described as
a "photoreal" walk through "the actual home" (audit A03). Publishing and
certifying are different questions:

* the reconstruction gate (recon_quality) decides what may be PUBLISHED, and
  blocks only results nobody would publish;
* this module decides what a published space may be CALLED. It is derived on
  every read from evidence already stored, never stored itself, so a photo
  uploaded after the build invalidates an old proof without anything being
  rewritten.

States, in the order `certify` decides them (the first that applies wins):

  1 pending     no build produced it (a phone scan), or its build is running
  2 rejected    the gate refused the build, or a check measured a failure
  3 pending     the build failed for another reason
  4 incomplete  part of the capture or of the house is known not to connect
                (a coverage gap)
  5 degraded    measured, usable, with a stated quality caveat
  6 pending     photos/video were added after it was built (stale evidence),
                or its image quality was never measured
  7 incomplete  a required check is missing, invalid or below target, the
                build could not read part of its capture, or whether the
                evidence is current cannot be told
  8 verified    every required check passed, on evidence that covers the
                newest capture media, all of it read

Measured negatives are said ahead of missing evidence: a space known to be
partial is "Partial", not merely "not verified", and staleness is listed in
its reasons beside that.

`verified` needs four checks to PASS (T12.09): held-out image quality,
registration, room connectivity and real-device tour performance. A missing
metric is missing evidence (CONTRACT C6), never a pass; an impossible one is
invalid. No producer writes room-connectivity or device-run evidence yet
(T12.06, T12.04), so with today's stored evidence `verified` is unreachable
by construction — the honest outcome until those exist, and pinned by tests.

The pass lines are the AC12.08 pilot targets — targets, not measurements, and
not calibrated on real houses. The registration lines reuse the provisional
recon_quality lines; raising them is a calibration and owner question
(docs/neoh-space-quality.md, "Certification").

Pure: no I/O, never raises on malformed input. Customer words carry no tool,
metric or file-format names.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

import recon_quality as rq

# ── states ───────────────────────────────────────────────────────────────────
PENDING = "pending"
INCOMPLETE = "incomplete"
DEGRADED = "degraded"
REJECTED = "rejected"
VERIFIED = "verified"
STATES = (PENDING, INCOMPLETE, DEGRADED, REJECTED, VERIFIED)

# ── the four required checks ─────────────────────────────────────────────────
HELD_OUT = "held_out_quality"
REGISTRATION = "registration"
CONNECTIVITY = "room_connectivity"
DEVICE = "device_performance"
REQUIRED_CHECKS = (HELD_OUT, REGISTRATION, CONNECTIVITY, DEVICE)

# ── check statuses ───────────────────────────────────────────────────────────
PASSED = "passed"              # at or beyond the verification target
ACCEPTABLE = "acceptable"      # measured, no caveat, but short of the target
LIMITED = "limited"            # measured, with a quality caveat
FAILED = "failed"              # measured, past a block line
COVERAGE_GAP = "coverage_gap"  # part of the capture or the house is not connected
MISSING = "missing"            # not measured: missing evidence, never a pass
INVALID = "invalid"            # present but impossible: a broken report

# ── verification targets (AC12.08 pilot targets; not measured on houses) ────
TARGET_PSNR_DB = 27.0
TARGET_SSIM = 0.88
TARGET_LPIPS = 0.12
#: Steady tour frame rate on a real phone (web-capture spec §7 candidate
#: target), required on both platforms below.
TARGET_TOUR_FPS = 30.0
DEVICE_PLATFORMS = ("ios_safari", "android_chrome")

_ACTIVE_JOB = frozenset({"queued", "running"})
_FAILED_JOB = frozenset({"failed", "needs_attention"})

# ── customer words ───────────────────────────────────────────────────────────
CERT_LABELS = {
    PENDING: "Quality not verified yet",
    INCOMPLETE: "Quality checks incomplete",
    DEGRADED: "Reduced quality",
    REJECTED: "Failed quality checks",
    VERIFIED: "Quality verified",
}
#: An incomplete space whose capture or rooms are known not to connect.
COVERAGE_LABEL = "Parts of the home may be missing"

#: Check names an operator reads beside each status.
CHECK_LABELS = {
    HELD_OUT: "Image quality",
    REGISTRATION: "Camera placement",
    CONNECTIVITY: "Room connections",
    DEVICE: "Phone performance",
}

#: One plain sentence per reason. Where a reason restates a published caveat
#: (space_status.LIMITATION_LABELS) the sentence is the same, so a reader that
#: lists both says it once.
REASON_LABELS = {
    "no_quality_evidence": "This space's quality wasn't measured",
    "build_unfinished": "Quality is checked once the space finishes building",
    "build_failed": "This build didn't finish, so its quality wasn't checked",
    "quality_gate_failed": "The finished space didn't match its photos closely enough",
    "evidence_stale": ("Photos or video were added after this space was built; "
                       "it hasn't been checked against them"),
    "evidence_age_unknown": ("We can't tell whether this space was built from the "
                             "latest photos and video"),
    "evidence_age_not_read": ("Whether this build covers the latest photos and video is "
                              "shown on the published space"),
    "capture_unread": "Some photos or video couldn't be read, so this space was built without them",
    "image_quality_not_measured": "Image quality wasn't measured for this space",
    "image_quality_invalid": "The image quality measurement for this space was unusable",
    "image_quality_failed": "Views differ badly from the photos",
    "image_quality_low": "Some views may look soft or blurry",
    "image_quality_below_target": "Image quality meets the minimum but not the standard for verification",
    "registration_not_measured": "How much of the capture could be placed wasn't measured",
    "registration_invalid": "The placement measurement for this capture was unusable",
    "registration_failed": "Too little of the capture could be placed",
    "registration_partial": "Parts of the capture couldn't be placed — some areas may be missing",
    "registration_imprecise": "Some views may look slightly smeared",
    "rooms_not_checked": ("Whether every room was captured and connects to the next "
                          "hasn't been checked"),
    "rooms_invalid": "The room connection record for this space was unusable",
    "rooms_disconnected": ("Some rooms or doorways are missing or aren't connected to the "
                           "rest of the space"),
    "device_not_tested": "It hasn't been tested on real phones yet",
    "device_invalid": "The phone test record for this space was unusable",
    "device_slow": "It may run slowly on some phones",
}

#: Which reason each check gives for each status (passed gives none).
_CHECK_REASONS = {
    HELD_OUT: {MISSING: "image_quality_not_measured", INVALID: "image_quality_invalid",
               FAILED: "image_quality_failed", LIMITED: "image_quality_low",
               ACCEPTABLE: "image_quality_below_target"},
    REGISTRATION: {MISSING: "registration_not_measured", INVALID: "registration_invalid",
                   FAILED: "registration_failed", COVERAGE_GAP: "registration_partial",
                   LIMITED: "registration_imprecise"},
    CONNECTIVITY: {MISSING: "rooms_not_checked", INVALID: "rooms_invalid",
                   COVERAGE_GAP: "rooms_disconnected"},
    DEVICE: {MISSING: "device_not_tested", INVALID: "device_invalid", LIMITED: "device_slow"},
}

#: A published caveat is measured evidence even when the numbers behind it
#: were not kept (records from before the metrics were stored): it sets the
#: least-severe status its check may have. It never raises a status.
_LIMITATION_FLOOR = {
    "quality_low": (HELD_OUT, LIMITED),
    "compression_loss": (HELD_OUT, LIMITED),
    "pose_error_high": (REGISTRATION, LIMITED),
    "partial_registration": (REGISTRATION, COVERAGE_GAP),
}
#: Worst first: a floor replaces a status only when it is worse.
_SEVERITY = (FAILED, COVERAGE_GAP, LIMITED, INVALID, MISSING, ACCEPTABLE, PASSED)


@dataclass(frozen=True)
class Check:
    status: str
    values: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Certification:
    state: str
    reasons: tuple[str, ...]
    checks: Mapping[str, Check]
    fresh: Optional[bool]
    #: Capture rows in the build's input set whose bytes it could not use.
    unread: int = 0

    @property
    def coverage_gap(self) -> bool:
        return any(c.status == COVERAGE_GAP for c in self.checks.values())

    @property
    def label(self) -> str:
        if self.state == INCOMPLETE and self.coverage_gap:
            return COVERAGE_LABEL
        return CERT_LABELS[self.state]

    def public(self) -> dict[str, Any]:
        """What anyone may read: the state and plain reasons. No numbers."""
        return {"state": self.state, "label": self.label,
                "reasons": [REASON_LABELS[r] for r in self.reasons]}

    def operator(self) -> dict[str, Any]:
        """Adds each check's status, the values used and the lines applied."""
        return {
            **self.public(),
            "reason_keys": list(self.reasons),
            "fresh": self.fresh,
            "unread_inputs": self.unread,
            "checks": {name: {"label": CHECK_LABELS[name], "status": check.status,
                              "values": check.values, "thresholds": check.thresholds}
                       for name, check in self.checks.items()},
        }


# ── the checks ───────────────────────────────────────────────────────────────
def _value(quality: Mapping, invalid: set[str], name: str) -> tuple[Optional[float], bool]:
    """One stored metric, re-validated: records written before the gate
    checked ranges may hold an impossible number."""
    number, bad = rq.valid_measurement(name, quality.get(name))
    return number, bad or name in invalid


def _held_out(quality: Mapping, invalid: set[str]) -> Check:
    names = {"psnr": "held_out_psnr", "ssim": "held_out_ssim", "lpips": "held_out_lpips",
             "cc_psnr": "held_out_cc_psnr", "cc_ssim": "held_out_cc_ssim"}
    values, bad = {}, False
    for short, name in names.items():
        number, broken = _value(quality, invalid, name)
        bad = bad or broken
        if number is not None:
            values[short] = number
    thresholds = {"block_psnr_db": rq.BLOCK_PSNR_DB, "block_ssim": rq.BLOCK_SSIM,
                  "soft_psnr_db": rq.SOFT_PSNR_DB, "soft_ssim": rq.SOFT_SSIM,
                  "target_psnr_db": TARGET_PSNR_DB, "target_ssim": TARGET_SSIM,
                  "target_lpips": TARGET_LPIPS}
    if bad:
        return Check(INVALID, values, thresholds)
    psnr, ssim, lpips = values.get("psnr"), values.get("ssim"), values.get("lpips")
    if psnr is None and ssim is None and lpips is None:
        return Check(MISSING, values, thresholds)
    # Blocking reads the colour-corrected figures, as the gate does.
    geo_psnr = values.get("cc_psnr", psnr)
    geo_ssim = values.get("cc_ssim", ssim)
    if (geo_psnr is not None and geo_psnr < rq.BLOCK_PSNR_DB) or (
            geo_ssim is not None and geo_ssim < rq.BLOCK_SSIM):
        return Check(FAILED, values, thresholds)
    if (psnr is not None and psnr < rq.SOFT_PSNR_DB) or (ssim is not None and ssim < rq.SOFT_SSIM):
        return Check(LIMITED, values, thresholds)
    # A pass needs all three, each on the right side of its target; the raw
    # figures are the ones a viewer sees.
    if (psnr is not None and ssim is not None and lpips is not None
            and psnr >= TARGET_PSNR_DB and ssim >= TARGET_SSIM and lpips <= TARGET_LPIPS):
        return Check(PASSED, values, thresholds)
    return Check(ACCEPTABLE, values, thresholds)


def _registration(quality: Mapping, invalid: set[str]) -> Check:
    values, bad = {}, False
    for short, name in (("ratio", "registration_ratio"),
                        ("reprojection_px", "mean_reprojection_error_px"),
                        ("input_images", "input_images")):
        number, broken = _value(quality, invalid, name)
        bad = bad or broken
        if number is not None:
            values[short] = number
    thresholds = {"block_ratio": rq.BLOCK_REGISTRATION, "partial_ratio": rq.PARTIAL_REGISTRATION,
                  "min_frames": rq.MIN_FRAMES_FOR_RATIO,
                  "soft_reprojection_px": rq.SOFT_REPROJECTION_PX,
                  "block_reprojection_px": rq.BLOCK_REPROJECTION_PX}
    if bad:
        return Check(INVALID, values, thresholds)
    if any(k not in values for k in ("ratio", "reprojection_px", "input_images")):
        return Check(MISSING, values, thresholds)
    if values["input_images"] < rq.MIN_FRAMES_FOR_RATIO:
        # Too few frames for a ratio to mean anything.
        return Check(INVALID, values, thresholds)
    if values["ratio"] < rq.BLOCK_REGISTRATION or values["reprojection_px"] > rq.BLOCK_REPROJECTION_PX:
        return Check(FAILED, values, thresholds)
    if values["ratio"] < rq.PARTIAL_REGISTRATION:
        return Check(COVERAGE_GAP, values, thresholds)
    if values["reprojection_px"] > rq.SOFT_REPROJECTION_PX:
        return Check(LIMITED, values, thresholds)
    return Check(PASSED, values, thresholds)


def _fps(value: Any) -> Optional[float]:
    """A frame rate a device run reported, or None when it reported none. As
    recon_quality.valid_measurement: a string or a boolean is not a number an
    instrument reported, however float() reads it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _connectivity(quality: Mapping) -> Check:
    """Rooms and doorway links proven by registration, compared with the
    home's own room count (T12.06): `room_graph = {rooms, components, edges,
    verified_edges, expected_rooms}`, where `expected_rooms` comes from the
    floor plan or the rooms declared for the property. One well-connected room
    is not the whole home, so without that count whole-home coverage is
    missing evidence, and fewer rooms than it is a coverage gap. Nothing
    writes the graph yet, so this is `missing` for every space today."""
    graph = quality.get("room_graph")
    thresholds = {"components": 1, "unverified_edges": 0, "uncaptured_rooms": 0}
    if graph is None:
        return Check(MISSING, {}, thresholds)
    if not isinstance(graph, Mapping):
        return Check(INVALID, {}, thresholds)
    rooms, components = _count(graph.get("rooms")), _count(graph.get("components"))
    edges, verified = _count(graph.get("edges")), _count(graph.get("verified_edges"))
    raw_expected = graph.get("expected_rooms")
    expected = _count(raw_expected)
    values = {k: v for k, v in (("rooms", rooms), ("components", components), ("edges", edges),
                                ("verified_edges", verified), ("expected_rooms", expected))
              if v is not None}
    if (None in (rooms, components, edges, verified) or rooms < 1 or components < 1
            or components > rooms or verified > edges
            # A graph of n rooms in c pieces has at least n - c links.
            or edges < rooms - components
            or (raw_expected is not None and (expected is None or expected < 1))):
        return Check(INVALID, values, thresholds)
    if components > 1 or verified < edges or (expected is not None and rooms < expected):
        return Check(COVERAGE_GAP, values, thresholds)
    if expected is None:
        return Check(MISSING, values, thresholds)
    return Check(PASSED, values, thresholds)


def _same_media(named: Any, media_id: Any) -> bool:
    if media_id is None or not isinstance(named, (str, UUID)):
        return False
    return str(named).strip().lower() == str(media_id).strip().lower()


def _device(runs: Any, media_id: Any) -> Check:
    """Tour performance measured on real phones, per platform, on THIS space:
    each run names the media (`media_id`) it measured, and a run on another
    build's space, or one that names none, says nothing about this one.
    Emulated or headless runs do not count (web-capture spec §10). No
    producer stores these yet, so this is `missing` for every space today."""
    thresholds = {"target_fps": TARGET_TOUR_FPS, "platforms": list(DEVICE_PLATFORMS)}
    if runs is None:
        return Check(MISSING, {}, thresholds)
    if not isinstance(runs, (list, tuple)):
        return Check(INVALID, {}, thresholds)
    worst: dict[str, float] = {}
    bad = False
    for run in runs:
        if not isinstance(run, Mapping) or run.get("real_device") is not True:
            continue
        if not _same_media(run.get("media_id"), media_id):
            continue
        platform = run.get("platform")
        if platform not in DEVICE_PLATFORMS:
            continue
        fps = _fps(run.get("steady_fps"))
        if fps is None:
            bad = True
            continue
        worst[platform] = min(fps, worst.get(platform, fps))
    values = {f"{p}_steady_fps": worst[p] for p in DEVICE_PLATFORMS if p in worst}
    if bad:
        return Check(INVALID, values, thresholds)
    if any(p not in worst for p in DEVICE_PLATFORMS):
        return Check(MISSING, values, thresholds)
    if min(worst.values()) < TARGET_TOUR_FPS:
        return Check(LIMITED, values, thresholds)
    return Check(PASSED, values, thresholds)


def _floor(check: Check, status: str) -> Check:
    if _SEVERITY.index(status) < _SEVERITY.index(check.status):
        return Check(status, check.values, check.thresholds)
    return check


# ── the policy ───────────────────────────────────────────────────────────────
def certify(
    *,
    job_found: bool,
    job_status: Optional[str] = None,
    limitations: Optional[Iterable[str]] = None,
    quality: Optional[Mapping[str, Any]] = None,
    fresh: Optional[bool] = None,
    device_runs: Any = None,
    media_id: Any = None,
    unread: int = 0,
    freshness_read: bool = True,
) -> Certification:
    """The certification of one space from its stored evidence.

    `job_found` — a build produced this space (its job row exists).
    `limitations` — that build's published caveats (diagnostics.limitations).
    `quality` — its recorded measurements (diagnostics.quality).
    `fresh` — True when the build's recorded input set covers every current
    capture photo/video of the property; False when newer media exist; None
    when that cannot be told. Never assumed.
    `device_runs` — real-device tour measurements; none are stored yet.
    `media_id` — the published space (property_media id) being certified. A
    device run counts only when it names this id: one measured on an older
    build's space, or naming none, is not evidence for this one. Without it
    no run counts.
    `unread` — capture rows in the build's input set whose bytes it could not
    use (diagnostics.capture.skipped_media_ids). The build's measurements say
    nothing about them, so a space with any is never verified.
    `freshness_read` — False when the caller never compared the build's input
    with the property's media (a build view), so `fresh` is None because it
    was not asked, not because it cannot be told.
    """
    quality = quality if isinstance(quality, Mapping) else {}
    unread = unread if isinstance(unread, int) and not isinstance(unread, bool) and unread > 0 else 0
    caveats = [x for x in (limitations or []) if isinstance(x, str)]
    raw_invalid = quality.get("invalid")
    invalid = {x for x in raw_invalid if isinstance(x, str)} if isinstance(raw_invalid, list) else set()

    checks = {
        HELD_OUT: _held_out(quality, invalid),
        REGISTRATION: _registration(quality, invalid),
        CONNECTIVITY: _connectivity(quality),
        DEVICE: _device(device_runs, media_id),
    }
    for caveat in caveats:
        if caveat in _LIMITATION_FLOOR:
            name, status = _LIMITATION_FLOOR[caveat]
            checks[name] = _floor(checks[name], status)

    def done(state: str, *first: str) -> Certification:
        rest = [r for name in REQUIRED_CHECKS
                for r in [_CHECK_REASONS[name].get(checks[name].status)] if r]
        if unread:
            rest.insert(0, "capture_unread")
        if fresh is False:
            rest.insert(0, "evidence_stale")
        elif (fresh is None and freshness_read and job_found
              and job_status not in _ACTIVE_JOB | _FAILED_JOB):
            rest.append("evidence_age_unknown")
        ordered = list(dict.fromkeys([*first, *rest]))
        return Certification(state, tuple(ordered), checks, fresh, unread)

    if not job_found:
        return Certification(PENDING, ("no_quality_evidence",), checks, fresh, unread)
    if job_status in _ACTIVE_JOB:
        return Certification(PENDING, ("build_unfinished",), checks, fresh, unread)
    if job_status == "failed_quality_gate" or quality.get("blocked") is True:
        return done(REJECTED, "quality_gate_failed")
    if job_status in _FAILED_JOB:
        return Certification(PENDING, ("build_failed",), checks, fresh, unread)

    statuses = {c.status for c in checks.values()}
    if FAILED in statuses:
        return done(REJECTED)
    if COVERAGE_GAP in statuses:
        return done(INCOMPLETE)
    if LIMITED in statuses:
        return done(DEGRADED)
    if fresh is False:
        return done(PENDING, "evidence_stale")
    if checks[HELD_OUT].status in (MISSING, INVALID):
        return done(PENDING)
    if statuses != {PASSED} or unread or (fresh is None and freshness_read):
        return done(INCOMPLETE)
    if fresh is not True:
        # Every check passed, but this view never compared the build's input
        # with the property's media; the published space's reading does.
        return done(INCOMPLETE, "evidence_age_not_read")
    return Certification(VERIFIED, (), checks, fresh, unread)


# ── readers ──────────────────────────────────────────────────────────────────
def _decode(value: Any) -> Any:
    """asyncpg returns jsonb as text unless a codec is set."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def from_tour_row(row: Any) -> Certification:
    """From a row of tour_api.fetch_tour_rows: the newest build that produced
    this space, whether its input set still covers the property's media, and
    how many current capture rows it could not read."""
    limitations = _decode(_get(row, "quality_limitations"))
    limitations = limitations if isinstance(limitations, list) else None
    job_found = _get(row, "evidence_job_id") is not None or limitations is not None
    return certify(
        job_found=job_found,
        job_status=_get(row, "evidence_job_status") or ("succeeded" if job_found else None),
        limitations=limitations,
        quality=_decode(_get(row, "quality_metrics")),
        fresh=_get(row, "evidence_fresh"),
        unread=_get(row, "evidence_unread") or 0,
    )


def from_job_row(row: Any) -> Certification:
    """From a reconstruction_jobs row (the build view). Freshness is not read
    here, so a build view is at best `incomplete`, never `verified`, and it
    does not claim the question cannot be answered: the published space's
    reading (from_tour_row) answers it."""
    diagnostics = _decode(_get(row, "diagnostics"))
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    limitations = diagnostics.get("limitations")
    capture = diagnostics.get("capture")
    skipped = capture.get("skipped_media_ids") if isinstance(capture, Mapping) else None
    return certify(
        job_found=True,
        job_status=_get(row, "status"),
        limitations=limitations if isinstance(limitations, list) else None,
        quality=diagnostics.get("quality"),
        fresh=None,
        unread=len(skipped) if isinstance(skipped, list) else 0,
        freshness_read=False,
    )
