"""reconstruction_providers.py — pluggable Gaussian-splat reconstruction backends.

A ReconstructionProvider turns a set of capture images into a Gaussian-splat file
(.ply or .splat). The worker (reconstruction_worker.py) runs the shared tail:
convert→store→record→broadcast. Adapters (selected by RECONSTRUCTION_PROVIDER):

  stub        StubProvider       — generates a synthetic room .splat (or copies a
                                    fixture). $0, no GPU, no network. Dev/demo/tests.
  local       LocalGpuProvider   — COLMAP (BSD) + an Apache-licensed 3DGS trainer
                                    on a local CUDA GPU. $0 compute.
  cloud       CloudGpuProvider   — rent a per-job GPU via a RunPod/Vast HTTP job
                                    endpoint. ~$0.10-0.30/house, per-second billing.
  serverless  ServerlessProvider — NO local GPU: POST the capture to a serverless
                                    GPU endpoint (RunPod Serverless / Replicate /
                                    Modal running our OSS pipeline) or a managed
                                    SaaS. pay-per-use or subscription.

Honesty rule: a real provider that isn't configured reports available()=(False,why)
so the enqueue endpoint returns 503 — it NEVER silently fabricates a result.
DUSt3R / INRIA-3DGS are deliberately excluded (non-commercial licenses).
"""

from __future__ import annotations

import asyncio
import types
from collections import deque
import json
import logging
import mimetypes
import os
import re
import shutil
import struct
import time

import capture_sidecars
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("oracle.reconstruction")

# Canonical here, rather than imported from spatial_agent, which used to own it.
# That module ran a parallel reconstruction pipeline whose output was served from
# an unauthenticated static mount at a path derived from sha256(address) — any
# party who knew an address could fetch another tenant's reconstruction. It was
# deleted rather than repaired; this is the only pipeline now.
SPATIAL_AI_DISCLOSURE = (
    "AI-generated 3D reconstruction from photos; geometry may be incomplete or "
    "inaccurate. Not a measured survey or a substitute for an in-person showing."
)

# Scratch space for providers that stage files on local disk before upload.
# Finished splats do NOT live here — they go to object storage and are served
# through the authenticated /api/media/{id} route like every other asset.
SPLAT_OUTPUT_DIR = Path(os.environ.get("ORACLE_SPLAT_DIR", "/tmp/oracle_splats"))

REQUEST_TIMEOUT = int(os.environ.get("RECON_HTTP_TIMEOUT", "1800"))  # 30 min poll budget
MIN_CAPTURE_IMAGES = 8
MAX_CAPTURE_IMAGES = 300
MAX_CAPTURE_IMAGE_BYTES = 50 * 1024 * 1024
MAX_RECON_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MIN_RUNPOD_TIMEOUT = 60
#: Raised when the default became 7200: a default sitting exactly on the
#: maximum leaves an operator no room to grant a slow capture more time.
MAX_RUNPOD_TIMEOUT = 14400
_RUNPOD_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")


class ProviderError(RuntimeError):
    """Reconstruction failed in a way the worker should record + surface."""


def _validate_capture_images(images: list[Path], *, minimum: int = 1) -> None:
    if len(images) < minimum:
        raise ProviderError(f"need at least {minimum} capture images")
    if len(images) > MAX_CAPTURE_IMAGES:
        raise ProviderError(f"capture exceeds {MAX_CAPTURE_IMAGES} image limit")
    for path in images:
        if not path.is_file():
            raise ProviderError("capture contains a missing image file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_CAPTURE_IMAGE_BYTES:
            raise ProviderError("capture contains an empty or oversized image")


def _staged_image_name(index: int, path: Path) -> str:
    suffix = path.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".img"
    return f"{index:04d}{suffix}"


#: The format a finished reconstruction is delivered in.
#:
#: This is `.sog`, and it is forced rather than chosen. The pipeline used to
#: target `.splat` (antimatter15) and asked PlayCanvas splat-transform to write
#: it — which no released version can do. v2.7.1, v3.0.0 and 3.3.0 all list
#: `.splat` as input-only, and the pinned binary's own --help agrees. So every
#: provider that emits PLY from splatfacto failed at conversion, and only
#: StubProvider (which writes .splat bytes directly, bypassing the converter)
#: ever succeeded. `.sog` is what splat-transform writes, what the PlayCanvas
#: engine in package.json renders, and ~10x smaller than PLY.
DELIVERY_SUFFIX = ".sog"

#: Raw formats a provider may return; all convert to DELIVERY_SUFFIX.
CONVERTIBLE_SUFFIXES = (".ply", ".compressed.ply", ".spz", ".ksplat")

#: Pinned, and it must stay pinned — keep in step with the container images.
#: This pipeline depended on format support upstream never had; an unpinned
#: resolve is how that kind of drift arrives with no diff to review.
SPLAT_TRANSFORM_VERSION = "3.3.0"


def _validate_artifact(path: Path, *, provider: str) -> None:
    """Reject an artifact that cannot be a splat, without assuming its format.

    The 32-byte row check is specific to `.splat`: that format is a headerless
    array of fixed 32-byte records, so a size that is not a multiple of 32 is
    proof of truncation. `.sog` is a compressed container and has no such
    invariant — applying the check to one would fail every valid file, turning
    a working reconstruction into "invalid artifact" 31 times out of 32.
    """
    if not path.is_file():
        raise ProviderError(f"{provider} produced no reconstruction artifact")
    size = path.stat().st_size
    if size == 0:
        raise ProviderError(f"{provider} produced an empty reconstruction artifact")
    name = path.name.lower()
    if name.endswith(".splat") and size % 32 != 0:
        raise ProviderError(
            f"{provider} produced a truncated .splat artifact "
            f"({size} bytes is not a multiple of the 32-byte row)"
        )
    if name.endswith(".ply"):
        with path.open("rb") as fh:
            if fh.read(3) != b"ply":
                raise ProviderError(f"{provider} produced an invalid .ply artifact")


def _download_first_available(s3, bucket: str, out_key: str, work_dir: Path, *, provider: str) -> Path:
    """Fetch whichever delivery format the remote container actually wrote.

    The container images now emit `model.sog`, but an image built before the
    format fix emits `model.splat`, and both are renderable. Trying the current
    format first and falling back means a redeploy of the backend does not
    require rebuilding and republishing every worker image on the same day.

    Raises with the *last* transport error rather than a generic "not found",
    so a permissions failure does not get reported as a missing artifact.
    """
    stem = out_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    prefix = out_key.rsplit("/", 1)[0]
    last_error: Optional[Exception] = None
    for suffix in (DELIVERY_SUFFIX, ".splat"):
        candidate = work_dir / f"{stem}{suffix}"
        try:
            s3.download_file(bucket, f"{prefix}/{stem}{suffix}", str(candidate))
        except Exception as exc:  # noqa: BLE001 - try the next format, keep the reason
            last_error = exc
            continue
        return candidate
    raise ProviderError(
        f"{provider} wrote no readable artifact "
        f"({stem}{DELIVERY_SUFFIX} or {stem}.splat): {last_error}"
    )


def _validate_remote_output_url(value: object, service_url: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ProviderError("remote job returned an invalid output URL")
    output = urlparse(value)
    service = urlparse(service_url)
    host = (output.hostname or "").lower().rstrip(".")
    service_host = (service.hostname or "").lower().rstrip(".")
    configured_hosts = {
        item.strip().lower().rstrip(".")
        for item in os.environ.get("RECON_REMOTE_OUTPUT_HOSTS", "").split(",")
        if item.strip()
    }
    is_s3 = (
        (host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn"))
        and any(label == "s3" or label.startswith("s3-") for label in host.split("."))
    )
    if (
        output.scheme != "https"
        or not host
        or output.username is not None
        or output.password is not None
        or output.port not in (None, 443)
        or output.fragment
        or (host != service_host and host not in configured_hosts and not is_s3)
    ):
        raise ProviderError("remote job returned an untrusted output URL")
    return value


def _validate_remote_service_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ProviderError("remote reconstruction URL is invalid")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    loopback_http = parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    if (
        not host
        or (parsed.scheme != "https" and not loopback_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise ProviderError("remote reconstruction URL must use HTTPS")
    return value.rstrip("/")


# ---------------------------------------------------------------------------
# Demo / stub splat — a correct 32-byte-row .splat (gsplat RowLength=32):
#   pos 3xf32 | scale 3xf32 | rgba 4xu8 | rot 4xu8 (quat [w,x,y,z], q*128+128)
# Generates a simple walkable "room" (floor, ceiling, four walls) so the viewer +
# bounds-clamp + walk controls are provable with zero capture/compute.
# ---------------------------------------------------------------------------
def _row(px, py, pz, sx, sy, sz, r, g, b, a=255) -> bytes:
    # Identity-ish rotation: bytes [255,128,128,128] -> quat (w~1, x=y=z=0).
    return struct.pack("<3f3f", px, py, pz, sx, sy, sz) + bytes((r, g, b, a, 255, 128, 128, 128))


def write_demo_splat(path: Path, *, w: float = 4.0, h: float = 2.6, d: float = 4.0, step: float = 0.09) -> Path:
    rows = bytearray()

    def plane_y(val, c1, c2):
        a = -w / 2
        while a <= w / 2 + 1e-6:
            b = -d / 2
            while b <= d / 2 + 1e-6:
                # subtle checker so flat planes read as surfaces, not a void
                even = (int((a + 8) / step) + int((b + 8) / step)) % 2 == 0
                col = c1 if even else c2
                rows.extend(_row(a, val, b, 0.05, 0.012, 0.05, *col))
                b += step
            a += step

    # floor + ceiling
    plane_y(0.0, (150, 140, 128), (120, 112, 100))
    plane_y(h, (210, 212, 220), (185, 188, 198))
    # four walls (sweep height)
    y = 0.0
    while y <= h + 1e-6:
        x = -w / 2
        while x <= w / 2 + 1e-6:
            rows.extend(_row(x, y, -d / 2, 0.05, 0.05, 0.012, 196, 184, 168))  # back
            rows.extend(_row(x, y, d / 2, 0.05, 0.05, 0.012, 196, 184, 168))   # front
            x += step
        z = -d / 2
        while z <= d / 2 + 1e-6:
            rows.extend(_row(-w / 2, y, z, 0.012, 0.05, 0.05, 188, 176, 160))  # left
            rows.extend(_row(w / 2, y, z, 0.012, 0.05, 0.05, 188, 176, 160))   # right
            z += step
        y += step

    path.write_bytes(rows)
    log.info("demo splat written: %s (%d gaussians)", path, len(rows) // 32)
    return path


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class ReconstructionProvider:
    name = "base"

    # What this provider's output actually depicts, stored on the resulting
    # property_media row (migration 0071). Only 'captured' output may be
    # presented as the actual home — the resolver refuses to promote anything
    # else to tier 3, no matter how well it renders.
    #
    # Real reconstruction backends inherit 'captured'. A provider that
    # synthesises geometry must say so; it is not the resolver's job to guess
    # from the provider name.
    produces = "captured"

    def available(self) -> tuple[bool, str]:
        """(ready, reason-if-not). The enqueue endpoint 503s when not ready."""
        return (False, "not implemented")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        """Produce a .ply or .splat in work_dir from the capture images."""
        raise NotImplementedError


class UnavailableProvider(ReconstructionProvider):
    """Fail-closed provider for an unknown configuration value."""

    def __init__(self, configured_name: str):
        self.name = configured_name or "<empty>"

    def available(self) -> tuple[bool, str]:
        return (False, f"unknown RECONSTRUCTION_PROVIDER {self.name!r}")


class StubProvider(ReconstructionProvider):
    name = "stub"
    # Not a capture of this property, either way: without RECON_STUB_FIXTURE it
    # synthesises a demo room, and with one it copies a fixture captured
    # somewhere else. Everything downstream keys off this to avoid describing
    # the result as the actual home.
    produces = "synthetic"

    def available(self) -> tuple[bool, str]:
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        out = work_dir / "stub.splat"
        fixture = os.environ.get("RECON_STUB_FIXTURE", "")
        if fixture and Path(fixture).is_file():
            shutil.copyfile(fixture, out)
            return out
        # No fixture → synthesize a walkable demo room.
        return write_demo_splat(out)


#: COLMAP links Qt and builds a QGuiApplication even for its CLI subcommands.
#: On a headless host — which every deployed backend is — that aborts with
#: SIGABRT inside QGuiApplicationPrivate::createPlatformIntegration() before it
#: reads a single image, so the failure looks like a corrupt capture rather than
#: a missing display. Verified on a headless GPU box 2026-08-23: without this,
#: `colmap feature_extractor` dies with rc=-6 in 0s; with a display available it
#: extracted features from 43 images in 27s.
#:
#: `offscreen` needs no extra process and is enough for COLMAP's CPU paths. GPU
#: SIFT wants a real GL context, so a deployment that sets
#: SiftExtraction.use_gpu must run the binary under xvfb-run instead — that is
#: an image-level concern, which is why this only guarantees the process starts.
_COLMAP_ENV = {"QT_QPA_PLATFORM": "offscreen"}


async def _run(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = REQUEST_TIMEOUT,
    env: Optional[dict[str, str]] = None,
) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env} if env else None,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProviderError(f"timed out: {' '.join(cmd[:2])}")
    if proc.returncode != 0:
        tail = (out or b"")[-600:].decode("utf-8", "replace")
        raise ProviderError(f"{cmd[0]} exited {proc.returncode}: {tail}")


class LocalGpuProvider(ReconstructionProvider):
    """COLMAP poses + an Apache-licensed 3DGS trainer on a local CUDA GPU.

    The trainer command is configurable (RECON_TRAINER_CMD) since installs vary
    (nerfstudio splatfacto, gsplat, Brush). Tokens {scene} and {out} are filled.
    """
    name = "local"

    def available(self) -> tuple[bool, str]:
        if shutil.which("nvidia-smi") is None:
            return (False, "no local NVIDIA GPU (nvidia-smi not found)")
        if shutil.which("colmap") is None:
            return (False, "COLMAP not installed (apt install colmap)")
        if not os.environ.get("RECON_TRAINER_CMD"):
            return (False, "set RECON_TRAINER_CMD to your 3DGS trainer (e.g. splatfacto/Brush)")
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        _validate_capture_images(images, minimum=MIN_CAPTURE_IMAGES)
        img_dir = work_dir / "images"
        img_dir.mkdir(exist_ok=True)
        for index, path in enumerate(images):
            shutil.copyfile(path, img_dir / _staged_image_name(index, path))
        db = work_dir / "colmap.db"
        sparse = work_dir / "sparse"
        sparse.mkdir(exist_ok=True)
        await _run(["colmap", "feature_extractor", "--database_path", str(db), "--image_path", str(img_dir)], env=_COLMAP_ENV)
        await _run(["colmap", "exhaustive_matcher", "--database_path", str(db)], env=_COLMAP_ENV)
        await _run(["colmap", "mapper", "--database_path", str(db), "--image_path", str(img_dir), "--output_path", str(sparse)], env=_COLMAP_ENV)
        out_ply = work_dir / "model.ply"
        trainer = os.environ["RECON_TRAINER_CMD"].format(scene=str(work_dir), out=str(out_ply))
        await _run(trainer.split())
        if not out_ply.is_file():
            raise ProviderError("trainer produced no .ply")
        return out_ply


async def _http_reconstruct(url: str, headers: dict, images: list[Path], work_dir: Path) -> Path:
    """Generic submit→poll→download against a remote reconstruction HTTP service.

    Shared by cloud + serverless. Expects: POST multipart images → {job_id};
    GET {url}/{job_id} → {status, output_url|done}. Adjust per-vendor via env.
    """
    import aiohttp  # local import: only when a remote provider is actually used

    url = _validate_remote_service_url(url)
    _validate_capture_images(images)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    open_files = []
    async with aiohttp.ClientSession(timeout=timeout) as s:
        form = aiohttp.FormData()
        try:
            for index, path in enumerate(images):
                image_file = path.open("rb")
                open_files.append(image_file)
                form.add_field(
                    "images",
                    image_file,
                    filename=_staged_image_name(index, path),
                    content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
            async with s.post(url, data=form, headers=headers, allow_redirects=False) as r:
                if r.status >= 400:
                    raise ProviderError(f"submit HTTP {r.status}")
                job = await r.json()
        finally:
            for image_file in open_files:
                image_file.close()
        if not isinstance(job, dict):
            raise ProviderError("remote submit returned an invalid response")
        job_id = job.get("job_id") or job.get("id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise ProviderError("no valid job id in response")
        # poll
        for _ in range(REQUEST_TIMEOUT // 15):
            await asyncio.sleep(15)
            async with s.get(
                f"{url.rstrip('/')}/{job_id}",
                headers=headers,
                allow_redirects=False,
            ) as r:
                if r.status >= 400:
                    raise ProviderError(f"status HTTP {r.status}")
                st = await r.json()
            if not isinstance(st, dict):
                raise ProviderError("remote status returned an invalid response")
            status = str(st.get("status") or "").lower()
            if status in ("succeeded", "completed", "done"):
                dl = _validate_remote_output_url(
                    st.get("output_url") or st.get("ply_url") or st.get("splat_url"),
                    url,
                )
                # Recognise every delivery format. Defaulting anything
                # unknown to .ply meant a .sog artifact was checked for a "ply"
                # magic header it does not have, and rejected as invalid.
                _p = urlparse(dl).path.lower()
                ext = next(
                    (e for e in (DELIVERY_SUFFIX, ".splat", ".spz", ".compressed.ply") if _p.endswith(e)),
                    ".ply",
                )
                out = work_dir / f"remote{ext}"
                async with s.get(dl, allow_redirects=False) as r:
                    if r.status >= 400:
                        raise ProviderError(f"artifact HTTP {r.status}")
                    declared = r.headers.get("Content-Length")
                    if declared:
                        try:
                            declared_size = int(declared)
                        except ValueError as exc:
                            raise ProviderError("remote artifact has an invalid size") from exc
                        if declared_size < 0 or declared_size > MAX_RECON_ARTIFACT_BYTES:
                            raise ProviderError("remote artifact exceeds size limit")
                    written = 0
                    with out.open("wb") as output_file:
                        async for chunk in r.content.iter_chunked(1 << 20):
                            written += len(chunk)
                            if written > MAX_RECON_ARTIFACT_BYTES:
                                raise ProviderError("remote artifact exceeds size limit")
                            output_file.write(chunk)
                if written <= 0:
                    raise ProviderError("remote job returned an empty artifact")
                _validate_artifact(out, provider="remote job")
                return out
            if status in ("failed", "error", "cancelled"):
                raise ProviderError(f"remote job ended with status {status}")
        raise ProviderError("remote job did not finish within budget")


class CloudGpuProvider(ReconstructionProvider):
    """Rent a per-job GPU via a RunPod/Vast HTTP job endpoint running our OSS pipeline.

    Configure RECON_CLOUD_URL + RECON_CLOUD_KEY (the deployed pod/endpoint that
    accepts images and returns a .ply/.splat).
    """
    name = "cloud"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("RECON_CLOUD_URL"):
            return (False, "set RECON_CLOUD_URL (+RECON_CLOUD_KEY) to your rented-GPU job endpoint")
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        url = os.environ["RECON_CLOUD_URL"]
        key = os.environ.get("RECON_CLOUD_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return await _http_reconstruct(url, headers, images, work_dir)


class ServerlessProvider(ReconstructionProvider):
    """No local GPU: a serverless GPU endpoint (RunPod Serverless / Replicate /
    Modal running our OSS container) or a managed SaaS. Configure RECON_SERVERLESS_URL
    + RECON_SERVERLESS_KEY.
    """
    name = "serverless"

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("RECON_SERVERLESS_URL"):
            return (False, "set RECON_SERVERLESS_URL (+RECON_SERVERLESS_KEY) to your serverless/SaaS endpoint")
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        url = os.environ["RECON_SERVERLESS_URL"]
        key = os.environ.get("RECON_SERVERLESS_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return await _http_reconstruct(url, headers, images, work_dir)


class AwsBatchProvider(ReconstructionProvider):
    """AWS GPU reconstruction via AWS Batch (SPOT g5, scales to zero — pay only
    per job, ~$0.30-1/house). Uploads the capture to S3, submits a Batch job that
    runs the COLMAP + 3DGS pipeline on a GPU and writes the .splat back to S3,
    polls, then downloads the result. No keys — the ECS task role provides creds.

    Configure (set by infra/terraform/reconstruction.tf outputs):
      RECON_S3_BUCKET         inputs/outputs bucket
      RECON_AWS_BATCH_QUEUE   Batch job-queue name/arn
      RECON_AWS_BATCH_JOBDEF  Batch job-definition name/arn
      AWS_REGION              region
    """
    name = "aws_batch"

    def available(self) -> tuple[bool, str]:
        miss = [v for v in ("RECON_S3_BUCKET", "RECON_AWS_BATCH_QUEUE", "RECON_AWS_BATCH_JOBDEF")
                if not os.environ.get(v)]
        if miss:
            return (False, "set " + ", ".join(miss) + " (deploy infra/terraform/reconstruction.tf)")
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        _validate_capture_images(images, minimum=MIN_CAPTURE_IMAGES)
        # boto3 is blocking — run off the event loop so the worker stays responsive.
        return await asyncio.to_thread(self._run_blocking, images, work_dir)

    def _run_blocking(self, images: list[Path], work_dir: Path) -> Path:
        import time
        import uuid as _uuid
        import boto3  # lazy — only the AWS path needs the SDK

        bucket = os.environ["RECON_S3_BUCKET"]
        queue = os.environ["RECON_AWS_BATCH_QUEUE"]
        jobdef = os.environ["RECON_AWS_BATCH_JOBDEF"]
        region = os.environ.get("AWS_REGION", "us-east-1")
        timeout = int(os.environ.get("RECON_AWS_TIMEOUT", "3600"))  # 60 min
        s3 = boto3.client("s3", region_name=region)
        batch = boto3.client("batch", region_name=region)

        job_key = _uuid.uuid4().hex
        in_prefix = f"recon-inputs/{job_key}"
        out_key = f"recon-outputs/{job_key}/model{DELIVERY_SUFFIX}"
        for index, path in enumerate(images):
            s3.upload_file(
                str(path),
                bucket,
                f"{in_prefix}/{_staged_image_name(index, path)}",
            )

        sub = batch.submit_job(
            jobName=f"neoh-recon-{job_key[:12]}",
            jobQueue=queue,
            jobDefinition=jobdef,
            containerOverrides={"environment": [
                {"name": "INPUT_S3", "value": f"s3://{bucket}/{in_prefix}"},
                {"name": "OUTPUT_S3", "value": f"s3://{bucket}/{out_key}"},
            ]},
        )
        job_id = sub["jobId"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(20)
            jobs = batch.describe_jobs(jobs=[job_id]).get("jobs", [])
            if not jobs:
                continue
            st = jobs[0]["status"]
            if st == "SUCCEEDED":
                break
            if st == "FAILED":
                raise ProviderError(f"AWS Batch job failed: {jobs[0].get('statusReason', 'unknown')}")
        else:
            try:
                batch.terminate_job(
                    jobId=job_id,
                    reason="Neoh reconstruction exceeded RECON_AWS_TIMEOUT",
                )
            except Exception:  # noqa: BLE001 - retain the original timeout failure
                log.exception("Unable to terminate timed-out AWS Batch reconstruction job %s", job_id)
            raise ProviderError("AWS Batch reconstruction did not finish within budget")

        # Accept either delivery format: a current image writes model.sog, an
        # image built before the format fix writes model.splat. Both render.
        out = _download_first_available(
            s3, bucket, out_key, work_dir, provider="AWS Batch"
        )
        _validate_artifact(out, provider="AWS Batch")
        return out


class RunPodProvider(ReconstructionProvider):
    """RunPod Serverless GPU reconstruction — the no-AWS-GPU-quota path.

    Stages the capture in S3, then hands the worker PRESIGNED URLs (a GET per
    image + a PUT for the result) so the RunPod worker needs NO AWS credentials —
    only the backend touches S3 (it already has the recon bucket's task role).
    Submits to /run, polls /status, downloads the .splat from S3, and CANCELS the
    RunPod job on timeout (the Batch path leaked orphaned jobs). Same
    recon-inputs/<job> / recon-outputs/<job>/model.splat keys as AwsBatchProvider,
    so _store_splat + the tour resolver are unchanged.

    Env: RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID, RECON_S3_BUCKET, AWS_REGION.
    """
    name = "runpod"

    @staticmethod
    def _settings() -> tuple[str, str, str, str, int]:
        api = os.environ.get("RUNPOD_API_KEY", "").strip()
        endpoint = os.environ.get("RUNPOD_ENDPOINT_ID", "").strip()
        bucket = os.environ.get("RECON_S3_BUCKET", "").strip()
        region = os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"
        if not api or not endpoint or not bucket:
            raise ProviderError("RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID, and RECON_S3_BUCKET are required")
        if not _RUNPOD_ENDPOINT_RE.fullmatch(endpoint):
            raise ProviderError("RUNPOD_ENDPOINT_ID has an invalid format")
        try:
            timeout = int(os.environ.get("RECON_RUNPOD_TIMEOUT", "3600"))
        except ValueError as exc:
            raise ProviderError("RECON_RUNPOD_TIMEOUT must be an integer") from exc
        if not MIN_RUNPOD_TIMEOUT <= timeout <= MAX_RUNPOD_TIMEOUT:
            raise ProviderError(
                f"RECON_RUNPOD_TIMEOUT must be between {MIN_RUNPOD_TIMEOUT} and {MAX_RUNPOD_TIMEOUT} seconds"
            )
        return api, endpoint, bucket, region, timeout

    def available(self) -> tuple[bool, str]:
        miss = [v for v in ("RUNPOD_API_KEY", "RUNPOD_ENDPOINT_ID", "RECON_S3_BUCKET")
                if not os.environ.get(v)]
        if miss:
            return (False, "set " + ", ".join(miss) + " (deploy infra/reconstruction-runpod)")
        try:
            self._settings()
        except ProviderError as exc:
            return (False, str(exc))
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        _validate_capture_images(images, minimum=MIN_CAPTURE_IMAGES)
        return await asyncio.to_thread(self._run_blocking, images, work_dir)

    def _run_blocking(self, images: list[Path], work_dir: Path) -> Path:
        import time
        import uuid as _uuid
        import boto3  # lazy — only the RunPod path needs these
        import requests

        api, endpoint, bucket, region, timeout = self._settings()
        base = f"https://api.runpod.ai/v2/{endpoint}"
        hdr = {"Authorization": f"Bearer {api}", "Content-Type": "application/json"}

        s3 = boto3.client("s3", region_name=region)
        job_key = _uuid.uuid4().hex
        in_prefix = f"recon-inputs/{job_key}"
        out_key = f"recon-outputs/{job_key}/model{DELIVERY_SUFFIX}"
        url_ttl = timeout + 1800  # presigned URLs must outlive queue-wait + run
        image_urls = []
        for index, p in enumerate(images):
            key = f"{in_prefix}/{_staged_image_name(index, p)}"
            s3.upload_file(str(p), bucket, key)
            image_urls.append(s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=url_ttl))
        # Sign the PUT with no Content-Type so the worker's header can't break the
        # signature; the object's type is irrelevant (_store_splat re-stores it).
        output_put_url = s3.generate_presigned_url(
            "put_object", Params={"Bucket": bucket, "Key": out_key}, ExpiresIn=url_ttl)

        body = {"input": {"image_urls": image_urls, "output_put_url": output_put_url},
                "policy": {"executionTimeout": timeout * 1000,       # override RunPod's 10-min default
                           "ttl": (timeout + 600) * 1000}}
        job_id: str | None = None
        terminal = False
        try:
            r = requests.post(f"{base}/run", json=body, headers=hdr, timeout=60)
            r.raise_for_status()
            try:
                submit_payload = r.json()
            except ValueError as exc:
                raise ProviderError("RunPod /run returned invalid JSON") from exc
            job_id = str(submit_payload.get("id") or "").strip()
            if not job_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
                raise ProviderError("RunPod /run returned no valid job id")

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError("RunPod reconstruction did not finish within budget")
                time.sleep(min(15, remaining))
                response = requests.get(f"{base}/status/{job_id}", headers=hdr, timeout=30)
                response.raise_for_status()
                try:
                    st = response.json()
                except ValueError as exc:
                    raise ProviderError("RunPod /status returned invalid JSON") from exc
                status = str(st.get("status") or "").upper()
                if status == "COMPLETED":
                    terminal = True
                    if isinstance(st.get("output"), dict) and st["output"].get("error"):
                        raise ProviderError("RunPod reconstruction worker reported an error")
                    break
                if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    terminal = True
                    raise ProviderError(f"RunPod reconstruction ended with status {status}")
        except requests.RequestException as exc:
            raise ProviderError("RunPod API request failed") from exc
        finally:
            if job_id and not terminal:
                try:
                    cancel = requests.post(f"{base}/cancel/{job_id}", headers=hdr, timeout=30)
                    cancel.raise_for_status()
                except Exception:  # noqa: BLE001 - preserve the original provider failure
                    log.exception("Unable to cancel abandoned RunPod reconstruction job %s", job_id)

        # Accept either delivery format: a current image writes model.sog, an
        # image built before the format fix writes model.splat. Both render.
        out = _download_first_available(
            s3, bucket, out_key, work_dir, provider="RunPod"
        )
        _validate_artifact(out, provider="RunPod")
        return out


class OnComputeProvider(ReconstructionProvider):
    """Ocean Network C2D reconstruction (oncompute.ai) — decentralised GPU jobs.

    The path chosen after RunPod serverless proved undeployable (workers never
    left `initializing` across every image size and endpoint; see
    infra/reconstruction-runpod/ history). Ocean nodes run per-job containers
    with the image we already publish to GHCR.

    Transport is deliberately cloud-storage-free: capture images are uploaded to
    the node's own persistent storage over HTTP and mount inside the job at
    /data/persistentStorage/<bucket>/<file>. C2D containers run with networking
    DISABLED (`enableNetwork: false` on the target env), so nothing inside the
    job can fetch or push — the node stages inputs before start, and the result
    is read back with getComputeResult afterwards. The RunPod presigned-URL
    scheme does not carry over; do not try.

    Auth is one of two modes:

    - **JWT** (ONCOMPUTE_AUTH_TOKEN): node-minted at dashboard.oncompute.ai,
      passed as the Authorization header. Revocable, no key in the container —
      preferred when minting works.
    - **Operator key** (ONCOMPUTE_PRIVATE_KEY_FILE / ONCOMPUTE_PRIVATE_KEY): a
      dedicated EOA signs every command locally. Exists because token minting
      is broken for this deployment in practice — the owner's wallet is a
      Coinbase Smart Wallet whose ERC-1271 signatures the node's ecrecover
      cannot validate, and the node's createAuthToken command rejected even
      plain EOAs ("nonce signature mismatch"). The signed-command flow is the
      one path verified end-to-end against the live node (2026-08-12/13).

    The signing recipe, confirmed from the node's own handler source
    (validateTokenOrSignature): personal_sign(keccak256(utf8(
    consumerAddress + nonce + command))) with the *command constant* from
    PROTOCOL_COMMANDS, and nonce = (GET /api/services/nonce) + 1, strictly
    increasing per consumer.

    Env: ONCOMPUTE_NODE_URL         — the node's HTTP gateway
         ONCOMPUTE_AUTH_TOKEN       — JWT for that node (mode 1)
         ONCOMPUTE_PRIVATE_KEY_FILE — path to a 0600 file with the hex key (mode 2)
         ONCOMPUTE_PRIVATE_KEY      — inline hex key (mode 2 fallback; prefer the file)
         ONCOMPUTE_ENV_ID     — optional; pinned compute env id. Envs ROTATE on
                                node restart, so the default is to resolve one
                                at submit time and a pin is only for testing.
         RECON_ONCOMPUTE_TIMEOUT — seconds, default 3600 (node cap is 7200)
         ONCOMPUTE_IMAGE      — default ghcr.io/ydngemini/neoh-recon-runpod:latest

    Free-tier envs are CPU-only (1 vCPU): enough to prove the image boots, far
    too slow for a real reconstruction. Real jobs need the paid GPU tier, which
    requires escrow funding on the owner's account — available() cannot see the
    escrow state, so an unfunded paid job fails at submit with the node's error
    rather than being silently queued forever.
    """
    name = "oncompute"

    # Two facts this driver encodes, both verified against the live image on
    # the free tier (2026-08-12, job 6093b8d2…):
    # - pipeline.sh lives at /usr/local/bin (the Dockerfile puts only handler.py
    #   at /), so it is resolved via PATH rather than assumed at the root;
    # - /handler.py must NEVER be imported here — it calls
    #   runpod.serverless.start() at module level (the RunPod Hub validator
    #   requires that), which exits the process when no test_input.json exists.
    _DRIVER = (
        "import glob, pathlib, shutil, subprocess, sys\n"
        "out = pathlib.Path('/data/outputs'); out.mkdir(parents=True, exist_ok=True)\n"
        "staged = pathlib.Path('/tmp/recon_images'); staged.mkdir(parents=True, exist_ok=True)\n"
        "count = 0\n"
        "for src in sorted(glob.glob('/data/persistentStorage/*/*')):\n"
        "    src = pathlib.Path(src)\n"
        "    if src.is_file():\n"
        "        shutil.copy2(src, staged / src.name); count += 1\n"
        "if count == 0:\n"
        "    sys.exit('no input images were mounted from persistent storage')\n"
        "pipeline = shutil.which('pipeline.sh') or '/usr/local/bin/pipeline.sh'\n"
        "rc = subprocess.call(['bash', pipeline, str(staged), str(out / 'model.splat')])\n"
        "sys.exit(rc)\n"
    )

    @staticmethod
    def _settings() -> tuple[str, Optional[str], Optional[str], str, int]:
        """(node_url, auth_token, private_key, image, timeout) — exactly one of
        auth_token / private_key is non-None."""
        node_url = os.environ.get("ONCOMPUTE_NODE_URL", "").strip().rstrip("/")
        token = os.environ.get("ONCOMPUTE_AUTH_TOKEN", "").strip() or None
        image = os.environ.get(
            "ONCOMPUTE_IMAGE", "ghcr.io/ydngemini/neoh-recon-runpod:latest"
        ).strip()

        key: Optional[str] = None
        key_file = os.environ.get("ONCOMPUTE_PRIVATE_KEY_FILE", "").strip()
        if key_file:
            try:
                key = Path(key_file).read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                raise ProviderError(f"ONCOMPUTE_PRIVATE_KEY_FILE is unreadable: {exc}") from exc
        if key is None:
            key = os.environ.get("ONCOMPUTE_PRIVATE_KEY", "").strip() or None

        if not node_url or not (token or key):
            raise ProviderError(
                "ONCOMPUTE_NODE_URL plus either ONCOMPUTE_AUTH_TOKEN or "
                "ONCOMPUTE_PRIVATE_KEY_FILE is required"
            )
        if not node_url.startswith(("https://", "http://")):
            raise ProviderError("ONCOMPUTE_NODE_URL must be an http(s) URL")
        if token and key:
            # Ambiguity is a config bug: the two modes produce different
            # consumer identities, and jobs/buckets belong to a consumer.
            raise ProviderError(
                "set ONCOMPUTE_AUTH_TOKEN or an operator key, not both"
            )
        try:
            timeout = int(os.environ.get("RECON_ONCOMPUTE_TIMEOUT", "3600"))
        except ValueError as exc:
            raise ProviderError("RECON_ONCOMPUTE_TIMEOUT must be an integer") from exc
        if not MIN_RUNPOD_TIMEOUT <= timeout <= MAX_RUNPOD_TIMEOUT:
            raise ProviderError(
                f"RECON_ONCOMPUTE_TIMEOUT must be between {MIN_RUNPOD_TIMEOUT} and {MAX_RUNPOD_TIMEOUT} seconds"
            )
        return node_url, token, key, image, timeout

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("ONCOMPUTE_NODE_URL"):
            return (False, "set ONCOMPUTE_NODE_URL (+ ONCOMPUTE_AUTH_TOKEN or ONCOMPUTE_PRIVATE_KEY_FILE)")
        try:
            _, token, key, _, _ = self._settings()
        except ProviderError as exc:
            return (False, str(exc))
        if key is not None:
            try:
                import eth_account  # noqa: F401 — presence check only
            except ImportError:
                return (False, "operator-key mode needs eth-account (pip install eth-account)")
        return (True, "")

    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        _validate_capture_images(images, minimum=MIN_CAPTURE_IMAGES)
        return await asyncio.to_thread(self._run_blocking, images, work_dir)

    # -- HTTP plumbing, one method per node endpoint ------------------------

    @staticmethod
    def _headers(token: Optional[str]) -> dict:
        return {"Authorization": token} if token else {}

    class _CommandSigner:
        """Signs node commands with the operator key.

        The message the node verifies is
        ``consumerAddress + nonce + command`` — command being the exact
        PROTOCOL_COMMANDS constant, hashed with keccak256 and signed EIP-191.
        Nonces are per-consumer and strictly increasing, so every signature
        first asks the node for the current value; two concurrent signers for
        one key would race, which is why the provider signs sequentially.
        """

        def __init__(self, session, node_url: str, private_key: str):
            from eth_account import Account  # lazy — only key mode needs it

            self._session = session
            self._node_url = node_url
            self._account = Account.from_key(private_key)
            self.address = self._account.address

        def params(self, command: str) -> dict:
            from eth_account.messages import encode_defunct
            from eth_utils import keccak

            response = self._session.get(
                f"{self._node_url}/api/services/nonce",
                params={"userAddress": self.address},
                timeout=30,
            )
            response.raise_for_status()
            try:
                payload = response.json()
                current = int(payload.get("nonce") if isinstance(payload, dict) else payload)
            except (ValueError, TypeError) as exc:
                raise ProviderError("OnCompute nonce endpoint returned no integer") from exc

            nonce = str(current + 1)
            digest = keccak(text=f"{self.address}{nonce}{command}")
            signature = self._account.sign_message(encode_defunct(primitive=digest))
            return {
                "consumerAddress": self.address,
                "nonce": nonce,
                "signature": "0x" + signature.signature.hex().removeprefix("0x"),
            }

    def _pick_environment(self, session, node_url: str, token: str) -> str:
        """Resolve a usable compute env id at submit time.

        Pinning an id in config only works until the node restarts — env ids are
        derived from node state and rotate. A stale pin fails the job, so the
        pin is honoured when present (tests) but never required.
        """
        pinned = os.environ.get("ONCOMPUTE_ENV_ID", "").strip()
        if pinned:
            return pinned
        response = session.get(
            f"{node_url}/api/services/computeEnvironments",
            headers=self._headers(token), timeout=30,
        )
        response.raise_for_status()
        envs = response.json()
        if not isinstance(envs, list) or not envs:
            raise ProviderError("OnCompute node lists no compute environments")
        open_free = [
            e for e in envs
            if isinstance(e, dict) and isinstance(e.get("free"), dict)
            and not (e["free"].get("access") or {}).get("addresses")
        ]
        if not open_free:
            raise ProviderError("OnCompute node has no openly accessible free environment")
        return str(open_free[0]["id"])

    def _run_blocking(self, images: list[Path], work_dir: Path) -> Path:
        import time
        import requests

        node_url, token, key, image, timeout = self._settings()
        session = requests.Session()
        signer = (
            self._CommandSigner(session, node_url, key) if key is not None else None
        )

        def _auth(command: str) -> dict:
            """Per-call credentials: signed params in key mode, empty in JWT
            mode (where the Authorization header carries identity instead)."""
            return signer.params(command) if signer is not None else {}

        try:
            env_id = self._pick_environment(session, node_url, token)

            # Stage the capture in the node's persistent storage. The create
            # call carries auth in the JSON body; uploads carry it as query
            # params — that split mirrors the node's own HTTP routes.
            bucket_resp = session.post(
                f"{node_url}/api/services/persistentStorage/buckets",
                headers=self._headers(token),
                json={"accessLists": [], **_auth("persistentStorageCreateBucket")},
                timeout=30,
            )
            bucket_resp.raise_for_status()
            bucket_id = str((bucket_resp.json() or {}).get("bucketId") or "")
            if not bucket_id:
                raise ProviderError("OnCompute node did not return a storage bucket id")

            datasets = []
            for index, path in enumerate(images):
                file_name = _staged_image_name(index, path)
                upload = session.post(
                    f"{node_url}/api/services/persistentStorage/buckets/{bucket_id}/files/{file_name}",
                    headers={**self._headers(token), "Content-Type": "application/octet-stream"},
                    params=_auth("persistentStorageUploadFile"),
                    data=path.read_bytes(), timeout=120,
                )
                upload.raise_for_status()
                datasets.append({
                    "fileObject": {
                        "type": "nodePersistentStorage",
                        "bucketId": bucket_id,
                        "fileName": file_name,
                    }
                })

            body = {
                "environment": env_id,
                "datasets": datasets,
                "algorithm": {
                    "meta": {
                        "rawcode": self._DRIVER,
                        "container": {
                            "image": image.rsplit(":", 1)[0],
                            "tag": image.rsplit(":", 1)[1] if ":" in image else "latest",
                            "entrypoint": "python3 $ALGO",
                        },
                    }
                },
                **_auth("freeStartCompute"),
            }
            submit = session.post(
                f"{node_url}/api/services/freeCompute",
                headers=self._headers(token), json=body, timeout=60,
            )
            submit.raise_for_status()
            payload = submit.json()
            job = payload[0] if isinstance(payload, list) and payload else payload
            job_id = str((job or {}).get("jobId") or "")
            if not job_id:
                raise ProviderError("OnCompute freeCompute returned no job id")

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError("OnCompute reconstruction did not finish within budget")
                time.sleep(min(10, max(1, remaining)))
                status_resp = session.get(
                    f"{node_url}/api/services/compute",
                    headers=self._headers(token),
                    params={"jobId": job_id, **_auth("getComputeStatus")},
                    timeout=30,
                )
                status_resp.raise_for_status()
                rows = status_resp.json()
                row = rows[0] if isinstance(rows, list) and rows else rows
                status = int((row or {}).get("status") or 0)
                status_text = str((row or {}).get("statusText") or "").lower()
                # C2DStatusNumber: 70/71 = finished; failure texts are the
                # node's own contract for terminal errors.
                if status in (70, 71):
                    break
                if any(word in status_text for word in ("failed", "expired", "vulnerabilit", "quota exceeded")):
                    raise ProviderError(f"OnCompute job ended: {status_text[:200]}")

            result = session.get(
                f"{node_url}/api/services/computeResult",
                headers=self._headers(token),
                params={"jobId": job_id, "index": 0, **_auth("getComputeResult")},
                timeout=300,
            )
            result.raise_for_status()
            raw = result.content
        except requests.RequestException as exc:
            raise ProviderError("OnCompute node request failed") from exc

        out = work_dir / "model.splat"
        # Result index 0 is the outputs archive when more than one file was
        # produced, or the bare file when only one was. Handle both without
        # guessing from headers.
        if raw[:2] == b"\x1f\x8b" or raw[:5] == b"ustar" or (len(raw) > 262 and raw[257:262] == b"ustar"):
            import io
            import tarfile

            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                member = next(
                    (m for m in archive.getmembers() if m.name.endswith("model.splat")), None
                )
                if member is None:
                    raise ProviderError("OnCompute result archive contains no model.splat")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ProviderError("OnCompute result archive entry is unreadable")
                out.write_bytes(extracted.read())
        else:
            out.write_bytes(raw)

        _validate_artifact(out, provider="OnCompute")
        return out


# ---------------------------------------------------------------------------
# RunPod pods — rent a GPU VM per job, run the pipeline, hand back the .sog
# ---------------------------------------------------------------------------

_RUNPOD_REST = "https://rest.runpod.io/v1"
_RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"

#: Indirection so a test can expire the provisioning window without moving
#: time.monotonic for the event loop as well.
def _now() -> float:
    return time.monotonic()

#: Every pod this provider creates is named `neoh-recon-<epoch>-<rand>`.
#:
#: The prefix lets the reaper tell our pods from anything the operator started
#: by hand, so a sweep can never collect an interactive session. The epoch is
#: there because the Pod API returns no creation timestamp at all — encoding it
#: in the name is what makes a leaked pod's age knowable without extra state.
POD_NAME_PREFIX = "neoh-recon-"

#: Preference order, cheapest-adequate first. `gpuTypeIds` takes a list and
#: RunPod picks the first available, so this degrades on its own when a tier is
#: sold out. 24 GB is the floor that matters: splatfacto peaks at 12-22 GB
#: depending on gaussian count, while COLMAP needs only 1-3 GB.
#:
#: Prices measured live 2026-08-23: A5000 $0.16/hr, 3090 $0.22, 4090 $0.34,
#: A40 $0.35. docs/runpod-pods-runbook.md called the 3090 the cheapest 24 GB
#: card, which the A5000 undercuts.
#: Widened 2026-08-24 after a live run was refused outright: RunPod answered
#: "This machine does not have the resources to deploy your pod" for the
#: four-card list at the default 40 GB container disk, while the SAME request
#: with a longer list was accepted immediately. `gpuTypeIds` is matched against
#: whatever is free RIGHT NOW, so a short list is not a cheaper choice, it is a
#: narrower chance of being placed at all. The 48 GB cards are kept last: they
#: cost more per hour but are the ones actually idle when the 24 GB tiers are
#: sold out, and an hour of A6000 beats an hour of not running.
_DEFAULT_POD_GPUS = (
    "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 3090",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA L4",
    "NVIDIA A40",
    "NVIDIA RTX A6000",
)

#: How many times to try terminating a pod before giving up and shouting.
#: Cheap: the call is idempotent, and the alternative to retrying is a machine
#: that bills until something else notices.
_TERMINATE_ATTEMPTS = 5

#: Lines of pipeline output kept for a failure report. Enough to carry a python
#: traceback and the stage markers around it, small enough to hold in memory
#: for the whole run.
_PIPELINE_TAIL_LINES = 400


def _is_already_gone(exc: Exception) -> bool:
    """Did this failure mean the pod does not exist? Then the job is done.

    A 404 on DELETE is success, not an error - the pod may have been collected
    by a sweep, by another replica, or by an earlier attempt whose response was
    lost. Treating it as a failure would retry four more times and then log an
    alarming line about a pod that is not there.
    """
    text = str(exc)
    return "404" in text or "not found" in text.lower()


#: Floor on a pod's advertised network speed, in Mbps, applied at placement.
#:
#: Deliberately LOW, and it is not the real defence. RunPod handed out a machine
#: that took the capture at 17 KB/s — 0.14 Mbps effective — but its advertised
#: speed was never observed, so there is no evidence a high floor would have
#: excluded it, and a high floor demonstrably narrows placement, which is a
#: failure that HAS been observed ("no free machine matching this request").
#: So this is a sanity check against a host advertising broken connectivity,
#: nothing more; POD_UPLOAD_BUDGET_SHARE is what actually catches a machine
#: that advertises well and delivers badly.
POD_MIN_MBPS = 10.0

#: Share of a job's budget that staging the capture may consume before the pod
#: is written off. A machine that cannot receive 60 images in that time will not
#: train on them either, and the sooner it is abandoned the sooner a retry lands
#: somewhere healthy.
POD_UPLOAD_BUDGET_SHARE = 0.25

#: Floor under that share, so a short budget still lets a healthy machine
#: receive its inputs — a fast pod stages 60 images in about 90 seconds.
POD_UPLOAD_MIN_SECONDS = 120.0

#: Fallback hourly rate for the cost ceiling, used only when RunPod does not
#: report costPerHr for the pod it just created. Deliberately pessimistic: an
#: over-estimate ends the job early, an under-estimate overspends.
_POD_FALLBACK_HOURLY = 0.40

#: Pinned for the same reason as everything else here: the pod image ships no
#: node at all, Ubuntu 22.04's apt has node 12, and splat-transform needs 22.
_POD_NODE_VERSION = "v22.23.2"

#: Pinned. A trainer resolved at run time is how version skew arrives with no
#: diff to review — one run was lost entirely to a pip-installed gsplat whose
#: examples were cloned from `main`.
_POD_GSPLAT_VERSION = "1.5.3"

#: Exhaustive matching is O(n^2): 43 images is 903 pairs and solved in minutes,
#: 128 is 8,128 pairs and exceeded 40 minutes on an L4. Image count, not GPU
#: tier, dominates what a reconstruction costs, so the capture is subsampled
#: before it is ever uploaded.
POD_TARGET_IMAGES = 60

#: The remote pipeline. Kept in the repo rather than only baked into an image so
#: the exact sequence is reviewable, and so a generic CUDA image still works.
#:
#: Two things here are load-bearing, both learned the hard way:
#:
#:  * COLMAP links Qt and builds a QGuiApplication even for CLI subcommands, so
#:    on a headless box it aborts with SIGABRT inside createPlatformIntegration()
#:    *before reading a single image* — and the failure looks like a corrupt
#:    capture, not a missing display. QT_QPA_PLATFORM=offscreen covers the CPU
#:    paths; GPU SIFT needs a real GL context, hence xvfb-run. Measured: with no
#:    display `colmap feature_extractor` exits rc=-6 in 0s; under xvfb it
#:    processed 43 images in 27s and the solve registered 43/43 at 0.51px.
#:  * The gsplat reference trainer imports viser and nerfview at module scope,
#:    before --disable-viewer is parsed, and `examples/datasets/` ships no
#:    __init__.py, so `import datasets.colmap` picks up HuggingFace's `datasets`
#:    package instead.
POD_PIPELINE = r"""
set -euo pipefail
cd /workspace

# Every stage announces itself, and the noisy installers are muted unless they
# fail. The provider reports the tail of the combined streams, and apt alone
# emits hundreds of "Setting up ..." lines — enough that a real failure was
# pushed clean out of the window. The first live run came back as
# "Pod pipeline failed (exit 1): -1) ... Setting up libxaw7", which says
# nothing about what broke.
say() { echo ">>> $*" >&2; }
quietly() {                      # $1 = label, rest = command
  local label="$1"; shift
  if ! "$@" >>/workspace/install.log 2>&1; then
    say "$label FAILED; last 30 lines:"
    tail -30 /workspace/install.log >&2
    return 1
  fi
}

if ! command -v colmap >/dev/null || ! command -v xvfb-run >/dev/null; then
  say "installing colmap + xvfb"
  quietly "apt-get update" apt-get -qq update
  quietly "apt-get install" apt-get -qq install -y colmap xvfb
fi
command -v colmap >/dev/null || { say "colmap is still not on PATH after install"; exit 2; }

# Node, installed HERE rather than discovered missing at the end.
#
# The pod image ships no node and no npm at all, and the conversion step is the
# very last line of the job — so a run did COLMAP, trained 7000 steps, exported
# its poses and wrote 743,656 points, and then died on
# "npm: command not found" with nothing to show for 35 minutes of GPU. Same
# lesson as the trainer's imports: check what the job needs before spending the
# expensive part, not after.
#
# A pinned static tarball rather than apt or NodeSource: Ubuntu 22.04 ships
# node 12, splat-transform needs >= 22, and an unpinned installer is how the
# toolchain changes underneath this with no diff to review.
if ! command -v splat-transform >/dev/null; then
  if ! command -v node >/dev/null || [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 22 ]; then
    say "installing node __NODE__"
    quietly "download node" curl -fsSL -o /tmp/node.tar.xz \
      "https://nodejs.org/dist/__NODE__/node-__NODE__-linux-x64.tar.xz"
    mkdir -p /opt/node
    quietly "unpack node" tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1
    export PATH="/opt/node/bin:$PATH"
  fi
  command -v npm >/dev/null || { say "npm is still not on PATH after installing node"; exit 2; }
  say "installing splat-transform __ST__"
  quietly "npm install splat-transform" npm install -g "@playcanvas/splat-transform@__ST__"
fi
export PATH="/opt/node/bin:$PATH"
command -v splat-transform >/dev/null || {
  say "splat-transform is not runnable; the job would die at the last line"; exit 2; }

export QT_QPA_PLATFORM=offscreen

# Xvfb is started directly rather than through xvfb-run.
#
# COLMAP's GPU SIFT needs an OpenGL context and therefore a display, but
# Ubuntu's xvfb-run wrapper is a /bin/sh script that fails on this image with
# "/usr/bin/xvfb-run: 184: 0: not found" before COLMAP is even reached — the
# first live pod run died there. Owning the server ourselves removes the
# wrapper, and a stale lock from a recycled pod is cleared rather than inherited.
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x24 >/workspace/xvfb.log 2>&1 &
export DISPLAY=:99
for _ in $(seq 1 30); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 1; done
[ -e /tmp/.X11-unix/X99 ] || { say "Xvfb never came up:"; tail -20 /workspace/xvfb.log >&2; exit 2; }
say "colmap $(colmap -h 2>&1 | head -1 || true); $(ls images | wc -l) images; DISPLAY=$DISPLAY"

# The trainer and everything it imports are installed and IMPORT-CHECKED before
# COLMAP runs, not after.
#
# Dependencies used to be a hand-written list — gsplat, viser, nerfview,
# splines, jaxtyping, tensorboard, tyro — which omitted cv2, pycolmap, imageio,
# torchmetrics, fused_ssim, sklearn, matplotlib and yaml. The job discovered
# that with `ModuleNotFoundError: No module named 'cv2'` *after* 27 minutes of
# feature extraction, matching and mapping had already been paid for.
#
# Two fixes, and the ordering is the more important one. The list now comes from
# the cloned tag's OWN requirements.txt, so it cannot drift from the trainer it
# feeds — and note pycolmap must be the rmbrualla fork pinned there, because
# PyPI's package of that name exposes a different API entirely. Then the whole
# import graph is exercised on a throwaway process, so a missing package costs
# three minutes instead of twenty-seven.
say "installing gsplat __GSPLAT__ and the trainer's dependencies"
python -c "import gsplat" 2>/dev/null || quietly "pip install gsplat" pip install -q "gsplat==__GSPLAT__"
if [ ! -d gs ]; then
  quietly "git clone gsplat" git clone --depth 1 --branch "v$(python -c 'import gsplat;print(gsplat.__version__.split("+")[0])')" \
      https://github.com/nerfstudio-project/gsplat.git gs
fi
touch gs/examples/datasets/__init__.py
quietly "pip install trainer requirements" pip install -q -r gs/examples/requirements.txt

say "checking the trainer imports"
( cd gs/examples && python -c "import simple_trainer" ) || {
  say "the trainer cannot import its own dependencies; not spending COLMAP time on it"
  exit 6
}

say "feature extraction"
colmap feature_extractor  --database_path db.db --image_path images \
                             --ImageReader.single_camera 1 --SiftExtraction.use_gpu 1
say "exhaustive matching"
colmap exhaustive_matcher --database_path db.db --SiftMatching.use_gpu 1
mkdir -p sparse
say "mapping"
colmap mapper             --database_path db.db --image_path images --output_path sparse
test -d sparse/0 || { echo "!! COLMAP registered no cameras" >&2; exit 3; }
colmap model_analyzer     --path sparse/0

say "training __STEPS__ steps"
# --save-ply is REQUIRED, and its absence is not visible until the very end.
#
# `save_ply` defaults to False and `--save-steps` controls .pt CHECKPOINTS, not
# point clouds — so a run trained all 7000 steps, reported PSNR 23.16 and
# SSIM 0.807 over 735,049 gaussians, rendered its trajectory video, exited 0,
# and left nothing for splat-transform to convert. Fifty-two minutes and a
# whole GPU hour to arrive at "training produced no .ply".
cd gs/examples && python simple_trainer.py default \
    --data-dir /workspace --data-factor 1 --result-dir /workspace/out \
    --max-steps __STEPS__ --save-steps __STEPS__ \
    --save-ply --ply-steps __STEPS__ --disable-viewer
cd /workspace

PLY=$(find /workspace/out -name '*.ply' | head -1)
test -n "$PLY" || { echo "!! training produced no .ply" >&2; exit 4; }

# Where the photographer stood, in the frame of the splat we are about to ship.
#
# COLMAP already solved this on the way here and the job used to discard it,
# which left estimate_up_axis's one decisive input with no way to be supplied in
# production. It settles exactly the rooms geometry cannot: a near-cubic
# bathroom, a stairwell taller than it is wide.
#
# Read back through gsplat's OWN Parser, with the arguments the trainer used,
# rather than from sparse/0 directly. The Parser normalises the scene — it
# recentres and rescales — so raw COLMAP centres do not belong to the same frame
# as the delivered model. Mixing the two does not fail loudly; it returns a
# confident up axis pointing somewhere else. Same rule as the point segmenter's
# features: whatever the consumer sees must come from the code that produced it.
say "exporting camera poses"
python - <<'POSES' || say "camera pose export failed (continuing without it)"
import json, sys, traceback
sys.path.insert(0, "/workspace/gs/examples")
try:
    from datasets.colmap import Parser
    parser = Parser(data_dir="/workspace", factor=1, normalize=True)
    centres = [[float(v) for v in c2w[:3, 3]] for c2w in parser.camtoworlds]
    json.dump({"frame": "trained", "positions": centres}, open("/workspace/cameras.json", "w"))
    print(f">>> exported {len(centres)} camera poses", file=sys.stderr)
except Exception:
    traceback.print_exc()
    raise SystemExit(1)
POSES

# A points-only cloud, because the delivered .sog is unreadable to the measurer.
#
# Delivery is .sog for good reasons — the viewer renders it and it is an order
# of magnitude smaller than PLY — but `parse_ply` cannot read a byte of it, so
# the floor plan path had no geometry to open. The full trained PLY would do
# (parse_ply ignores everything but x/y/z and opacity) at roughly 175 MB for
# 735k gaussians; this writes the same information at about a fifteenth of that.
#
# OPACITY IS CONVERTED, not copied. A 3DGS PLY stores it as a logit, and the
# consumer compares it against MIN_OPACITY = 0.35 as a probability. Copying the
# raw value would silently apply a threshold of sigmoid(0.35) = 0.59 instead —
# numbers that look right and mean something else, which is the failure mode
# this whole path keeps producing.
say "writing a points-only cloud for the plan path"
python - "$PLY" <<'POINTS' || say "points export failed (continuing without it)"
import sys, numpy as np

src = open(sys.argv[1], "rb").read()
head_end = src.find(b"end_header")
head_end = src.find(b"\n", head_end) + 1
header = src[:head_end].decode("ascii", "replace")

TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
         "uchar": "u1", "uint8": "u1", "char": "i1", "int8": "i1",
         "ushort": "<u2", "uint16": "<u2", "short": "<i2", "int16": "<i2",
         "uint": "<u4", "uint32": "<u4", "int": "<i4", "int32": "<i4"}
fields, count, in_vertex = [], 0, False
for line in header.splitlines():
    parts = line.split()
    if not parts:
        continue
    if parts[0] == "element":
        in_vertex = parts[1] == "vertex"
        if in_vertex:
            count = int(parts[2])
    elif parts[0] == "property" and in_vertex and parts[1] != "list":
        fields.append((parts[2], TYPES[parts[1]]))

table = np.frombuffer(src[head_end:head_end + np.dtype(fields).itemsize * count],
                      dtype=np.dtype(fields))
out = np.empty(count, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                             ("opacity", "<f4")])
out["x"], out["y"], out["z"] = table["x"], table["y"], table["z"]
if "opacity" in table.dtype.names:
    logit = table["opacity"].astype("float64")
    out["opacity"] = 1.0 / (1.0 + np.exp(-np.clip(logit, -30, 30)))
else:
    out["opacity"] = 1.0

with open("/workspace/points.ply", "wb") as handle:
    handle.write(
        b"ply\nformat binary_little_endian 1.0\n"
        + f"element vertex {count}\n".encode()
        + b"property float x\nproperty float y\nproperty float z\n"
        + b"property float opacity\nend_header\n"
    )
    handle.write(out.tobytes())
print(f">>> wrote {count} points", file=sys.stderr)
POINTS

say "converting to .sog"
# .sog, never .splat: splat-transform lists .splat input-only in every released
# version, so asking it to write one fails on every real run.
splat-transform "$PLY" /workspace/model.sog
test -s /workspace/model.sog || { echo "!! conversion produced no .sog" >&2; exit 5; }
echo "OK $(stat -c%s /workspace/model.sog) bytes"
"""


#: Fetches its own inputs and posts its own result, because in this transport
#: nothing ever connects *to* the pod. The pod holds two capability URLs and no
#: credentials: a read SAS per input and a write SAS for exactly one output key.
#:
#: The script itself arrives the same way rather than being embedded in the
#: container command — RunPod's dockerStartCmd is not the place for a hundred
#: lines of shell, and a long argv is a truncation bug waiting to happen.
POD_BLOB_BOOTSTRAP = r"""
set -euo pipefail
mkdir -p /workspace/images
cd /workspace

curl -fsSL "$NEOH_MANIFEST_URL" -o /workspace/manifest.txt
n=0
while IFS= read -r url; do
  [ -n "$url" ] || continue
  n=$((n+1))
  curl -fsSL "$url" -o "$(printf '/workspace/images/%04d.jpg' "$n")"
done < /workspace/manifest.txt
echo ">> fetched $n source images"

bash /workspace/pipeline.sh

# x-ms-blob-type is required by Azure and ignored by S3, so one PUT covers both.
curl -fsS -X PUT -T /workspace/model.sog      -H "x-ms-blob-type: BlockBlob"      -H "Content-Type: application/octet-stream"      "$NEOH_OUTPUT_URL"
echo ">> uploaded"

# Camera poses, if the pipeline produced them. Uploaded second and never
# allowed to fail the job: the reconstruction is the deliverable, this is an
# accelerator for one downstream estimate.
if [ -s /workspace/cameras.json ] && [ -n "${NEOH_POSES_URL:-}" ]; then
  curl -fsS -X PUT -T /workspace/cameras.json         -H "x-ms-blob-type: BlockBlob"         -H "Content-Type: application/json"         "$NEOH_POSES_URL" && echo ">> uploaded camera poses" || echo ">> camera pose upload failed (ignored)"
fi

if [ -s /workspace/points.ply ] && [ -n "${NEOH_POINTS_URL:-}" ]; then
  curl -fsS -X PUT -T /workspace/points.ply         -H "x-ms-blob-type: BlockBlob"         -H "Content-Type: application/octet-stream"         "$NEOH_POINTS_URL" && echo ">> uploaded point cloud" || echo ">> point cloud upload failed (ignored)"
fi
"""


def _subsample_capture(images: list[Path], target: int) -> list[Path]:
    """Evenly thin a capture to `target` frames, keeping coverage.

    Evenly spaced rather than truncated: a walk-through video sampled at 2fps
    produces frames in spatial order, so taking the first N would reconstruct
    one room in detail and leave the rest of the house unsolved.
    """
    if target <= 0 or len(images) <= target:
        return list(images)
    step = len(images) / target
    return [images[min(len(images) - 1, int(i * step))] for i in range(target)]


def _adopt_camera_poses(artifact: Path, downloaded: Path) -> None:
    """Move the pod's cameras.json into the sidecar the plan path looks for.

    The pod writes a minimal file; `capture_sidecars` owns the on-disk contract —
    the version, the frame check and the minimum count — so the payload is
    re-emitted through it rather than copied. That way there is exactly one
    place that decides what a valid sidecar is.
    """
    try:
        payload = json.loads(downloaded.read_text())
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        written = capture_sidecars.write(
            artifact,
            payload.get("positions") or [],
            frame=payload.get("frame") or capture_sidecars.FRAME_TRAINED,
        )
        if written:
            log.info(
                "Recorded %d camera poses beside %s",
                len(payload.get("positions") or []), artifact.name,
            )
    except (ValueError, OSError) as exc:
        # Absent or unreadable poses are a normal outcome, not an incident: the
        # plan path already handles having none. Logged plainly rather than as a
        # traceback so it does not read like the reconstruction went wrong.
        log.info("Ignoring unusable camera poses from the pod (%s)", exc)
    finally:
        downloaded.unlink(missing_ok=True)


def _pipeline_failure(result) -> str:
    """The pod's own account of what broke, with the streams kept apart.

    Concatenating stdout and stderr and taking the last 1200 characters made
    every failure look the same, because whichever stream ended last filled the
    window — in practice the installer, whose output is enormous and never the
    reason. stderr carries the `>>>` stage markers and the real error, so it is
    reported first and given the most room.
    """
    err = (result.stderr or "").strip()
    out = (result.stdout or "").strip()
    stage = ""
    for line in reversed(err.splitlines()):
        if line.startswith(">>> "):
            stage = f" during: {line[4:]}"
            break
    parts = [f"Pod pipeline failed (exit {result.exit_status}){stage}"]
    if err:
        parts.append(f"stderr: ...{err[-2000:]}")
    if out:
        parts.append(f"stdout: ...{out[-600:]}")
    return "\n".join(parts)


def _pod_placement_error(response, settings: dict) -> str:
    """Turn RunPod's placement refusal into something an operator can act on.

    "This machine does not have the resources to deploy your pod" and "There
    are no instances currently available" both read like our request was
    malformed. Neither is: they mean nothing matching is free this minute, and
    the fix is to widen the pool or ask for less disk — measured, both work.
    The raw text names neither the pool that was tried nor the knob that changes
    it, so a caller reads it as an outage and waits.
    """
    text = str(response)
    capacity = (
        "does not have the resources" in text
        or "no instances currently available" in text
    )
    if not capacity:
        return f"RunPod did not return a pod id: {text[:200]}"
    return (
        "RunPod had no free machine matching this request: "
        f"{', '.join(settings['gpu_ids'])} on {settings['cloud_type']} cloud with "
        f"{settings['disk_gb']} GB of container disk and at least "
        f"{settings['min_mbps']:.0f} Mbps. This is capacity, not "
        "configuration — the same request is accepted minutes later. Widen the "
        "pool with RECON_POD_GPU_IDS, lower RECON_POD_DISK_GB or "
        "RECON_POD_MIN_MBPS, or set RECON_POD_CLOUD_TYPE=COMMUNITY, then retry."
    )


def _pod_name() -> str:
    """`neoh-recon-<epoch>-<rand>` — see POD_NAME_PREFIX for why the epoch."""
    return f"{POD_NAME_PREFIX}{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _pod_age_seconds(name: str) -> Optional[int]:
    """Age from the name, or None when it does not carry a timestamp.

    None means "cannot tell", and the reaper leaves those alone rather than
    guessing — terminating a pod on an unreadable name would be the one bug
    worse than leaking one.
    """
    if not name.startswith(POD_NAME_PREFIX):
        return None
    stamp = name[len(POD_NAME_PREFIX):].split("-", 1)[0]
    try:
        created = int(stamp)
    except ValueError:
        return None
    age = int(time.time()) - created
    return age if age >= 0 else None


class PodProvider(ReconstructionProvider):
    """Reconstruction on a rented RunPod GPU VM, start to finish, unattended.

    Distinct from RunPodProvider, which targets RunPod *serverless* — a surface
    that is dead for this account (workers never left `initializing`; the
    endpoint was deleted 2026-08-14) and which stages captures through S3
    presigned URLs. That staging could never work here: RECON_S3_BUCKET is unset
    and ORACLE_STORAGE_BACKEND defaults to azure-files, so adding credits would
    not have made it run. This provider moves bytes over SSH and touches no
    object store.

    Lifecycle: create pod -> wait for SSH -> push images -> run the pipeline ->
    pull the .sog -> **terminate**. Termination is in a `finally` and is also
    swept by `reap_stale_pods`, because a pod bills by the hour whether or not
    it computes and a leak is completely silent: an idle 3090 left running
    overnight is about $5, and nothing in the product would show it.
    """

    name = "runpod_pod"
    produces = "captured"

    # -- configuration ------------------------------------------------------
    @staticmethod
    def _settings() -> dict:
        api = os.environ.get("RUNPOD_API_KEY", "").strip()
        if not api:
            raise ProviderError("RUNPOD_API_KEY is required")

        image = os.environ.get(
            "RECON_POD_IMAGE",
            "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ).strip()
        if not image:
            raise ProviderError("RECON_POD_IMAGE must not be empty")

        def _num(var: str, default: str, lo: float, hi: float, cast=float):
            raw = os.environ.get(var, default)
            try:
                value = cast(raw)
            except (TypeError, ValueError) as exc:
                raise ProviderError(f"{var} must be a number") from exc
            if not lo <= value <= hi:
                raise ProviderError(f"{var} must be between {lo} and {hi}")
            return value

        gpus = [g.strip() for g in os.environ.get("RECON_POD_GPU_IDS", "").split(",") if g.strip()]
        cloud = os.environ.get("RECON_POD_CLOUD_TYPE", "SECURE").strip().upper() or "SECURE"
        if cloud not in ("SECURE", "COMMUNITY"):
            raise ProviderError("RECON_POD_CLOUD_TYPE must be SECURE or COMMUNITY")

        # ssh is primary: it streams, needs no object store, and works on the
        # default azure-files backend. blob is the fallback for a deployment
        # that cannot open outbound 22 to RunPod.
        transport = (os.environ.get("RECON_POD_TRANSPORT", "ssh").strip().lower() or "ssh")
        if transport not in ("ssh", "blob"):
            raise ProviderError("RECON_POD_TRANSPORT must be 'ssh' or 'blob'")

        return {
            "api_key": api,
            "image": image,
            "gpu_ids": gpus or list(_DEFAULT_POD_GPUS),
            "disk_gb": _num("RECON_POD_DISK_GB", "40", 20, 500, int),
            "volume_gb": _num("RECON_POD_VOLUME_GB", "0", 0, 500, int),
            # 90 minutes was too tight for the work, measured. The pipeline
            # reached conversion in 3,103s before the trainer's full
            # requirements were installed from source; those add CUDA
            # extension builds (fused-ssim, fused-bilagrid) and the next run
            # was still going at 5,400s. Raised to two hours so the CLOCK stops
            # being the binding constraint and `max_cost` is — a ceiling in
            # dollars is the one that means something, and at $0.74/hr two
            # hours is $1.48 against a $2.00 cap, so money still bites first.
            "timeout": _num("RECON_POD_TIMEOUT", "7200", MIN_RUNPOD_TIMEOUT, MAX_RUNPOD_TIMEOUT, int),
            # A ceiling on one job, in dollars. The pod is terminated when the
            # budget is spent even if training has not converged, so a wedged
            # job costs a known amount rather than an unbounded one.
            "max_cost": _num("RECON_POD_MAX_COST_USD", "2.00", 0.05, 100.0),
            # Refuse to start below this. An empty or negative balance is a
            # permanent, fixable condition and must be reported as one.
            "min_balance": _num("RECON_POD_MIN_BALANCE_USD", "1.00", 0.0, 1000.0),
            "min_mbps": _num("RECON_POD_MIN_MBPS", str(POD_MIN_MBPS), 0.0, 10000.0),
            "cloud_type": cloud,
            "transport": transport,
            "steps": _num("RECON_POD_STEPS", "7000", 500, 60000, int),
        }

    # -- HTTP ---------------------------------------------------------------
    @staticmethod
    def _rest(api_key: str, method: str, path: str, *, json_body=None, timeout: int = 60):
        import requests  # lazy — only this path needs it

        response = requests.request(
            method,
            f"{_RUNPOD_REST}{path}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=json_body,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise ProviderError(
                f"RunPod {method} {path} failed ({response.status_code}): {response.text[:300]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    @classmethod
    def _balance(cls, api_key: str) -> float:
        import requests

        response = requests.post(
            _RUNPOD_GRAPHQL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": "query { myself { clientBalance } }"},
            timeout=30,
        )
        if response.status_code >= 400:
            raise ProviderError(f"RunPod balance query failed ({response.status_code})")
        payload = response.json()
        if payload.get("errors"):
            raise ProviderError(f"RunPod rejected the API key: {str(payload['errors'])[:200]}")
        value = ((payload.get("data") or {}).get("myself") or {}).get("clientBalance")
        if value is None:
            raise ProviderError("RunPod returned no balance for this account")
        return float(value)

    # -- readiness ----------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        """(ready, reason-if-not).

        The balance is read live rather than inferred from configuration.
        RunPodProvider checked only that env vars were *shaped* correctly, so it
        reported ready and then failed mid-job — which reads as an outage rather
        than the fixable billing state it is. Same distinction the Regrid fix
        drew between an expired credential and a provider being down.
        """
        try:
            settings = self._settings()
        except ProviderError as exc:
            return (False, str(exc))

        if settings["transport"] == "ssh":
            try:
                import asyncssh  # noqa: F401  - presence check only
            except ImportError:
                return (
                    False,
                    "asyncssh is not installed (pip install asyncssh), or set "
                    "RECON_POD_TRANSPORT=blob to hand the job over object storage",
                )
        else:
            # Checked here rather than discovered mid-job: azure-files has no
            # URL to hand out, so a pod could never return its result and the
            # failure would look like a reconstruction that silently never
            # finished.
            import object_storage

            if not object_storage.is_configured():
                return (False, "RECON_POD_TRANSPORT=blob needs ORACLE_STORAGE_BACKEND configured")
            if object_storage.BACKEND == "azure-files":
                return (
                    False,
                    "RECON_POD_TRANSPORT=blob cannot use ORACLE_STORAGE_BACKEND="
                    "azure-files (a mounted share has no URL to hand a pod). Use "
                    "azure-blob or s3, or the ssh transport.",
                )

        try:
            balance = self._balance(settings["api_key"])
        except ProviderError as exc:
            return (False, str(exc))
        except Exception as exc:  # noqa: BLE001 - network/parse: report, don't crash the poll
            return (False, f"could not read the RunPod balance: {exc}")

        if balance < settings["min_balance"]:
            return (
                False,
                f"RunPod balance is ${balance:.2f}, below the "
                f"${settings['min_balance']:.2f} minimum — add credits at "
                f"runpod.io/console/user/billing",
            )
        return (True, "")

    # -- the job ------------------------------------------------------------
    async def reconstruct(self, images: list[Path], work_dir: Path) -> Path:
        _validate_capture_images(images, minimum=MIN_CAPTURE_IMAGES)
        settings = self._settings()

        # Subsampled here rather than on the pod: matching cost is quadratic in
        # image count, so trimming before upload is the biggest lever on price.
        staged = _subsample_capture(images, POD_TARGET_IMAGES)
        if len(staged) < len(images):
            log.info(
                "Subsampled capture from %d to %d images (exhaustive matching is O(n^2))",
                len(images), len(staged),
            )

        # Appended by _launch the moment RunPod returns an id, before anything
        # can fail. Binding the id from _launch's *return value* left a window
        # where the pod existed but the name did not — a timeout, a network
        # error mid-poll or a cancellation in there leaked a billing pod.
        launched: list[str] = []
        try:
            if settings["transport"] == "blob":
                return await self._run_via_blob(settings, launched, staged, work_dir)
            host, port, key, hourly = await self._launch(settings, launched)
            return await self._run_on_pod(settings, host, port, key, hourly, staged, work_dir)
        finally:
            # Unconditional, and it must stay that way: a pod bills by the hour
            # whether or not it computes, and nothing surfaces a leaked one.
            for pod_id in launched:
                await asyncio.to_thread(self._terminate, settings["api_key"], pod_id)

    async def _launch(self, settings: dict, launched: list[str]):
        """Create a pod and wait for SSH. Returns (host, port, key, hourly).

        `launched` is an out-parameter on purpose: the pod id is recorded there
        the instant it exists, so the caller's `finally` can terminate it no
        matter where this method fails afterwards.
        """
        import asyncssh

        # An ephemeral keypair per job: nothing long-lived to store, rotate or
        # leak, and the key dies with the reconstruction that used it.
        key = asyncssh.generate_private_key("ssh-ed25519")
        public_key = key.export_public_key().decode().strip()

        created = await asyncio.to_thread(
            self._rest, settings["api_key"], "POST", "/pods",
            json_body={
                "name": _pod_name(),
                "imageName": settings["image"],
                "gpuTypeIds": settings["gpu_ids"],
                "gpuCount": 1,
                "containerDiskInGb": settings["disk_gb"],
                "volumeInGb": settings["volume_gb"],
                "ports": ["22/tcp"],
                "cloudType": settings["cloud_type"],
                "supportPublicIp": True,
                # Both directions: the capture goes to the pod and the model
                # comes back, and a host that is bad at one is usually bad at
                # both. Omitted entirely at 0 so the pool is not narrowed by a
                # filter the operator turned off.
                **({"minDownloadMbps": settings["min_mbps"],
                    "minUploadMbps": settings["min_mbps"]}
                   if settings["min_mbps"] > 0 else {}),
                # Pre-emption mid-training wastes the entire spend, and spot
                # matched on-demand at these tiers when it was measured.
                "interruptible": False,
                "env": {"PUBLIC_KEY": public_key},
            },
        ) or {}
        pod_id = created.get("id")
        if not pod_id:
            raise ProviderError(_pod_placement_error(created, settings))
        launched.append(pod_id)          # billing starts here, so ownership does too
        log.info("RunPod pod %s provisioning (%s)", pod_id, settings["image"])

        hourly = float(created.get("costPerHr") or 0) or _POD_FALLBACK_HOURLY
        deadline = _now() + min(900, settings["timeout"])
        while _now() < deadline:
            pod = await asyncio.to_thread(
                self._rest, settings["api_key"], "GET", f"/pods/{pod_id}"
            ) or {}
            host = pod.get("publicIp")
            port = (pod.get("portMappings") or {}).get("22")
            if host and port:
                hourly = float(pod.get("costPerHr") or 0) or hourly
                return host, int(port), key, hourly
            await asyncio.sleep(5)

        raise ProviderError(
            "RunPod pod never exposed SSH within the provisioning window "
            "(the pod is being terminated)"
        )

    async def _run_watching(self, conn, command, *, timeout, deadline, hourly, ceiling):
        """Run the pipeline while watching it, so a timeout can say WHERE.

        `conn.run` buffers every byte until the command finishes, so a job cut
        off by the budget guard reported only that it had been cut off — no
        stage, no output, nothing to tell whether the ceiling is too tight or
        the pipeline is wedged. Reading incrementally costs nothing and keeps
        the last `>>>` marker the script emitted.
        """
        seen = {"stage": "starting up"}
        tail: deque[str] = deque(maxlen=_PIPELINE_TAIL_LINES)

        async def _pump(stream):
            async for line in stream:
                text = line.rstrip("\n")
                tail.append(text)
                if text.startswith(">>> "):
                    seen["stage"] = text[4:]

        async with conn.create_process(command) as proc:
            readers = [
                asyncio.create_task(_pump(proc.stdout)),
                asyncio.create_task(_pump(proc.stderr)),
            ]
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise ProviderError(
                    f"Reconstruction exceeded its budget ({deadline}s at "
                    f"${hourly:.2f}/hr, ceiling ${ceiling:.2f}) during: "
                    f"{seen['stage']}. The pod is being terminated. Last output:"
                    f"\n...{chr(10).join(tail)[-1200:]}"
                ) from exc
            finally:
                for reader in readers:
                    reader.cancel()

        return types.SimpleNamespace(
            exit_status=proc.exit_status,
            stdout="\n".join(tail),
            stderr="\n".join(tail),
        )

    async def _run_on_pod(self, settings, host, port, key, hourly, images, work_dir) -> Path:
        import asyncssh

        # Bound by money as well as time: whichever ceiling comes first ends the
        # job, so a wedged run costs a known amount. Computed from the rate
        # RunPod actually charged for this pod, not an assumed one.
        budget_seconds = int((settings["max_cost"] / max(hourly, 0.01)) * 3600)
        deadline = min(settings["timeout"], budget_seconds)

        # The budget covers the WHOLE session, not just the training run.
        #
        # Only `conn.run` used to be bounded, which left the upload and the
        # download unbounded — and RunPod hands out machines whose network is
        # unusable. One pod took the images at 17 KB/s, an upload that would
        # have run for over an hour billing the whole time, and nothing would
        # have stopped it before the four-hour reaper. Every phase now draws
        # from one clock that starts when the pod does.
        started = _now()

        def _left(phase: str) -> float:
            remaining = deadline - (_now() - started)
            if remaining <= 0:
                raise ProviderError(
                    f"Reconstruction ran out of budget before {phase} "
                    f"({deadline}s at ${hourly:.2f}/hr, ceiling "
                    f"${settings['max_cost']:.2f}); the pod is being terminated"
                )
            return remaining

        #: A pod that cannot take its own inputs promptly will not finish the
        #: job either, and the sooner that is called the sooner a retry lands on
        #: a different machine. Staging gets a slice of the budget, not all of it.
        upload_budget = max(POD_UPLOAD_MIN_SECONDS, deadline * POD_UPLOAD_BUDGET_SHARE)

        last_error: Optional[Exception] = None
        conn = None
        for _ in range(30):  # sshd comes up a little after the port is mapped
            try:
                conn = await asyncssh.connect(
                    host, port=port, username="root", client_keys=[key],
                    # A pod is created fresh for this job and destroyed after it;
                    # there is no prior host key that pinning could compare to.
                    known_hosts=None,
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry until the window closes
                last_error = exc
                await asyncio.sleep(5)
        if conn is None:
            raise ProviderError(f"Could not open SSH to the RunPod pod: {last_error}")

        script = (
            POD_PIPELINE
            .replace("__STEPS__", str(settings["steps"]))
            .replace("__ST__", SPLAT_TRANSFORM_VERSION)
            .replace("__GSPLAT__", _POD_GSPLAT_VERSION)
            .replace("__NODE__", _POD_NODE_VERSION)
        )

        async def _upload() -> None:
            await conn.run("mkdir -p /workspace/images", check=True)
            async with conn.start_sftp_client() as sftp:
                for index, path in enumerate(images):
                    await sftp.put(
                        str(path), f"/workspace/images/{_staged_image_name(index, path)}"
                    )
                async with sftp.open("/workspace/run.sh", "w") as handle:
                    await handle.write(script)

        async with conn:
            staging = min(upload_budget, _left("the capture was uploaded"))
            try:
                await asyncio.wait_for(_upload(), timeout=staging)
            except asyncio.TimeoutError as exc:
                raise ProviderError(
                    f"This pod could not take the capture within {staging:.0f}s "
                    f"({len(images)} images) — its network is too slow to finish "
                    f"the job. Retrying will land on a different machine; the pod "
                    f"is being terminated."
                ) from exc

            result = await self._run_watching(
                conn, "bash /workspace/run.sh",
                timeout=_left("the pipeline finished"),
                deadline=deadline, hourly=hourly, ceiling=settings["max_cost"],
            )

            if result.exit_status != 0:
                raise ProviderError(_pipeline_failure(result))

            out = work_dir / f"model{DELIVERY_SUFFIX}"
            async with conn.start_sftp_client() as sftp:
                await asyncio.wait_for(
                    sftp.get("/workspace/model.sog", str(out)),
                    timeout=_left("the result was retrieved"),
                )
                # Best-effort, and it must stay that way: a reconstruction that
                # computed is not failed because an accelerator's sidecar is
                # missing. Older pods do not write one at all.
                try:
                    poses = work_dir / "cameras.json"
                    await sftp.get("/workspace/cameras.json", str(poses))
                except Exception:  # noqa: BLE001
                    log.info("Pod returned no camera poses; the plan path will infer up from geometry")
                else:
                    _adopt_camera_poses(out, poses)
                # The measurable geometry. Delivery is .sog and `parse_ply`
                # cannot read it, so without this the plan path has nothing to
                # open.
                try:
                    await sftp.get(
                        "/workspace/points.ply",
                        str(capture_sidecars.points_sidecar_for(out)),
                    )
                except Exception:  # noqa: BLE001
                    log.info("Pod returned no point cloud; this splat cannot be measured")

        _validate_artifact(out, provider="RunPod pod")
        return out

    # -- transport 2: object storage, for deploys with no SSH egress ---------
    async def _run_via_blob(self, settings, launched, images, work_dir) -> Path:
        """Run a job without ever connecting to the pod.

        SSH is the primary transport and the better one — it streams, it needs
        no object store, and it works on the default azure-files backend. But a
        deployment that cannot open outbound 22 to RunPod had no path at all,
        which is what this closes.

        The shape is deliberately capability-based: the pod is handed a read SAS
        per input and a write SAS for exactly one output key, and holds no
        credential of ours. It cannot list the container, read anything it was
        not given, or write anywhere else. That matters more here than over SSH,
        because in this direction we never talk to the machine again — we only
        wait for a blob to appear.
        """
        import object_storage

        job_key = uuid.uuid4().hex
        in_prefix = f"recon-inputs/{job_key}"
        out_key = f"recon-outputs/{job_key}/model{DELIVERY_SUFFIX}"
        poses_key = f"recon-outputs/{job_key}/cameras.json"
        points_key = f"recon-outputs/{job_key}/points.ply"
        # The URLs must outlive queue wait plus the whole run, or the pod loses
        # the ability to hand back a result it has already paid to compute.
        ttl = int(settings["timeout"]) + 3600

        def _stage() -> tuple[str, str, str, str, str]:
            urls = []
            for index, path in enumerate(images):
                key = f"{in_prefix}/{_staged_image_name(index, path)}"
                object_storage.put_file(key, path, "image/jpeg")
                urls.append(object_storage.signed_url(key, ttl))

            pipeline = (
                POD_PIPELINE
                .replace("__STEPS__", str(settings["steps"]))
                .replace("__ST__", SPLAT_TRANSFORM_VERSION)
                .replace("__GSPLAT__", _POD_GSPLAT_VERSION)
            .replace("__NODE__", _POD_NODE_VERSION)
            )
            object_storage.put_bytes(f"{in_prefix}/manifest.txt",
                                     "\n".join(urls).encode(), "text/plain")
            object_storage.put_bytes(f"{in_prefix}/pipeline.sh",
                                     pipeline.encode(), "text/x-shellscript")
            object_storage.put_bytes(f"{in_prefix}/bootstrap.sh",
                                     POD_BLOB_BOOTSTRAP.encode(), "text/x-shellscript")

            output_url = object_storage.presigned_put_url(out_key, ttl)
            poses_url = object_storage.presigned_put_url(poses_key, ttl)
            points_url = object_storage.presigned_put_url(points_key, ttl)
            if not output_url:
                raise ProviderError(
                    "The blob transport needs a storage backend that can issue a "
                    "write URL. ORACLE_STORAGE_BACKEND=azure-files cannot (a "
                    "mounted share has no URL), so use the ssh transport there."
                )
            return (
                object_storage.signed_url(f"{in_prefix}/manifest.txt", ttl),
                object_storage.signed_url(f"{in_prefix}/bootstrap.sh", ttl),
                output_url,
                poses_url or "",
                points_url or "",
            )

        manifest_url, bootstrap_url, output_url, poses_url, points_url = (
            await asyncio.to_thread(_stage)
        )

        created = await asyncio.to_thread(
            self._rest, settings["api_key"], "POST", "/pods",
            json_body={
                "name": _pod_name(),
                "imageName": settings["image"],
                "gpuTypeIds": settings["gpu_ids"],
                "gpuCount": 1,
                "containerDiskInGb": settings["disk_gb"],
                "volumeInGb": settings["volume_gb"],
                "cloudType": settings["cloud_type"],
                "interruptible": False,
                "env": {
                    "NEOH_MANIFEST_URL": manifest_url,
                    "NEOH_OUTPUT_URL": output_url,
                    "NEOH_POSES_URL": poses_url,
                    "NEOH_POINTS_URL": points_url,
                    "NEOH_BOOTSTRAP_URL": bootstrap_url,
                },
                # Tiny on purpose: the real script is fetched, not embedded.
                # `pipeline.sh` is pulled by the bootstrap from the same prefix.
                "dockerStartCmd": [
                    "bash", "-lc",
                    'curl -fsSL "$NEOH_BOOTSTRAP_URL" -o /workspace/bootstrap.sh && '
                    'curl -fsSL "${NEOH_MANIFEST_URL%manifest.txt}pipeline.sh" '
                    '-o /workspace/pipeline.sh && bash /workspace/bootstrap.sh',
                ],
            },
        ) or {}
        pod_id = created.get("id")
        if not pod_id:
            raise ProviderError(_pod_placement_error(created, settings))
        launched.append(pod_id)
        log.info("RunPod pod %s running (blob transport, job %s)", pod_id, job_key)

        hourly = float(created.get("costPerHr") or 0) or _POD_FALLBACK_HOURLY
        budget_seconds = int((settings["max_cost"] / max(hourly, 0.01)) * 3600)
        deadline = _now() + min(settings["timeout"], budget_seconds)

        # Nothing reports progress in this direction, so the finished artifact
        # appearing IS the completion signal.
        while _now() < deadline:
            try:
                payload = await asyncio.to_thread(object_storage.get_bytes, out_key)
            except Exception:  # noqa: BLE001 - not there yet is the normal case
                await asyncio.sleep(20)
                continue
            out = work_dir / f"model{DELIVERY_SUFFIX}"
            out.write_bytes(payload)
            # The same sidecar as the SSH transport, so which transport a
            # deployment happens to use is not visible downstream.
            try:
                poses = await asyncio.to_thread(object_storage.get_bytes, poses_key)
            except Exception:  # noqa: BLE001
                log.info("Pod returned no camera poses; the plan path will infer up from geometry")
            else:
                staged = work_dir / "cameras.json"
                staged.write_bytes(poses)
                _adopt_camera_poses(out, staged)
            try:
                cloud = await asyncio.to_thread(object_storage.get_bytes, points_key)
            except Exception:  # noqa: BLE001
                log.info("Pod returned no point cloud; this splat cannot be measured")
            else:
                capture_sidecars.points_sidecar_for(out).write_bytes(cloud)
            _validate_artifact(out, provider="RunPod pod")
            return out

        raise ProviderError(
            f"Reconstruction produced no artifact within its budget "
            f"({int(settings['max_cost'] / max(hourly, 0.01) * 3600)}s at "
            f"${hourly:.2f}/hr); the pod is being terminated"
        )

    # -- cleanup ------------------------------------------------------------
    @classmethod
    def _terminate(cls, api_key: str, pod_id: str) -> None:
        """Terminate a pod, retrying — this is the one call that bills forever.

        Every other request in this provider can fail and cost nothing: the job
        is lost and that is the end of it. This one is different. A single
        connect timeout to rest.runpod.io left a pod running after its budget
        guard had correctly fired, and it billed until a sweep noticed. The
        guard worked; the cleanup did not, and the failure was one dropped TCP
        connection.

        Retried with backoff, and a 404 counts as success: the pod being gone is
        the outcome asked for, whoever got there first.
        """
        last: Optional[Exception] = None
        for attempt in range(_TERMINATE_ATTEMPTS):
            try:
                cls._rest(api_key, "DELETE", f"/pods/{pod_id}")
                log.info("RunPod pod %s terminated", pod_id)
                return
            except Exception as exc:  # noqa: BLE001 - never mask the job's own failure
                if _is_already_gone(exc):
                    log.info("RunPod pod %s was already gone", pod_id)
                    return
                last = exc
                if attempt + 1 < _TERMINATE_ATTEMPTS:
                    time.sleep(min(2 ** attempt, 15))
        log.error(
            "FAILED to terminate RunPod pod %s after %d attempts (%s) - it is "
            "STILL BILLING. Terminate it at runpod.io/console/pods; a sweep will "
            "also collect it once it is older than the job ceiling.",
            pod_id, _TERMINATE_ATTEMPTS, last,
        )

    @classmethod
    def reap_stale_pods(cls, max_age_seconds: int = 4 * 3600) -> list[str]:
        """Terminate our own pods that outlived any plausible job.

        The `finally` in reconstruct covers a failing job; it cannot cover the
        backend being killed between creating a pod and reaching that block. In
        that window the pod bills indefinitely and nothing points at it.

        Only pods carrying POD_NAME_PREFIX are touched: an interactive pod the
        operator started by hand must never be collected by an automatic sweep.
        """
        settings = cls._settings()
        pods = cls._rest(settings["api_key"], "GET", "/pods") or []
        if isinstance(pods, dict):
            pods = pods.get("data") or []

        reaped: list[str] = []
        for pod in pods:
            age = _pod_age_seconds(pod.get("name") or "")
            if age is None or age < max_age_seconds:
                continue
            log.warning(
                "Reaping leaked RunPod pod %s (%s), age %ds",
                pod.get("id"), pod.get("name"), age,
            )
            cls._terminate(settings["api_key"], pod["id"])
            reaped.append(pod["id"])
        return reaped


_PROVIDERS = {
    "stub": StubProvider,
    "local": LocalGpuProvider,
    "cloud": CloudGpuProvider,
    "serverless": ServerlessProvider,
    "aws_batch": AwsBatchProvider,
    "aws": AwsBatchProvider,
    "runpod": RunPodProvider,
    # The pods path: rents a GPU VM per job rather than calling a serverless
    # endpoint. `runpod` above targets serverless, which is dead for this
    # account and stages through an S3 bucket that is not configured here.
    "runpod_pod": PodProvider,
    "pod": PodProvider,
    "oncompute": OnComputeProvider,
}


def get_provider() -> ReconstructionProvider:
    """The configured provider (RECONSTRUCTION_PROVIDER, default 'stub' for dev).

    The stub is refused outside development. It synthesises a checkerboard room
    and captures nothing, and its output is stored as ordinary media — only the
    provenance column stops the tour describing it as the actual home. Inheriting
    that default in production would mean shipping demo rooms to real customers.

    Refused here rather than at boot, because a deployment that does no 3D
    capture at all should still start: this turns capture into an honest 503
    ("not configured") instead of preventing the whole API from running.
    """
    name = os.environ.get("RECONSTRUCTION_PROVIDER", "stub").strip().lower()

    from config import IS_DEV

    if name == "stub" and not IS_DEV:
        return UnavailableProvider("stub (refused outside development)")

    cls = _PROVIDERS.get(name)
    return cls() if cls is not None else UnavailableProvider(name)
