"""Learned point segmentation — optional, and never load-bearing.

Classifies each point of a gravity-aligned reconstruction as floor, ceiling,
wall or clutter, so the wall mask comes from a decision about each point rather
than from a height band that hopes furniture is short.

**The model is optional and the pipeline must work without it.** That is a
design constraint, not a convenience:

  * a model file is a deployment artifact, and a plan that only works where
    somebody remembered to ship a .onnx is a plan that silently stops working;
  * the geometric path is tested against known dimensions and is the thing that
    has been verified. The model is an improvement on it, not a replacement for
    having one.

So `segment` returns None whenever anything is missing — no runtime, no model,
a shape mismatch — and `slicing` falls back to the band. A missing model must
never raise, because the difference between "no model here" and "this capture
is unusable" matters to whoever reads the error.

**What it may and may not decide.** It classifies; it does not measure. Scale
stays with `resolve_scale_anchor`, and no output of this model can produce a
dimension. A learned classifier being wrong costs a misplaced wall, which is
visible in the plan. A learned *scale* being wrong multiplies every length and
area by a constant and looks entirely correct — which is why that is not on
offer here regardless of how well this performs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .pointfeatures import FEATURE_NAMES, LABELS, extract

log = logging.getLogger("oracle.floorplan.segmentation")

#: Where the exported graph lives. Overridable so a deployment can ship it
#: outside the package (a mounted volume, a model sidecar) without a code change.
MODEL_ENV = "ORACLE_FLOORPLAN_SEGMENTER"
DEFAULT_MODEL_PATH = Path(__file__).with_name("models") / "point_segmenter.onnx"

#: Below this the point's class is not asserted — it falls back to whatever the
#: geometric path would have said. A model that is unsure about a point should
#: not out-vote a measurement.
MIN_CONFIDENCE = 0.55

_SESSION = None
_SESSION_PATH: Optional[str] = None


def model_path() -> Optional[Path]:
    override = (os.environ.get(MODEL_ENV) or "").strip()
    candidate = Path(override) if override else DEFAULT_MODEL_PATH
    return candidate if candidate.is_file() else None


def available() -> tuple[bool, str]:
    """(ready, reason-if-not), matching every other provider seam in this repo.

    The reason is worth returning even though nothing 503s on it: "no segmenter
    is installed" and "onnxruntime is missing" call for different actions, and
    a caller that logs one message for both teaches nobody anything.
    """
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return (False, "onnxruntime is not installed")
    if model_path() is None:
        # Name the path actually consulted. Reporting DEFAULT_MODEL_PATH while
        # an override is set sends the reader to inspect a file that was never
        # going to be loaded.
        override = (os.environ.get(MODEL_ENV) or "").strip()
        looked_at = override or DEFAULT_MODEL_PATH
        suffix = "" if override else f" (set {MODEL_ENV} to override)"
        return (False, f"no point segmenter at {looked_at}{suffix}")
    return (True, "")


def _session():
    """Cached inference session. Rebuilt only when the path changes."""
    global _SESSION, _SESSION_PATH

    path = model_path()
    if path is None:
        return None
    if _SESSION is not None and _SESSION_PATH == str(path):
        return _SESSION

    import onnxruntime

    # Single-threaded on purpose: this runs inside a request-handling process
    # that already has its own concurrency, and letting ORT spawn a thread pool
    # per call turns a millisecond of inference into contention.
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    _SESSION = onnxruntime.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    _SESSION_PATH = str(path)
    log.info("Loaded floor-plan point segmenter from %s", path)
    return _SESSION


def reset_cache() -> None:
    """Drop the cached session. For tests that swap the model path."""
    global _SESSION, _SESSION_PATH
    _SESSION, _SESSION_PATH = None, None


def segment(xyz, up, *, floor: float, ceiling: Optional[float] = None):
    """Per-point labels, or None when no usable model is installed.

    None is a normal outcome and the caller is expected to handle it. Raising
    would make an optional accelerator into a hard dependency, and the whole
    point of this module is that the plan is derivable without it.
    """
    try:
        # Inside the guard: a corrupt or unreadable graph raises HERE, and this
        # module's whole promise is that a broken accelerator does not take the
        # plan with it. Building the session outside the try made that promise
        # false for the failure most likely to occur in a deployment.
        session = _session()
        if session is None:
            return None

        import numpy as np

        features = extract(xyz, up, floor=floor, ceiling=ceiling)

        # Check the width the GRAPH expects, not the width `extract` just
        # stacked. Comparing `features.shape[1]` to `len(FEATURE_NAMES)` was a
        # branch that can never be taken — `extract` builds its output from that
        # same constant — while the mismatch this module exists to catch went
        # unchecked. The feature count went 7 -> 10 on this branch, so a
        # deployment pointing ORACLE_FLOORPLAN_SEGMENTER at a mounted v1 model
        # (the documented reason the override exists) fed a 10-wide tensor to a
        # 7-input graph, onnxruntime raised inside `session.run`, and the
        # operator saw a generic "Point segmentation failed" that named neither
        # the version nor the width.
        spec = session.get_inputs()[0]
        expected = spec.shape[-1] if spec.shape else None
        if isinstance(expected, int) and expected != features.shape[1]:
            log.warning(
                "Segmenter at %s expects %d features per point; this build computes "
                "%d (%s). The model is a different version — ignoring it.",
                _SESSION_PATH, expected, features.shape[1], ", ".join(FEATURE_NAMES),
            )
            return None

        name = spec.name
        logits = session.run(None, {name: features})[0]
        if logits.shape[0] != features.shape[0] or logits.shape[1] != len(LABELS):
            log.warning(
                "Segmenter returned %s for %d points and %d classes; ignoring it.",
                logits.shape, features.shape[0], len(LABELS),
            )
            return None

        # Softmax, then refuse to assert a class the model is not sure of.
        shifted = logits - logits.max(axis=1, keepdims=True)
        probs = np.exp(shifted)
        probs /= probs.sum(axis=1, keepdims=True)

        labels = probs.argmax(axis=1).astype("int8")
        labels[probs.max(axis=1) < MIN_CONFIDENCE] = -1
        return labels
    except Exception:  # noqa: BLE001
        # An accelerator that fails must not take the plan with it.
        log.exception("Point segmentation failed; falling back to geometry")
        return None
