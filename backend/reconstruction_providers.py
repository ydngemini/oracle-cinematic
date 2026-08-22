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
import logging
import mimetypes
import os
import re
import shutil
import struct
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
MAX_RUNPOD_TIMEOUT = 7200
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


async def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: int = REQUEST_TIMEOUT) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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
        await _run(["colmap", "feature_extractor", "--database_path", str(db), "--image_path", str(img_dir)])
        await _run(["colmap", "exhaustive_matcher", "--database_path", str(db)])
        await _run(["colmap", "mapper", "--database_path", str(db), "--image_path", str(img_dir), "--output_path", str(sparse)])
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
                ext = ".splat" if urlparse(dl).path.lower().endswith(".splat") else ".ply"
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
                if ext == ".splat" and written % 32 != 0:
                    raise ProviderError("remote job returned an invalid .splat artifact")
                if ext == ".ply":
                    with out.open("rb") as artifact_file:
                        if artifact_file.read(3) != b"ply":
                            raise ProviderError("remote job returned an invalid .ply artifact")
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
        out_key = f"recon-outputs/{job_key}/model.splat"
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

        out = work_dir / "model.splat"
        s3.download_file(bucket, out_key, str(out))
        if not out.is_file() or out.stat().st_size == 0 or out.stat().st_size % 32 != 0:
            raise ProviderError("AWS Batch produced an invalid .splat artifact")
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
        out_key = f"recon-outputs/{job_key}/model.splat"
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

        out = work_dir / "model.splat"
        s3.download_file(bucket, out_key, str(out))
        if not out.is_file() or out.stat().st_size == 0 or out.stat().st_size % 32 != 0:
            raise ProviderError("RunPod produced an invalid .splat artifact")
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

        if not out.is_file() or out.stat().st_size == 0 or out.stat().st_size % 32 != 0:
            raise ProviderError("OnCompute produced an invalid .splat artifact")
        return out


_PROVIDERS = {
    "stub": StubProvider,
    "local": LocalGpuProvider,
    "cloud": CloudGpuProvider,
    "serverless": ServerlessProvider,
    "aws_batch": AwsBatchProvider,
    "aws": AwsBatchProvider,
    "runpod": RunPodProvider,
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
