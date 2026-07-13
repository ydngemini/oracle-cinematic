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

# Reuse the existing output dir + AI-disclosure contract from the spatial agent.
try:
    from spatial_agent import SPLAT_OUTPUT_DIR, SPATIAL_AI_DISCLOSURE  # type: ignore
except Exception:  # spatial_agent optional at import time
    SPLAT_OUTPUT_DIR = Path(os.environ.get("ORACLE_SPLAT_DIR", "/tmp/oracle_splats"))
    SPATIAL_AI_DISCLOSURE = (
        "AI-generated 3D reconstruction from photos; geometry may be incomplete or "
        "inaccurate. Not a measured survey or a substitute for an in-person showing."
    )

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


_PROVIDERS = {
    "stub": StubProvider,
    "local": LocalGpuProvider,
    "cloud": CloudGpuProvider,
    "serverless": ServerlessProvider,
    "aws_batch": AwsBatchProvider,
    "aws": AwsBatchProvider,
    "runpod": RunPodProvider,
}


def get_provider() -> ReconstructionProvider:
    """The configured provider (RECONSTRUCTION_PROVIDER, default 'stub' for dev)."""
    name = os.environ.get("RECONSTRUCTION_PROVIDER", "stub").strip().lower()
    cls = _PROVIDERS.get(name)
    return cls() if cls is not None else UnavailableProvider(name)
