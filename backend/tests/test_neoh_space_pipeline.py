"""Neoh Space pipeline, end to end on synthetic inputs (no GPU, no database).

Drives `reconstruction_worker._process` with a recording database, an
in-memory object store, a fake provider and real (tiny, generated) JPEGs, and
checks the things a customer and an operator depend on:

* the capture quality gate refuses an obviously bad capture BEFORE a provider
  is called, and says how to recapture;
* stages advance in order and never claim a percentage;
* the provider's job id and cost are recorded;
* publish is atomic: insert + supersede + ready in ONE transaction;
* a conversion failure preserves the raw output and needs no new GPU run;
* a resume-from-conversion job never calls the provider;
* camera poses travel through conversion and their absence is stated, not
  hidden behind READY.
"""
from __future__ import annotations

import asyncio
import io
import json
import types
from pathlib import Path

import numpy as np
import pytest

import capture_quality
import capture_sidecars
import reconstruction_worker as worker
import space_status
from reconstruction_providers import ProviderError
from tests.test_floorplan_slicing import _ply
from tests.test_scene_manifest import _cameras, _room

TENANT = "00000000-0000-0000-0000-0000000000aa"
LEAD = "11111111-1111-1111-1111-111111111111"
JOB = "22222222-2222-2222-2222-222222222222"


# ── fakes ────────────────────────────────────────────────────────────────────
class _Conn:
    def __init__(self, db, tx_id):
        self.db, self.tx_id = db, tx_id

    async def execute(self, sql, *args):
        self.db.calls.append((self.tx_id, " ".join(sql.split()), args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        self.db.calls.append((self.tx_id, " ".join(sql.split()), args))
        return {"id": "33333333-3333-3333-3333-333333333333"}

    async def fetchval(self, sql, *args):
        self.db.calls.append((self.tx_id, " ".join(sql.split()), args))
        return 3

    async def fetch(self, sql, *args):
        self.db.calls.append((self.tx_id, " ".join(sql.split()), args))
        return []


class _Db:
    def __init__(self):
        self.calls = []
        self.tx = 0

    def __call__(self, ctx):
        db = self

        class _Tx:
            async def __aenter__(self_inner):
                db.tx += 1
                return _Conn(db, db.tx)

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    def stages(self):
        out = []
        for _, sql, args in self.calls:
            if sql.startswith("UPDATE reconstruction_jobs SET status = $2") and "stage = $3" in sql:
                out.append(args[2])
        return out

    def find(self, needle):
        return [(tx, sql, args) for tx, sql, args in self.calls if needle in sql]


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_file(self, key, path, content_type):
        self.objects[key] = Path(path).read_bytes()
        return key

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = bytes(data)
        return key

    def get_bytes(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


def _jpeg(path: Path, seed: int, *, blur=False, dark=False, size=(960, 720)):
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, size=(size[1] // 8, size[0] // 8, 3), dtype=np.uint8)
    img = Image.fromarray(arr).resize(size, Image.NEAREST)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(18))
    if dark:
        img = Image.eval(img, lambda v: v // 20)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    path.write_bytes(buf.getvalue())
    return path


class _Provider:
    name = "fake_gpu"
    produces = "captured"

    def __init__(self, *, output=".sog", with_points=True, with_poses=True):
        self.calls = 0
        self.output = output
        self.with_points = with_points
        self.with_poses = with_poses
        self.on_submitted = None
        self.last_metrics = None

    def available(self):
        return True, ""

    async def reconstruct(self, images, work_dir):
        self.calls += 1
        if self.on_submitted:
            self.on_submitted("pod-xyz")
        out = work_dir / f"model{self.output}"
        out.write_bytes(b"PK\x03\x04" + b"0" * 4096)
        if self.with_points:
            capture_sidecars.points_sidecar_for(out).write_bytes(_ply(_room()))
        if self.with_poses:
            capture_sidecars.write(out, _cameras().tolist(), frame=capture_sidecars.FRAME_TRAINED)
        self.last_metrics = {"provider_job_id": "pod-xyz",
                             "cost": {"estimated_usd": 0.4321, "billed_seconds_estimate": 900.0}}
        return out


@pytest.fixture
def env(monkeypatch, tmp_path):
    db, store = _Db(), _Store()
    monkeypatch.setattr(worker, "tenant_tx", db)
    import media_storage
    import object_storage

    monkeypatch.setattr(media_storage, "storage_available", lambda: True)
    for name in ("put_file", "put_bytes", "get_bytes"):
        monkeypatch.setattr(object_storage, name, getattr(store, name))
    monkeypatch.setattr(worker, "FLOORPLAN_FROM_RECONSTRUCTION", False)
    monkeypatch.setattr(worker.ws_hub, "broadcast", lambda *a, **k: asyncio.sleep(0))
    return types.SimpleNamespace(db=db, store=store, tmp=tmp_path, mp=monkeypatch)


def _job(**kw):
    from tenancy import Role, TenantContext

    return worker.ReconstructionJob(
        ctx=TenantContext(agent_id="agent@x.test", tenant_id=TENANT, role=Role.AGENT),
        job_id=JOB, lead_id=LEAD, listing_id=None, **kw,
    )


def _with_images(env, images):
    async def _gather(job, dest):
        return images, {"photos": len(images), "videos": 0, "frames": 0}

    env.mp.setattr(worker, "_gather_source_images", _gather)


def _good_capture(tmp, n=24):
    d = tmp / "cap"
    d.mkdir(exist_ok=True)
    return [_jpeg(d / f"f{i:03d}.jpg", seed=i) for i in range(n)]


def _run(env, provider, job=None):
    env.mp.setattr(worker, "get_provider", lambda: provider)
    asyncio.run(worker._process(job or _job()))


# ── tests ────────────────────────────────────────────────────────────────────
def test_a_good_capture_reaches_ready_through_every_stage_in_order(env):
    _with_images(env, _good_capture(env.tmp))
    provider = _Provider()
    _run(env, provider)

    assert provider.calls == 1
    assert env.db.stages() == ["reconstructing", "converting", "analyzing"]
    publish = env.db.find("stage = 'ready'")
    assert publish, "the job was never marked ready"
    tx, sql, args = publish[-1]
    # Atomic publish: the media insert and the supersede share the ready tx.
    same_tx = [s for t, s, _ in env.db.calls if t == tx]
    assert any(s.startswith("INSERT INTO property_media") for s in same_tx)
    assert any("SET superseded_at = now()" in s for s in same_tx)
    assert "cost_estimate_usd" in sql and 0.4321 in args
    assert "output_bytes" in sql


def test_the_provider_job_id_is_recorded_the_moment_it_exists(env):
    _with_images(env, _good_capture(env.tmp))
    _run(env, _Provider())
    recorded = env.db.find("SET provider_job_id = $2")
    assert recorded and recorded[0][2][1] == "pod-xyz"


def test_scene_manifest_and_poses_travel_with_the_delivered_asset(env):
    _with_images(env, _good_capture(env.tmp))
    _run(env, _Provider())
    keys = list(env.store.objects)
    sog = [k for k in keys if k.endswith(".sog")]
    assert len(sog) == 1
    assert sog[0] + capture_sidecars.CAMERA_SIDECAR_SUFFIX in keys
    assert sog[0] + capture_sidecars.POINTS_SIDECAR_SUFFIX in keys
    scene = json.loads(env.store.objects[sog[0] + ".scene.json"])
    assert scene["version"] == 2
    assert scene["pipelineVersion"] == worker.PIPELINE_VERSION
    assert scene["entryCamera"] is not None
    assert scene["scale"]["measurementsAllowed"] is False


def test_missing_poses_are_a_stated_limitation_not_a_silent_ready(env):
    _with_images(env, _good_capture(env.tmp))
    _run(env, _Provider(with_poses=False, with_points=False))
    summary = env.db.find("'limitations'")
    assert summary
    limitations = json.loads(summary[-1][2][2])
    assert "camera_poses_missing" in limitations
    assert "floorplan_unavailable" in limitations
    view = space_status.public_view({
        "id": JOB, "status": "succeeded", "stage": "ready",
        "diagnostics": {"limitations": limitations},
    })
    assert view["state"] == "ready"
    assert any("Starting view" in c for c in view["caveats"])


def test_a_blurry_dark_capture_is_refused_before_any_gpu_is_rented(env):
    d = env.tmp / "bad"
    d.mkdir()
    images = [_jpeg(d / f"b{i}.jpg", seed=i, blur=True, dark=True) for i in range(20)]
    _with_images(env, images)
    provider = _Provider()
    _run(env, provider)

    assert provider.calls == 0, "a GPU was rented for a capture the gate should refuse"
    final = env.db.find("SET status = $2")[-1]
    assert final[2][1] == "failed_quality_gate"
    gate = [a for _, s, a in env.db.calls if "jsonb_build_object($2::text" in s and a[1] == "quality_gate"]
    payload = json.loads(gate[-1][2])
    assert payload["refused"] is True
    assert payload["guidance"], "no recapture guidance was recorded"
    view = space_status.public_view({"id": JOB, "status": "failed_quality_gate",
                                     "stage": "failed", "quality_gate": "capture",
                                     "diagnostics": {"quality_gate": payload}})
    assert view["retry_kind"] == "recapture"
    assert view["guidance"]
    for word in ("COLMAP", "Gaussian", "GPU", "pod", "splat"):
        assert word.lower() not in json.dumps(view).lower()


def test_a_conversion_failure_keeps_the_raw_output_and_needs_no_new_gpu(env):
    _with_images(env, _good_capture(env.tmp))
    provider = _Provider(output=".ply")

    async def _broken(src, work_dir, media_id):
        raise ProviderError("splat-transform could not convert model.ply to .sog: boom")

    env.mp.setattr(worker, "_convert_to_delivery", _broken)
    _run(env, provider)

    final = env.db.find("SET status = $2")[-1]
    assert final[2][1] == "needs_attention"
    raw_keys = [k for k in env.store.objects if "/raw/" in k]
    assert any(k.endswith(".ply") for k in raw_keys), raw_keys
    assert not env.db.find("stage = 'ready'")

    # Retry from the preserved output: the provider is never called again.
    raw_key = next(k for k in raw_keys if k.endswith(f"{JOB}.ply"))
    env.mp.undo()
    env.mp.setattr(worker, "tenant_tx", env.db)
    import media_storage
    import object_storage

    env.mp.setattr(media_storage, "storage_available", lambda: True)
    for name in ("put_file", "put_bytes", "get_bytes"):
        env.mp.setattr(object_storage, name, getattr(env.store, name))
    env.mp.setattr(worker, "FLOORPLAN_FROM_RECONSTRUCTION", False)
    env.mp.setattr(worker.ws_hub, "broadcast", lambda *a, **k: asyncio.sleep(0))

    async def _ok(src, work_dir, media_id):
        out = work_dir / f"{media_id}.sog"
        out.write_bytes(b"PK\x03\x04" + b"1" * 2048)
        worker._carry_companions(src, out)
        return out

    env.mp.setattr(worker, "_convert_to_delivery", _ok)
    retry_provider = _Provider()
    _run(env, retry_provider, job=_job(resume_from="conversion", raw_output_key=raw_key))
    assert retry_provider.calls == 0
    assert env.db.find("stage = 'ready'")


def test_a_provider_failure_is_categorised_and_reraised_for_the_loop(env):
    _with_images(env, _good_capture(env.tmp))

    class _Down(_Provider):
        async def reconstruct(self, images, work_dir):
            raise ProviderError("RunPod POST /pods failed (500)")

    with pytest.raises(ProviderError):
        _run(env, _Down())
    final = env.db.find("SET status = $2")[-1]
    assert final[2][1] == "failed"
    assert "provider" in final[2]


def test_progress_is_never_a_guessed_percentage():
    for stage in space_status.STAGES:
        label, message = space_status.LABELS[stage]
        assert "%" not in label and "%" not in message
    view = space_status.public_view({"id": JOB, "status": "running", "stage": "reconstructing"})
    assert "progress" not in view
    assert [s["state"] for s in view["steps"]][:3] == ["done", "done", "current"]


def test_a_stalled_job_reads_failed_even_if_its_stage_was_mid_run():
    # reconciliation.py fails stalled rows by status only; the stage column
    # must not keep claiming "building".
    view = space_status.public_view({"id": JOB, "status": "failed", "stage": "reconstructing"})
    assert view["state"] == "failed"
    assert view["can_retry"] is True


def test_quality_gate_drops_duplicates_but_never_below_the_minimum(tmp_path):
    frames = [_jpeg(tmp_path / f"d{i}.jpg", seed=1) for i in range(12)]  # all identical
    frames += [_jpeg(tmp_path / f"u{i}.jpg", seed=100 + i) for i in range(10)]
    verdict = capture_quality.assess(frames)
    assert not verdict.refused
    assert verdict.dropped["duplicate"] >= 5
    assert len(verdict.kept) >= capture_quality.MIN_USABLE_FRAMES
    assert "duplicates" in verdict.warnings


def test_quality_gate_refuses_thumbnails_and_too_few_frames(tmp_path):
    tiny = [_jpeg(tmp_path / f"t{i}.jpg", seed=i, size=(320, 240)) for i in range(20)]
    assert capture_quality.assess(tiny).reason == "low_resolution"
    few = [_jpeg(tmp_path / f"f{i}.jpg", seed=i) for i in range(3)]
    verdict = capture_quality.assess(few)
    assert verdict.refused and verdict.reason == "too_few_frames"
    assert verdict.guidance
    assert capture_quality.assess([]).reason == "no_media"


def test_unreadable_files_are_reported_not_fatal(tmp_path):
    good = [_jpeg(tmp_path / f"g{i}.jpg", seed=i) for i in range(12)]
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    verdict = capture_quality.assess(good + [broken])
    assert not verdict.refused
    assert verdict.dropped["unreadable"] == 1
    assert "unreadable" in verdict.warnings
