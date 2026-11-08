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
    # Which frame, so the build can say which capture it never used.
    assert verdict.unreadable == [broken]
    # The recorded verdict stays counts and ratios: no file paths.
    assert str(broken) not in json.dumps(verdict.as_diagnostics(), default=str)


# ── post-reconstruction quality gate ────────────────────────────────────────

class _MeasuredProvider(_Provider):
    """A provider whose pod returned a quality report (quality.json)."""

    def __init__(self, quality, *, sog_model=None, **kw):
        super().__init__(**kw)
        self.quality = quality
        self.sog_model = sog_model

    async def reconstruct(self, images, work_dir):
        out = await super().reconstruct(images, work_dir)
        if self.sog_model is not None:
            import zipfile

            with zipfile.ZipFile(out, "w") as bundle:
                bundle.writestr("meta.json", json.dumps({"version": 2, "model": self.sog_model}))
                bundle.writestr("means_l.webp", b"0" * 2048)
        self.last_metrics = {**(self.last_metrics or {}), "quality": self.quality}
        return out


_GOOD_QUALITY = {
    "input_images": 24,
    "colmap": {"registration_ratio": 0.95, "mean_reprojection_error_px": 0.7},
    "held_out": {"psnr": 26.0, "ssim": 0.86},
    "training": {"antialiased": True},
}


def test_a_reconstruction_that_does_not_match_its_photos_is_not_published(env):
    _with_images(env, _good_capture(env.tmp))
    bad = {**_GOOD_QUALITY, "held_out": {"psnr": 12.0, "ssim": 0.41}}
    _run(env, _MeasuredProvider(bad))

    assert not env.db.find("stage = 'ready'"), "a space that fails its own photos was published"
    final = env.db.find("SET status = $2")[-1]
    assert final[2][1] == "failed_quality_gate"
    assert "reconstruction" in final[2], "the gate must say WHICH gate refused it"
    # The GPU work is already paid for: its output is kept.
    assert any("/raw/" in k for k in env.store.objects)
    gate = [a for _, s, a in env.db.calls if "jsonb_build_object($2::text" in s and a[1] == "quality_gate"]
    payload = json.loads(gate[-1][2])
    assert payload["gate"] == "reconstruction" and payload["guidance"]
    view = space_status.public_view({"id": JOB, "status": "failed_quality_gate", "stage": "failed",
                                     "quality_gate": "reconstruction",
                                     "diagnostics": {"quality_gate": payload}})
    assert view["retry_kind"] == "recapture"
    for word in ("COLMAP", "Gaussian", "GPU", "pod", "splat", "PSNR"):
        assert word.lower() not in json.dumps(view["guidance"]).lower()


def test_measured_quality_and_render_model_travel_with_the_space(env):
    _with_images(env, _good_capture(env.tmp))
    soft = {**_GOOD_QUALITY, "held_out": {"psnr": 20.0, "ssim": 0.8}}
    _run(env, _MeasuredProvider(soft, sog_model="antialiased"))

    assert env.db.find("stage = 'ready'")
    sog = next(k for k in env.store.objects if k.endswith(".sog"))
    scene = json.loads(env.store.objects[sog + ".scene.json"])
    assert scene["renderModel"] == "antialiased"
    assert scene["quality"]["held_out_psnr"] == 20.0
    assert "quality_low" in scene["limitations"]
    summary = json.loads(env.db.find("'limitations'")[-1][2][2])
    assert "quality_low" in summary


def test_the_file_decides_how_it_is_drawn(env):
    """The pod report says antialiased, the delivered .sog says classic: the
    file wins, because it is what the viewer will actually draw."""
    _with_images(env, _good_capture(env.tmp))
    _run(env, _MeasuredProvider(_GOOD_QUALITY, sog_model="classic-tagless"))
    sog = next(k for k in env.store.objects if k.endswith(".sog"))
    assert json.loads(env.store.objects[sog + ".scene.json"])["renderModel"] == "classic"


def test_an_untagged_sog_falls_back_to_the_training_report(env):
    """splat-transform's filters drop the tag; untagged is unknown, and the pod
    report (trained antialiased) decides. Treating it as classic drew the proof
    run's scene the wrong way."""
    _with_images(env, _good_capture(env.tmp))

    class _Untagged(_MeasuredProvider):
        async def reconstruct(self, images, work_dir):
            import zipfile

            out = await super().reconstruct(images, work_dir)
            with zipfile.ZipFile(out, "w") as bundle:
                bundle.writestr("meta.json", json.dumps({"version": 2}))
            return out

    _run(env, _Untagged(_GOOD_QUALITY))
    sog = next(k for k in env.store.objects if k.endswith(".sog"))
    assert json.loads(env.store.objects[sog + ".scene.json"])["renderModel"] == "antialiased"


def test_a_run_without_a_quality_report_is_marked_unverified(env):
    _with_images(env, _good_capture(env.tmp))
    _run(env, _Provider())
    assert env.db.find("stage = 'ready'")
    summary = json.loads(env.db.find("'limitations'")[-1][2][2])
    assert "quality_unverified" in summary


def test_the_build_records_which_media_it_was_built_from(env):
    """Freshness is decided against this set (T12.09): a photo added after the
    build is one the space was never checked against."""
    images = _good_capture(env.tmp)
    ids = ["aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002"]

    async def _gather(job, dest):
        return images, {"photos": len(images), "videos": 0, "frames": 0,
                        "input_media_ids": ids, "skipped_media_ids": ids[1:],
                        "newest_input_at": "2026-10-17T09:00:00+00:00"}

    env.mp.setattr(worker, "_gather_source_images", _gather)
    _run(env, _Provider())
    capture = [json.loads(a[2]) for _, s, a in env.db.calls
               if "jsonb_build_object($2::text" in s and a[1] == "capture"]
    assert capture, "the capture stage was never recorded"
    assert capture[0]["input_media_ids"] == ids
    assert capture[0]["skipped_media_ids"] == ids[1:]
    assert capture[0]["newest_input_at"] == "2026-10-17T09:00:00+00:00"


def _photo_rows(ids):
    """property_media rows for photos with stored bytes, as the gather reads them."""
    import datetime as dt

    stamp = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.timezone.utc)
    return [{"id": mid, "kind": "photo", "s3_key": f"{mid}.jpg", "bytes": None,
             "content_type": "image/jpeg", "created_at": stamp} for mid in ids]


def _serve_capture(env, rows, blobs):
    """The real gather reads these rows, and each row's bytes from `blobs`."""
    original = _Conn.fetch

    async def _fetch(self, sql, *args):
        if "FROM property_media pm" in sql:
            self.db.calls.append((self.tx_id, " ".join(sql.split()), args))
            return rows
        return await original(self, sql, *args)

    async def _load(row):
        return blobs[str(row["id"])]

    env.mp.setattr(_Conn, "fetch", _fetch)
    env.mp.setattr(worker.media_storage, "load_media_bytes", _load)


def _stage_records(env, stage):
    """The writes of one diagnostics stage that committed, in order."""
    return [json.loads(a[2]) for _, s, a in env.db.calls
            if "jsonb_build_object($2::text" in s and a[1] == stage]


def _evidence_that_verifies(sc):
    """Every other piece of evidence a space needs, all passing (the control)."""
    media_id = "cccccccc-0000-4000-8000-000000000001"
    runs = [{"platform": p, "real_device": True, "steady_fps": 40.0, "media_id": media_id}
            for p in sc.DEVICE_PLATFORMS]
    return dict(
        job_found=True, job_status="succeeded", limitations=[], fresh=True,
        media_id=media_id, device_runs=runs,
        quality={"held_out_psnr": 28.8, "held_out_ssim": 0.89, "held_out_lpips": 0.086,
                 "registration_ratio": 0.95, "mean_reprojection_error_px": 0.5,
                 "input_images": 150,
                 "room_graph": {"rooms": 1, "components": 1, "edges": 0, "verified_edges": 0,
                                "expected_rooms": 1}})


def _with_a_truncated_photo(env, prefix):
    """12 good photos and one JPEG cut off mid-scan: it loads, but the capture
    gate cannot decode it. Returns (every id, the truncated id)."""
    good = _good_capture(env.tmp, n=12)
    ids = [f"{prefix}-0000-4000-8000-{i:012d}" for i in range(13)]
    truncated = ids[-1]
    blobs = {mid: path.read_bytes() for mid, path in zip(ids, good)}
    whole = good[0].read_bytes()
    blobs[truncated] = whole[: len(whole) // 3]
    _serve_capture(env, _photo_rows(ids), blobs)
    return ids, truncated


def test_a_photo_that_loads_but_cannot_be_decoded_is_recorded_as_unread(env):
    """Bytes that load but do not decode reach the capture gate, which drops
    them. The capture stage was written before the gate, so the build used to
    list them as read: a space missing that photo read "every capture read"
    and could be certified (T12.09, AC12.06)."""
    import space_certification as sc

    ids, truncated = _with_a_truncated_photo(env, "bbbbbbbb")
    _run(env, _Provider())

    gate = _stage_records(env, "quality_gate")
    assert gate[-1]["dropped"]["unreadable"] == 1 and not gate[-1]["refused"]
    final = _stage_records(env, "capture")[-1]
    assert final["input_media_ids"] == sorted(ids)
    assert final["skipped_media_ids"] == [truncated]
    assert truncated not in json.dumps(gate), "the gate's record carries no media ids or paths"

    # The tour counts the current capture rows the build listed as skipped
    # (evidence_unread); the build view counts the list itself.
    view = sc.from_job_row({"status": "succeeded", "diagnostics": {"capture": final}})
    assert view.unread == 1 and "capture_unread" in view.reasons
    everything_else_passed = _evidence_that_verifies(sc)
    assert sc.certify(**everything_else_passed).state == sc.VERIFIED   # the control
    cert = sc.certify(**everything_else_passed, unread=len(final["skipped_media_ids"]))
    assert cert.state != sc.VERIFIED and "capture_unread" in cert.reasons


@pytest.mark.parametrize("fail_from", [1, 2])
def test_a_capture_record_the_database_refuses_never_says_an_undecodable_photo_was_read(
        env, fail_from):
    """The unread record and the build's success are one outcome (T12.09).

    The capture stage used to be written twice: before the capture gate,
    listing every row as read, then again, best-effort, with the rows the gate
    could not decode. When the first write committed and the second failed,
    the build published while its record said the undecodable photo was read,
    so certification could count it read and fresh. `fail_from` is the first
    capture-stage write the database refuses: 1, every one; 2, every one
    after the first."""
    ids, truncated = _with_a_truncated_photo(env, "dddddddd")
    attempts = []
    original = _Conn.execute

    async def _execute(self, sql, *args):
        if "jsonb_build_object($2::text" in sql and args[1] == "capture":
            attempts.append(args)
            if len(attempts) >= fail_from:
                raise ConnectionError("the database went away")
        return await original(self, sql, *args)

    env.mp.setattr(_Conn, "execute", _execute)
    provider = _Provider()
    try:
        _run(env, provider)
    except ConnectionError:
        pass

    published = bool(env.db.find("stage = 'ready'"))
    records = _stage_records(env, "capture")
    for record in records:
        read = set(record["input_media_ids"]) - set(record.get("skipped_media_ids") or [])
        assert truncated not in read, "a committed record says the undecodable photo was read"
    # One write, after the gate, carrying what it found. It commits, or the
    # build stops before any GPU is rented and publishes nothing.
    assert len(attempts) == 1
    if fail_from == 1:
        assert not records and not published and provider.calls == 0
        assert env.db.find("SET status = $2")[-1][2][1] == "failed"
    else:
        assert published and records[-1]["skipped_media_ids"] == [truncated]


def test_frames_outside_the_gates_sample_are_named_and_never_kept(tmp_path):
    """The gate analyses at most MAX_ANALYSED frames, evenly spaced (the
    sampling is unchanged). The frames it never looked at are named, so the
    build can say which capture it never read, and stay out of its record."""
    paths = [tmp_path / f"s{i:04d}.jpg" for i in range(capture_quality.MAX_ANALYSED + 5)]
    seen = []

    def _stats(path):
        seen.append(path)
        return capture_quality.FrameStats(path=path, width=960, height=720,
                                          luma=120.0, sharpness=300.0)

    verdict = capture_quality.assess(paths, analyse=_stats)
    assert len(seen) == capture_quality.MAX_ANALYSED
    assert verdict.unexamined == [p for p in paths if p not in set(seen)]
    assert len(verdict.unexamined) == 5
    assert not set(verdict.unexamined) & set(verdict.kept)
    assert str(verdict.unexamined[0]) not in json.dumps(verdict.as_diagnostics(), default=str)
    # Within the budget every frame is looked at.
    assert capture_quality.assess(paths[:50], analyse=_stats).unexamined == []


def test_a_media_row_is_read_when_any_of_its_files_was_analysed_and_decoded():
    """Frames the gate could not decode and frames outside its sample are both
    unread. A video with one analysed, decoded frame was read."""
    video, broken_video, photo, read_photo = (
        f"ffffffff-0000-4000-8000-{i:012d}" for i in range(4))
    work = Path("/work/images")
    images = [work / f"{video}_1.jpg", work / f"{video}_2.jpg",
              work / f"{broken_video}_1.jpg", work / f"{broken_video}_2.jpg",
              work / f"{photo}.jpg", work / f"{read_photo}.jpg"]
    verdict = capture_quality.QualityVerdict(
        refused=False, reason=None, kept=[images[0], images[5]],
        unreadable=[images[2]], unexamined=[images[1], images[3], images[4]])
    counts = {"input_media_ids": [video, broken_video, photo, read_photo]}
    assert worker._unread_media(images, verdict, counts) == sorted([broken_video, photo])
    # Only ids in the recorded input set count.
    assert worker._unread_media(images, verdict, {"input_media_ids": [video]}) == []


def test_a_photo_outside_the_capture_gates_sample_is_recorded_as_unread(env):
    """A capture of more than MAX_ANALYSED frames: the gate analyses an evenly
    spaced sample and keeps only what it analysed, so a photo outside the
    sample is never decoded or reconstructed. It used to stay in the input set
    as read, so a space built without it could be certified (T12.09,
    AC12.06). It is now recorded as unread; the sampling is unchanged."""
    import space_certification as sc

    n = capture_quality.MAX_ANALYSED + 1
    ids = [f"eeeeeeee-0000-4000-8000-{i:012d}" for i in range(n)]
    jpeg = _good_capture(env.tmp, n=1)[0].read_bytes()
    _serve_capture(env, _photo_rows(ids), {mid: jpeg for mid in ids})

    # Pixel statistics stand in for decoding 901 JPEGs; the gather, the
    # sampling, the selection and the build's record are the real ones.
    analysed = []
    real_assess = capture_quality.assess

    def _stats(path):
        analysed.append(path)
        return capture_quality.FrameStats(path=path, width=960, height=720,
                                          luma=120.0, sharpness=300.0)

    env.mp.setattr(capture_quality, "assess",
                   lambda images, **kw: real_assess(images, analyse=_stats, **kw))
    _run(env, _Provider())

    assert env.db.find("stage = 'ready'"), "a capture this size still builds"
    never = sorted(set(ids) - {p.stem for p in analysed})
    assert len(never) == 1, never      # one photo fell outside the sample
    gate = _stage_records(env, "quality_gate")[-1]
    assert (gate["submitted"], gate["analysed"]) == (n, capture_quality.MAX_ANALYSED)
    final = _stage_records(env, "capture")[-1]
    assert final["input_media_ids"] == sorted(ids)
    assert final["skipped_media_ids"] == never

    view = sc.from_job_row({"status": "succeeded", "diagnostics": {"capture": final}})
    assert view.unread == 1 and "capture_unread" in view.reasons
    everything_else_passed = _evidence_that_verifies(sc)
    assert sc.certify(**everything_else_passed).state == sc.VERIFIED   # the control
    cert = sc.certify(**everything_else_passed, unread=len(final["skipped_media_ids"]))
    assert cert.state != sc.VERIFIED and "capture_unread" in cert.reasons
