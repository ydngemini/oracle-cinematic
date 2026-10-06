"""space_status.py — the one state machine a Neoh Space build moves through.

A reconstruction job has two vocabularies and they must never be confused:

* **status** (legacy, coarse, operator-facing): queued / running / succeeded /
  failed / failed_quality_gate / needs_attention. The worker claims on it and
  the reaper/reconciliation sweeps key off it. Kept for compatibility.
* **stage** (canonical, customer-facing): the explicit states below. This is
  what a person waiting on their space sees, mapped to plain product language.

Progress is honest by construction: no provider in this pipeline reports a
real percentage, so none is shown. A stage is a fact; "83%" would be a guess.

Nothing here ever names a GPU provider, a pod, COLMAP, Gaussians or a file
format. `public_view()` is the only shape a non-operator receives; operators
read `operator_view()`, which adds diagnostics, provider and raw error text.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

# ── the states ───────────────────────────────────────────────────────────────
UPLOADING = "uploading"          # client-side only: files still going up
QUEUED = "queued"
PREPARING = "preparing"          # gathering source media, quality gate, frame selection
RECONSTRUCTING = "reconstructing"
CONVERTING = "converting"        # delivery format
ANALYZING = "analyzing"          # orientation, scale, entry view, floor plan, diagnostics
READY = "ready"
NEEDS_ATTENTION = "needs_attention"
FAILED = "failed"

STAGES = (UPLOADING, QUEUED, PREPARING, RECONSTRUCTING, CONVERTING, ANALYZING,
          READY, NEEDS_ATTENTION, FAILED)
TERMINAL = frozenset({READY, NEEDS_ATTENTION, FAILED})
ACTIVE = frozenset({QUEUED, PREPARING, RECONSTRUCTING, CONVERTING, ANALYZING})

#: Legal transitions. Anything else is a bug in the caller, and `advance()`
#: refuses it rather than letting a job jump from ready back to building.
TRANSITIONS: dict[str, frozenset[str]] = {
    UPLOADING: frozenset({QUEUED, FAILED}),
    QUEUED: frozenset({PREPARING, NEEDS_ATTENTION, FAILED}),
    PREPARING: frozenset({RECONSTRUCTING, CONVERTING, NEEDS_ATTENTION, FAILED, QUEUED}),
    RECONSTRUCTING: frozenset({CONVERTING, NEEDS_ATTENTION, FAILED, QUEUED}),
    CONVERTING: frozenset({ANALYZING, NEEDS_ATTENTION, FAILED, QUEUED}),
    ANALYZING: frozenset({READY, NEEDS_ATTENTION, FAILED, QUEUED}),
    READY: frozenset(),
    NEEDS_ATTENTION: frozenset(),
    FAILED: frozenset(),
}

#: The coarse status each stage implies, so the two columns cannot disagree.
STATUS_FOR_STAGE = {
    QUEUED: "queued",
    PREPARING: "running",
    RECONSTRUCTING: "running",
    CONVERTING: "running",
    ANALYZING: "running",
    READY: "succeeded",
    NEEDS_ATTENTION: "needs_attention",
    FAILED: "failed",
}

# ── customer language ────────────────────────────────────────────────────────
LABELS = {
    UPLOADING: ("Uploading", "Sending your capture. Keep this page open until the upload finishes."),
    QUEUED: ("Waiting to start", "Your capture is safely stored and in line to be processed."),
    PREPARING: ("Processing capture", "Checking the capture and choosing the clearest frames."),
    RECONSTRUCTING: ("Building your space", "This is the long step — often 15–40 minutes. You can leave this page; it keeps going."),
    CONVERTING: ("Preparing walkthrough", "Packaging the space so it opens quickly on phones and computers."),
    ANALYZING: ("Finishing touches", "Setting the starting view and checking for a floor plan."),
    READY: ("Ready", "Your space is ready to walk through."),
    NEEDS_ATTENTION: ("Needs attention", "This space needs another look before it can be shown."),
    FAILED: ("Couldn't build this space", "Your original photos and video are kept. You can try again or recapture."),
}

#: Ordered steps shown as a stage list (never as a percentage).
STEP_ORDER = (QUEUED, PREPARING, RECONSTRUCTING, CONVERTING, ANALYZING, READY)

#: Plain explanations for terminal outcomes, keyed by failure category. The
#: raw provider error stays in the operator view.
FAILURE_MESSAGES = {
    "quality_gate": "This capture can't make a usable space yet. See how to improve it below.",
    "conversion": "The space was built but couldn't be packaged for viewing. It can be retried without rebuilding.",
    "provider": "We couldn't finish building this space. Your photos and video are safe — try again.",
    "orphaned": "Processing was interrupted. Your photos and video are safe — try again.",
    "stalled": "Processing stopped responding. Your photos and video are safe — try again.",
    "storage": "The space was built but couldn't be saved. Try again.",
    "cost_guard": "This space was rebuilt too many times today. Try again tomorrow or ask an admin.",
}

#: Customer caveats for a READY space that is usable but limited. Every one of
#: these is a true, specific statement — never a score.
LIMITATION_LABELS = {
    "camera_poses_missing": "Starting view and floor plan unavailable for this capture",
    "orientation_uncertain": "Orientation may be slightly off",
    "scale_unknown": "Measurements unavailable — real-world scale unknown",
    "floorplan_unavailable": "Floor plan unavailable",
    "entry_view_default": "Opens on an overview instead of a room view",
    "floaters_present": "Some stray artefacts outside the rooms (windows, edges)",
    # From the post-reconstruction quality check (recon_quality.py).
    "quality_unverified": "Image quality wasn't measured for this space",
    "quality_low": "Some views may look soft or blurry",
    "partial_registration": "Parts of the capture couldn't be placed — some areas may be missing",
    "pose_error_high": "Some views may look slightly smeared",
    "compression_loss": "Fine detail is softened by compression",
}


class IllegalTransition(ValueError):
    pass


def check_transition(current: Optional[str], nxt: str) -> None:
    """Raise when `current -> nxt` is not a legal move."""
    if nxt not in STAGES:
        raise IllegalTransition(f"unknown stage {nxt!r}")
    if current is None or current == nxt:
        return
    allowed = TRANSITIONS.get(current, frozenset())
    if nxt not in allowed:
        raise IllegalTransition(f"{current} -> {nxt} is not a legal transition")


def stage_of(row: Mapping[str, Any]) -> str:
    """The canonical stage for a job row, deriving it for rows written before
    the column existed (so old jobs read correctly, not as 'unknown')."""
    stage = _get(row, "stage")
    status = _get(row, "status")
    # `status` is authoritative whenever it is not `running`: sweeps outside
    # the worker (reconciliation's stalled-job pass, an operator) move status
    # alone, and a stale stage must never keep claiming "building" over a job
    # that has failed. Only a running job's sub-stage comes from `stage`.
    if status == "running":
        return stage if stage in ACTIVE and stage != QUEUED else RECONSTRUCTING
    derived = {
        "queued": QUEUED,
        "succeeded": READY,
        "failed": FAILED,
        "failed_quality_gate": FAILED,
        "needs_attention": NEEDS_ATTENTION,
    }.get(status or "")
    if derived is not None:
        return derived
    return stage if stage in STAGES else FAILED


def _get(row: Mapping[str, Any], key: str, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def failure_category(row: Mapping[str, Any]) -> Optional[str]:
    if _get(row, "quality_gate") or _get(row, "status") == "failed_quality_gate":
        return "quality_gate"
    return _get(row, "failure_category")


def public_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """What a customer sees about one build. No provider, no percent, no
    toolchain words, no raw error text."""
    stage = stage_of(row)
    label, message = LABELS[stage]
    diagnostics = _json(_get(row, "diagnostics"))
    category = failure_category(row)
    if stage in (FAILED, NEEDS_ATTENTION) and category in FAILURE_MESSAGES:
        message = FAILURE_MESSAGES[category]

    gate = diagnostics.get("quality_gate") if isinstance(diagnostics.get("quality_gate"), dict) else {}
    guidance = list(gate.get("guidance") or []) if stage in (FAILED, NEEDS_ATTENTION) else []
    warnings = [w for w in (diagnostics.get("warnings") or []) if isinstance(w, str)]
    limitations = [w for w in (diagnostics.get("limitations") or []) if isinstance(w, str)]

    from capture_quality import WARNING_LABELS

    caveats = [WARNING_LABELS[w] for w in warnings if w in WARNING_LABELS]
    caveats += [LIMITATION_LABELS[x] for x in limitations if x in LIMITATION_LABELS]

    can_retry = stage in (FAILED, NEEDS_ATTENTION) and category != "cost_guard"
    retry_kind = None
    if can_retry:
        retry_kind = "conversion" if _get(row, "raw_output_key") else (
            "recapture" if category == "quality_gate" else "full")

    floorplan = diagnostics.get("floorplan") if isinstance(diagnostics.get("floorplan"), dict) else {}
    steps = []
    reached = STEP_ORDER.index(stage) if stage in STEP_ORDER else -1
    for i, step in enumerate(STEP_ORDER):
        steps.append({
            "stage": step,
            "label": LABELS[step][0],
            "state": ("done" if (reached >= 0 and i < reached) or (stage == READY and step == READY)
                      else "current" if i == reached else "pending"),
        })

    return {
        "job_id": str(_get(row, "id", "")),
        "state": stage,
        "label": label,
        "message": message,
        "active": stage in ACTIVE,
        "terminal": stage in TERMINAL,
        "steps": steps,
        "guidance": guidance[:4],
        "caveats": caveats,
        "can_retry": can_retry,
        "retry_kind": retry_kind,
        "floorplan": (
            "available" if floorplan.get("status") == "derived"
            else "unavailable" if floorplan.get("status") in ("unavailable", "refused", "failed")
            else None
        ),
        "attempt": int(_get(row, "attempts", 0) or 0),
        "created_at": _iso(_get(row, "created_at")),
        "updated_at": _iso(_get(row, "updated_at")),
    }


def operator_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Everything an operator needs to diagnose a build. Not for customers."""
    out = public_view(row)
    out.update({
        "status": _get(row, "status"),
        "provider": _get(row, "provider"),
        "provider_job_id": _get(row, "provider_job_id"),
        "error": _get(row, "error"),
        "failure_category": failure_category(row),
        "quality_gate": _get(row, "quality_gate"),
        "pipeline_version": _get(row, "pipeline_version"),
        "cost_estimate_usd": _num(_get(row, "cost_estimate_usd")),
        "gpu_seconds": _num(_get(row, "gpu_seconds")),
        "output_bytes": _get(row, "output_bytes"),
        "raw_output_key": _get(row, "raw_output_key"),
        "retry_of": str(_get(row, "retry_of")) if _get(row, "retry_of") else None,
        "diagnostics": _json(_get(row, "diagnostics")),
    })
    return out


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
