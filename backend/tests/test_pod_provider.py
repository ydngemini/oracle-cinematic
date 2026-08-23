"""PodProvider: rent a GPU, run the job, and always give the GPU back.

RunPod bills a pod by the hour whether or not it is computing, and a leaked pod
is completely silent — nothing in the product surfaces one. An idle 24 GB card
left running overnight is about $5. So the invariant these tests defend is not
"the reconstruction works", it is **the pod is terminated on every path out**,
including the ones nobody plans for.

The rest covers the distinction the previous provider got wrong: RunPodProvider
validated only that env vars were shaped correctly, so it reported ready and
then failed mid-job. An empty balance is a permanent, fixable state and must be
reported as one, before anything is provisioned.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import reconstruction_providers as rp
from reconstruction_providers import (
    DELIVERY_SUFFIX,
    POD_NAME_PREFIX,
    POD_PIPELINE,
    PodProvider,
    ProviderError,
    _pod_age_seconds,
    _subsample_capture,
)


@pytest.fixture
def pod_env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RECON_POD_MIN_BALANCE_USD", "1.00")
    monkeypatch.setenv("RECON_POD_MAX_COST_USD", "2.00")


def _images(tmp_path, count=10):
    out = []
    for i in range(count):
        p = tmp_path / f"img_{i:03d}.jpg"
        p.write_bytes(b"\xff\xd8\xff" + bytes([i]) * 64)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# The invariant: the pod always goes back
# ---------------------------------------------------------------------------

def test_the_pod_is_terminated_when_the_job_raises(pod_env, monkeypatch, tmp_path):
    killed = []
    monkeypatch.setattr(PodProvider, "_terminate",
                        classmethod(lambda cls, key, pod_id: killed.append(pod_id)))

    async def fake_launch(self, settings, launched):
        launched.append("pod-abc")      # the real one records it before it can fail
        return "1.2.3.4", 22001, object(), 0.22

    async def fake_run(self, *a, **k):
        raise ProviderError("COLMAP registered no cameras")

    monkeypatch.setattr(PodProvider, "_launch", fake_launch)
    monkeypatch.setattr(PodProvider, "_run_on_pod", fake_run)

    with pytest.raises(ProviderError, match="no cameras"):
        asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert killed == ["pod-abc"], "a failed job must not leave a pod billing"


def test_the_pod_is_terminated_when_the_job_is_cancelled(pod_env, monkeypatch, tmp_path):
    """Cancellation is not an error path the code writes — it is injected from
    outside, and `finally` is the only thing that covers it."""
    killed = []
    monkeypatch.setattr(PodProvider, "_terminate",
                        classmethod(lambda cls, key, pod_id: killed.append(pod_id)))

    async def fake_launch(self, settings, launched):
        launched.append("pod-cancel")
        return "1.2.3.4", 22001, object(), 0.22

    async def fake_run(self, *a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(PodProvider, "_launch", fake_launch)
    monkeypatch.setattr(PodProvider, "_run_on_pod", fake_run)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert killed == ["pod-cancel"]


def test_a_pod_that_never_exposed_ssh_is_still_terminated(pod_env, monkeypatch, tmp_path):
    """The subtle leak: provisioning fails, so there is no connection — but the
    pod exists and is billing. Raising without carrying its id would strand it."""
    killed = []
    monkeypatch.setattr(PodProvider, "_terminate",
                        classmethod(lambda cls, key, pod_id: killed.append(pod_id)))

    calls = {"n": 0}

    def fake_rest(api_key, method, path, *, json_body=None, timeout=60):
        calls["n"] += 1
        if method == "POST":
            return {"id": "pod-stuck", "costPerHr": 0.22}
        return {"desiredStatus": "PENDING"}          # never exposes publicIp/ports

    monkeypatch.setattr(PodProvider, "_rest", staticmethod(fake_rest))
    monkeypatch.setattr(rp, "_now", _clock(step=400))
    monkeypatch.setattr(rp.asyncio, "sleep", _noop_sleep)

    with pytest.raises(ProviderError, match="never exposed SSH"):
        asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert killed == ["pod-stuck"], "a pod that never came up is still billing"


def test_a_pod_is_terminated_when_provisioning_itself_errors(pod_env, monkeypatch, tmp_path):
    """The general case behind the timeout: the pod exists, then something in
    the polling loop throws. Binding the id from _launch's return value meant
    `finally` had nothing to terminate on any of these paths."""
    killed = []
    monkeypatch.setattr(PodProvider, "_terminate",
                        classmethod(lambda cls, key, pod_id: killed.append(pod_id)))

    def fake_rest(api_key, method, path, *, json_body=None, timeout=60):
        if method == "POST":
            return {"id": "pod-boom", "costPerHr": 0.22}
        raise ProviderError("RunPod GET /pods/pod-boom failed (502)")

    monkeypatch.setattr(PodProvider, "_rest", staticmethod(fake_rest))

    with pytest.raises(ProviderError, match="502"):
        asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert killed == ["pod-boom"]


def _clock(step):
    """A monotonic clock that jumps, so provisioning windows expire without
    the test actually waiting them out."""
    state = {"t": 0.0}

    def now():
        state["t"] += step
        return state["t"]
    return now


async def _noop_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# Readiness: say what is wrong, before spending anything
# ---------------------------------------------------------------------------

def test_an_empty_balance_is_reported_as_fixable_not_as_an_outage(pod_env, monkeypatch):
    monkeypatch.setattr(PodProvider, "_balance", classmethod(lambda cls, key: -0.05))

    ready, reason = PodProvider().available()

    assert ready is False
    assert "-0.05" in reason or "$-0.05" in reason
    assert "add credits" in reason.lower(), "the reason must name the fix"


def test_a_funded_balance_is_ready(pod_env, monkeypatch):
    monkeypatch.setattr(PodProvider, "_balance", classmethod(lambda cls, key: 9.94))
    assert PodProvider().available() == (True, "")


def test_a_missing_api_key_never_reaches_the_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    def explode(*a, **k):
        raise AssertionError("availability must not call RunPod without a key")

    monkeypatch.setattr(PodProvider, "_balance", classmethod(explode))
    ready, reason = PodProvider().available()

    assert ready is False
    assert "RUNPOD_API_KEY" in reason


def test_a_rejected_key_is_not_reported_as_an_empty_balance(pod_env, monkeypatch):
    def rejected(cls, key):
        raise ProviderError("RunPod rejected the API key: unauthorized")

    monkeypatch.setattr(PodProvider, "_balance", classmethod(rejected))
    ready, reason = PodProvider().available()

    assert ready is False
    assert "rejected the API key" in reason


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------

def test_the_reaper_only_touches_our_own_pods(pod_env, monkeypatch):
    """An interactive pod the operator started by hand must never be collected
    by an automatic sweep."""
    old = int(rp.time.time()) - 6 * 3600
    listed = [
        {"id": "ours-old", "name": f"{POD_NAME_PREFIX}{old}-aaaa"},
        {"id": "ours-new", "name": f"{POD_NAME_PREFIX}{int(rp.time.time())}-bbbb"},
        {"id": "theirs", "name": "my-jupyter-box"},
        {"id": "unnamed", "name": ""},
    ]
    killed = []
    monkeypatch.setattr(PodProvider, "_rest",
                        staticmethod(lambda *a, **k: listed))
    monkeypatch.setattr(PodProvider, "_terminate",
                        classmethod(lambda cls, key, pod_id: killed.append(pod_id)))

    reaped = PodProvider.reap_stale_pods(max_age_seconds=4 * 3600)

    assert reaped == ["ours-old"]
    assert killed == ["ours-old"]


def test_an_unreadable_pod_name_is_left_alone(pod_env):
    """None means "cannot tell". Terminating on a name we cannot parse would be
    the one bug worse than leaking a pod."""
    assert _pod_age_seconds("my-jupyter-box") is None
    assert _pod_age_seconds(f"{POD_NAME_PREFIX}notanumber-x") is None
    assert _pod_age_seconds("") is None
    fresh = _pod_age_seconds(f"{POD_NAME_PREFIX}{int(rp.time.time())}-x")
    assert fresh is not None and fresh < 5


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def test_the_capture_is_thinned_evenly_rather_than_truncated(tmp_path):
    """Matching is O(n^2), so trimming is the biggest lever on price — but a
    walk-through is captured in spatial order, so keeping the first N would
    solve one room in detail and leave the rest of the house unregistered."""
    images = _images(tmp_path, 200)

    picked = _subsample_capture(images, 60)

    assert len(picked) == 60
    assert picked[0] == images[0]
    # Spread across the whole capture, not the front of it.
    assert images.index(picked[-1]) > 150
    assert len(set(picked)) == 60, "no frame is used twice"


def test_a_small_capture_is_left_alone(tmp_path):
    images = _images(tmp_path, 12)
    assert _subsample_capture(images, 60) == images


def test_the_budget_is_derived_from_the_rate_runpod_actually_charged(pod_env, monkeypatch, tmp_path):
    """A fixed assumed rate would under-bound an expensive card and over-bound a
    cheap one; the ceiling is in dollars, so it has to use the real price."""
    seen = {}

    async def capture_deadline(self, settings, host, port, key, hourly, images, work_dir):
        seen["hourly"] = hourly
        seen["max_cost"] = settings["max_cost"]
        out = work_dir / f"model{DELIVERY_SUFFIX}"
        out.write_bytes(b"SOG\x00payload")
        return out

    async def fake_launch(self, settings, launched):
        launched.append("pod-x")
        return "1.2.3.4", 22001, object(), 0.16            # A5000

    monkeypatch.setattr(PodProvider, "_launch", fake_launch)
    monkeypatch.setattr(PodProvider, "_run_on_pod", capture_deadline)
    monkeypatch.setattr(PodProvider, "_terminate", classmethod(lambda cls, k, p: None))

    asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    # $2.00 ceiling at $0.16/hr is 12.5 hours of headroom — the point is that
    # the number came from the pod, not from a constant.
    assert seen["hourly"] == 0.16
    assert seen["max_cost"] == 2.00


# ---------------------------------------------------------------------------
# The remote pipeline
# ---------------------------------------------------------------------------

def test_the_remote_pipeline_never_asks_for_an_unwritable_format():
    """The defect that silently broke every provider: splat-transform lists
    .splat input-only in every released version."""
    convert = [l for l in POD_PIPELINE.splitlines() if l.strip().startswith("splat-transform ")]
    assert convert, "the pipeline no longer converts"
    for line in convert:
        assert not line.split()[-1].endswith(".splat"), line
        assert line.split()[-1].endswith(".sog"), line


def test_the_remote_pipeline_runs_colmap_under_a_display():
    """Headless, COLMAP aborts with SIGABRT inside createPlatformIntegration()
    before reading a single image, and the failure looks like a corrupt capture
    rather than a missing display. GPU SIFT needs a real GL context, so
    QT_QPA_PLATFORM=offscreen alone is not enough."""
    for line in POD_PIPELINE.splitlines():
        stripped = line.strip()
        if stripped.startswith("$X colmap") or stripped.startswith("colmap "):
            assert stripped.startswith("$X colmap"), f"colmap without xvfb: {stripped}"
    assert "xvfb-run" in POD_PIPELINE
    assert "QT_QPA_PLATFORM=offscreen" in POD_PIPELINE


def test_the_remote_pipeline_pins_every_tool_it_installs():
    """One run was lost entirely to skew between a pip-installed gsplat and
    examples cloned from main. An unpinned install is that bug waiting."""
    assert 'gsplat==__GSPLAT__' in POD_PIPELINE
    assert 'splat-transform@__ST__' in POD_PIPELINE
