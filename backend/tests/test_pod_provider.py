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
    QT_QPA_PLATFORM=offscreen alone is not enough.

    The display is now a server we start ourselves rather than the `xvfb-run`
    wrapper: on the pod image that wrapper dies with
    "/usr/bin/xvfb-run: 184: 0: not found" before COLMAP is reached, which is
    how the first live run failed. So this asserts the property — a display
    exists, and it is established before the first colmap call — instead of the
    mechanism that used to provide it.
    """
    lines = [line.strip() for line in POD_PIPELINE.splitlines()]
    display_at = next(
        i for i, line in enumerate(lines) if line.startswith("export DISPLAY=")
    )
    colmap_at = next(i for i, line in enumerate(lines) if line.startswith("colmap "))
    assert display_at < colmap_at, "colmap runs before a display exists"

    assert "Xvfb :99" in POD_PIPELINE
    # Waited for, not assumed: the server takes a moment to create its socket
    # and COLMAP would race it.
    assert "/tmp/.X11-unix/X99" in POD_PIPELINE
    assert "QT_QPA_PLATFORM=offscreen" in POD_PIPELINE


def test_the_remote_pipeline_pins_every_tool_it_installs():
    """One run was lost entirely to skew between a pip-installed gsplat and
    examples cloned from main. An unpinned install is that bug waiting."""
    assert 'gsplat==__GSPLAT__' in POD_PIPELINE
    assert 'splat-transform@__ST__' in POD_PIPELINE


# ---------------------------------------------------------------------------
# Transport 2: object storage, for deploys with no SSH egress
# ---------------------------------------------------------------------------

class _FakeStorage:
    """Records what was staged and hands out capability URLs."""

    BACKEND = "azure-blob"

    def __init__(self, *, output_after=0):
        self.put_keys: list[str] = []
        self.read_urls: list[str] = []
        self.write_urls: list[str] = []
        self._polls = 0
        self._output_after = output_after

    def is_configured(self):
        return True

    def put_file(self, key, path, content_type):
        self.put_keys.append(key)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.put_keys.append(key)

    def signed_url(self, key, ttl):
        url = f"https://blob.test/{key}?sig=read&se={ttl}"
        self.read_urls.append(url)
        return url

    def presigned_put_url(self, key, ttl):
        url = f"https://blob.test/{key}?sig=write&se={ttl}"
        self.write_urls.append(url)
        return url

    def get_bytes(self, key):
        self._polls += 1
        if self._polls <= self._output_after:
            raise FileNotFoundError(key)
        return b"SOG\x00compressed-payload"


def _blob_env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RECON_POD_TRANSPORT", "blob")


def test_the_blob_transport_never_connects_to_the_pod(monkeypatch, tmp_path):
    """The whole point of the fallback: no outbound 22, so nothing ever opens a
    connection to the rented machine."""
    _blob_env(monkeypatch)
    storage = _FakeStorage()
    monkeypatch.setitem(__import__("sys").modules, "object_storage", storage)

    def no_ssh(*a, **k):
        raise AssertionError("the blob transport must not open SSH")

    monkeypatch.setattr(PodProvider, "_launch", no_ssh)
    monkeypatch.setattr(PodProvider, "_terminate", classmethod(lambda cls, k, p: None))

    created = {}

    def fake_rest(api_key, method, path, *, json_body=None, timeout=60):
        if method == "POST":
            created.update(json_body)
            return {"id": "pod-blob", "costPerHr": 0.16}
        return {}

    monkeypatch.setattr(PodProvider, "_rest", staticmethod(fake_rest))
    monkeypatch.setattr(rp.asyncio, "sleep", _noop_sleep)

    out = asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert out.name == f"model{DELIVERY_SUFFIX}"
    assert created["dockerStartCmd"], "the pod must start the job itself"
    # The pipeline and bootstrap are fetched, not embedded: a hundred lines of
    # shell in argv is a truncation bug waiting to happen.
    assert len(" ".join(created["dockerStartCmd"])) < 400


def test_the_pod_receives_capability_urls_and_no_credentials(monkeypatch, tmp_path):
    """We never talk to this machine again, so what it holds is all it can do."""
    _blob_env(monkeypatch)
    storage = _FakeStorage()
    monkeypatch.setitem(__import__("sys").modules, "object_storage", storage)
    monkeypatch.setattr(PodProvider, "_terminate", classmethod(lambda cls, k, p: None))

    created = {}

    def fake_rest(api_key, method, path, *, json_body=None, timeout=60):
        if method == "POST":
            created.update(json_body)
            return {"id": "pod-blob", "costPerHr": 0.16}
        return {}

    monkeypatch.setattr(PodProvider, "_rest", staticmethod(fake_rest))
    monkeypatch.setattr(rp.asyncio, "sleep", _noop_sleep)
    asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    env = created["env"]
    blob = " ".join(str(v) for v in env.values())
    for secret in ("RUNPOD_API_KEY", "test-key", "AZURE_STORAGE_CONNECTION_STRING"):
        assert secret not in blob, f"{secret} must never reach the pod"
    # Write capabilities exist ONLY for this job's own output keys.
    #
    # This used to assert a count of one, which stopped being the invariant when
    # the pod started returning camera poses as well. The count was never the
    # point: what matters is that the pod cannot write anywhere it was not
    # explicitly granted, so every URL it holds is checked against the exact set
    # of keys this job owns.
    allowed = {f"model{DELIVERY_SUFFIX}", "cameras.json", "points.ply"}
    assert storage.write_urls, "the pod must be able to return its result"
    prefixes = set()
    for url in storage.write_urls:
        path_part = url.split("?")[0]
        prefix, _, key = path_part.rpartition("/")
        assert key in allowed, f"write capability for an unexpected key: {key}"
        assert "recon-outputs/" in prefix, f"write escapes the output area: {url}"
        assert "sig=write" in url
        prefixes.add(prefix)
    assert len(prefixes) == 1, f"writes span more than one job prefix: {prefixes}"
    assert any(f"model{DELIVERY_SUFFIX}" in url for url in storage.write_urls)


def test_the_finished_artifact_is_the_completion_signal(monkeypatch, tmp_path):
    """Nothing reports progress in this direction, so the job is done when the
    blob shows up — not when the pod says so."""
    _blob_env(monkeypatch)
    storage = _FakeStorage(output_after=3)   # missing three times, then present
    monkeypatch.setitem(__import__("sys").modules, "object_storage", storage)
    monkeypatch.setattr(PodProvider, "_terminate", classmethod(lambda cls, k, p: None))
    monkeypatch.setattr(PodProvider, "_rest", staticmethod(
        lambda *a, **k: {"id": "pod-blob", "costPerHr": 0.16}))
    monkeypatch.setattr(rp.asyncio, "sleep", _noop_sleep)

    out = asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert out.read_bytes().startswith(b"SOG")


def test_the_pod_is_terminated_on_the_blob_path_too(monkeypatch, tmp_path):
    _blob_env(monkeypatch)
    storage = _FakeStorage(output_after=10_000)     # never appears
    monkeypatch.setitem(__import__("sys").modules, "object_storage", storage)
    killed = []
    monkeypatch.setattr(PodProvider, "_terminate",
                        classmethod(lambda cls, key, pod_id: killed.append(pod_id)))
    monkeypatch.setattr(PodProvider, "_rest", staticmethod(
        lambda *a, **k: {"id": "pod-blob", "costPerHr": 0.16}))
    monkeypatch.setattr(rp, "_now", _clock(step=4000))
    monkeypatch.setattr(rp.asyncio, "sleep", _noop_sleep)

    with pytest.raises(ProviderError, match="no artifact"):
        asyncio.run(PodProvider().reconstruct(_images(tmp_path), tmp_path))

    assert killed == ["pod-blob"]


def test_a_backend_that_cannot_hand_out_a_url_is_refused_up_front(monkeypatch):
    """azure-files has no URL to give a pod, so a job would compute and then have
    nowhere to put the result — which looks like a job that silently never
    finished rather than a configuration mistake."""
    _blob_env(monkeypatch)
    storage = _FakeStorage()
    storage.BACKEND = "azure-files"
    monkeypatch.setitem(__import__("sys").modules, "object_storage", storage)
    monkeypatch.setattr(PodProvider, "_balance", classmethod(lambda cls, key: 9.94))

    ready, reason = PodProvider().available()

    assert ready is False
    assert "azure-files" in reason and "ssh" in reason


def test_the_ssh_transport_still_needs_asyncssh_and_says_so(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RECON_POD_TRANSPORT", "ssh")
    monkeypatch.setitem(__import__("sys").modules, "asyncssh", None)
    monkeypatch.setattr(PodProvider, "_balance", classmethod(lambda cls, key: 9.94))

    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "asyncssh":
            raise ImportError("no asyncssh")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    ready, reason = PodProvider().available()

    assert ready is False
    assert "asyncssh" in reason
    assert "blob" in reason, "the reason should name the fallback"


def test_an_unknown_transport_is_refused(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RECON_POD_TRANSPORT", "carrier-pigeon")

    ready, reason = PodProvider().available()

    assert ready is False and "ssh" in reason and "blob" in reason


def test_the_upload_is_bounded_by_the_budget_too(pod_env, monkeypatch, tmp_path):
    """Only the training run used to be under a timeout.

    RunPod hands out machines whose network is unusable — one took the capture
    at 17 KB/s, an upload that would have billed for over an hour before the
    four-hour reaper noticed. Every phase now draws from one clock, and staging
    gets a slice of it rather than all of it, because a pod that cannot receive
    60 images will not train on them either.
    """
    import sys
    import types

    class _StalledSftp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, *a, **k):
            await asyncio.sleep(3600)          # the dud machine

        def open(self, *a, **k):
            raise AssertionError("staging never gets this far")

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, *a, **k):
            return types.SimpleNamespace(exit_status=0, stdout="", stderr="")

        def start_sftp_client(self):
            return _StalledSftp()

    async def _connect(*a, **k):
        return _Conn()

    stub = types.SimpleNamespace(connect=_connect)
    monkeypatch.setitem(sys.modules, "asyncssh", stub)
    # A tiny ceiling, so the test does not wait out a real budget.
    monkeypatch.setenv("RECON_POD_TIMEOUT", "600")
    monkeypatch.setattr(
        "reconstruction_providers.POD_UPLOAD_BUDGET_SHARE", 0.0, raising=False
    )
    monkeypatch.setattr(
        "reconstruction_providers.POD_UPLOAD_MIN_SECONDS", 0.2, raising=False
    )

    provider = PodProvider()
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider._run_on_pod(
            PodProvider._settings(), "1.2.3.4", 22, object(), 0.74,
            _images(tmp_path, 3), tmp_path,
        ))

    message = str(caught.value)
    assert "could not take the capture" in message
    assert "different machine" in message, "the operator needs to know a retry helps"


def test_the_trainer_is_asked_for_the_point_cloud_it_must_produce():
    """`--save-steps` writes .pt checkpoints; the PLY is a separate opt-in.

    Without `--save-ply` the trainer runs to completion, reports its metrics,
    renders its trajectory video and exits 0 — having written nothing the
    converter can read. That failure is invisible until the last line of the
    job, which is the most expensive place to discover anything.
    """
    train = [l for l in POD_PIPELINE.splitlines() if "simple_trainer.py" in l]
    assert train, "the pipeline no longer trains"
    command = POD_PIPELINE[POD_PIPELINE.index("simple_trainer.py"):]
    command = command.split("cd /workspace")[0]

    assert "--save-ply" in command, "the trainer will not write a point cloud"
    assert "--ply-steps" in command, "the PLY must be written at the step we stop on"
    # And the step it writes at has to be the step we stop at, or the file is
    # from the middle of training.
    assert command.count("__STEPS__") >= 3


def test_a_network_floor_is_asked_for_but_stays_out_of_the_way(pod_env, monkeypatch):
    """RunPod can filter on advertised link speed at placement.

    Kept low on purpose. The machine that took a capture at 17 KB/s never had
    its advertised speed observed, so there is no evidence a high floor would
    have excluded it — while a high floor demonstrably narrows placement, which
    is a failure that HAS been seen. At zero the fields are omitted entirely, so
    an operator who turns it off gets the widest pool rather than a filter that
    silently still applies.
    """
    monkeypatch.setenv("RECON_POD_MIN_MBPS", "25")
    assert PodProvider._settings()["min_mbps"] == 25

    monkeypatch.setenv("RECON_POD_MIN_MBPS", "0")
    assert PodProvider._settings()["min_mbps"] == 0

    sent = {}

    def _rest(api_key, method, path, *, json_body=None, timeout=60):
        if method == "POST" and path == "/pods":
            sent.update(json_body or {})
            return {"id": "pod-net", "costPerHr": 0.5}
        return {"publicIp": "1.2.3.4", "portMappings": {"22": 9000}, "costPerHr": 0.5}

    monkeypatch.setattr(PodProvider, "_rest", staticmethod(_rest))
    import types
    monkeypatch.setitem(
        __import__("sys").modules, "asyncssh",
        types.SimpleNamespace(generate_private_key=lambda kind: types.SimpleNamespace(
            export_public_key=lambda: b"ssh-ed25519 AAAA")),
    )
    asyncio.run(PodProvider()._launch(PodProvider._settings(), []))

    assert "minDownloadMbps" not in sent, "a disabled filter must not be sent"
    assert "minUploadMbps" not in sent
