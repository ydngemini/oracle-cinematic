#!/usr/bin/env python3
"""RunPod serverless handler — Gaussian-splat property reconstruction.

Runs the SAME recipe as the Neoh AWS Batch worker (infra/reconstruction/run.sh):
COLMAP poses (nerfstudio ns-process-data) -> train splatfacto (Apache-2.0 3DGS)
-> export .ply -> PlayCanvas splat-transform -> antimatter15 .splat. Only the
transport/orchestration changes: RunPod serverless /run + /status instead of
AWS Batch submit_job / describe_jobs. The compute core lives in pipeline.sh.

Input (job["input"]):
  image_urls      ["https://.../1.jpg", ...]   presigned S3 GET URLs
  output_put_url  "https://...signed-PUT..."   presigned S3 PUT URL
  return_splat_b64  true                        selftest only
  # tuning
  iters           int    splatfacto iterations (default $RECON_ITERS or 7000)
  # ops
  selftest        true   skip GPU work; emit a synthetic demo splat (boot check)

Output: {"gaussians": int, "bytes": int, "splat_s3"?: str, "splat_put"?: bool,
         "splat_b64"?: str, "selftest"?: bool, "disclosure": str}
On failure returns {"error": "..."} so RunPod marks the job FAILED gracefully.

The worker deliberately accepts only presigned S3 HTTPS URLs. It never receives
AWS credentials and cannot read or write arbitrary buckets.
"""
from __future__ import annotations

import base64
import collections
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

DISCLOSURE = (
    "AI-generated 3D reconstruction from photos; geometry may be incomplete or "
    "inaccurate. Not a measured survey or a substitute for an in-person showing."
)
MIN_IMAGES = 8
MAX_IMAGES = 300
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_INLINE_SPLAT_BYTES = 5 * 1024 * 1024
MIN_ITERS = 1000
MAX_ITERS = 30000
PIPELINE = "/usr/local/bin/pipeline.sh"


# --- transport: image source -----------------------------------------------
def _validate_presigned_s3_url(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError(f"{field} must be a valid presigned S3 URL")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    aws_domain = host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn")
    s3_label = any(label == "s3" or label.startswith("s3-") for label in host.split("."))
    if (
        parsed.scheme != "https"
        or not aws_domain
        or not s3_label
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError(f"{field} must target S3 over HTTPS")
    query_keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    required = {"x-amz-algorithm", "x-amz-credential", "x-amz-expires", "x-amz-signature"}
    if not required.issubset(query_keys):
        raise ValueError(f"{field} must be an AWS Signature V4 presigned URL")
    return value


def _looks_like_image(path: Path) -> bool:
    with path.open("rb") as image_file:
        head = image_file.read(16)
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    )


def _pull_urls(urls: list[str], dest: Path) -> int:
    if not isinstance(urls, list) or not urls:
        raise ValueError("image_urls must be a non-empty list")
    if len(urls) > MAX_IMAGES:
        raise ValueError(f"image_urls exceeds the {MAX_IMAGES} image limit")
    for i, url in enumerate(urls):
        safe_url = _validate_presigned_s3_url(url, f"image_urls[{i}]")
        ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
        if ext.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            ext = ".img"
        target = dest / f"img_{i:04d}{ext.lower()}"
        with requests.get(
            safe_url,
            stream=True,
            timeout=120,
            allow_redirects=False,
        ) as r:
            r.raise_for_status()
            declared_size = r.headers.get("Content-Length")
            if declared_size:
                try:
                    size_int = int(declared_size)
                except ValueError as exc:
                    raise ValueError(f"image_urls[{i}] returned an invalid Content-Length") from exc
                if size_int > MAX_IMAGE_BYTES:
                    raise ValueError(f"image_urls[{i}] exceeds the per-image byte limit")
            written = 0
            with target.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_IMAGE_BYTES:
                        raise ValueError(f"image_urls[{i}] exceeds the per-image byte limit")
                    f.write(chunk)
        if not _looks_like_image(target):
            raise ValueError(f"image_urls[{i}] did not return a supported image")
    return len(urls)


def _gather(job_input: dict, images: Path) -> int:
    if job_input.get("input_s3"):
        raise ValueError("input_s3 is disabled; use presigned image_urls")
    if job_input.get("image_urls"):
        return _pull_urls(job_input["image_urls"], images)
    raise ValueError("provide image_urls")


# --- transport: result sink -------------------------------------------------
def _push_put_url(path: Path, url: str) -> None:
    safe_url = _validate_presigned_s3_url(url, "output_put_url")
    with open(path, "rb") as f:
        r = requests.put(
            safe_url,
            data=f,
            headers={"Content-Type": "application/octet-stream"},
            timeout=300,
            allow_redirects=False,
        )
    r.raise_for_status()


def _emit(job_input: dict, splat: Path, *, selftest: bool) -> dict:
    out: dict = {}
    sank = False
    if job_input.get("output_s3"):
        raise ValueError("output_s3 is disabled; use a presigned output_put_url")
    if job_input.get("output_put_url"):
        _push_put_url(splat, job_input["output_put_url"])
        out["splat_put"] = True
        sank = True
    if job_input.get("return_splat_b64"):
        if not selftest:
            raise ValueError("return_splat_b64 is restricted to selftest jobs")
        if splat.stat().st_size > MAX_INLINE_SPLAT_BYTES:
            raise ValueError("selftest splat exceeds the inline response limit")
        out["splat_b64"] = base64.b64encode(splat.read_bytes()).decode("ascii")
        sank = True
    if not sank:
        raise ValueError("provide output_put_url (or return_splat_b64 for selftest)")
    return out


# --- synthetic splat for selftest (no GPU, no images) -----------------------
def _row(px, py, pz, sx, sy, sz, r, g, b, a=255) -> bytes:
    # 32-byte gsplat row: pos 3xf32 | scale 3xf32 | rgba 4xu8 | rot 4xu8 (identity).
    return struct.pack("<3f3f", px, py, pz, sx, sy, sz) + bytes((r, g, b, a, 255, 128, 128, 128))


def _write_demo_splat(path: Path, w=4.0, h=2.6, d=4.0, step=0.12) -> Path:
    rows = bytearray()
    y = 0.0
    while y <= h + 1e-6:  # four walls
        x = -w / 2
        while x <= w / 2 + 1e-6:
            rows.extend(_row(x, y, -d / 2, 0.05, 0.05, 0.012, 196, 184, 168))
            rows.extend(_row(x, y, d / 2, 0.05, 0.05, 0.012, 196, 184, 168))
            x += step
        y += step
    a = -w / 2
    while a <= w / 2 + 1e-6:  # floor
        b = -d / 2
        while b <= d / 2 + 1e-6:
            rows.extend(_row(a, 0.0, b, 0.05, 0.012, 0.05, 150, 140, 128))
            b += step
        a += step
    path.write_bytes(rows)
    return path


# --- pipeline ---------------------------------------------------------------
def _run_pipeline(images: Path, splat: Path, iters: str) -> tuple[int, str]:
    """Run pipeline.sh, tee its output to the RunPod worker log, keep an error tail."""
    import threading
    timeout_seconds = int(os.environ.get("RECON_TIMEOUT", "3600"))
    env = {**os.environ, "RECON_ITERS": iters}
    proc = subprocess.Popen(
        [PIPELINE, str(images), str(splat)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    tail: collections.deque[str] = collections.deque(maxlen=80)
    timer_triggered = [False]

    def timeout_kill():
        timer_triggered[0] = True
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait()

    timer = threading.Timer(timeout_seconds, timeout_kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)  # -> visible live in RunPod worker logs
            tail.append(line)
        proc.wait()
    finally:
        timer.cancel()
    if timer_triggered[0]:
        tail.append(f"[TIMEOUT] Pipeline killed after {timeout_seconds}s\n")
        return 124, "".join(tail)
    return proc.returncode, "".join(tail)


def _progress(job: dict, message: str) -> None:
    import runpod

    runpod.serverless.progress_update(job, message)


def _iterations(value: object) -> int:
    try:
        iterations = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("iters must be an integer") from exc
    if not MIN_ITERS <= iterations <= MAX_ITERS:
        raise ValueError(f"iters must be between {MIN_ITERS} and {MAX_ITERS}")
    return iterations


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, requests.RequestException):
        return "object transfer failed"
    if isinstance(exc, ValueError):
        return str(exc)[:300]
    if isinstance(exc, OSError):
        return "worker filesystem operation failed"
    return "worker execution failed"


def handler(job):
    if not isinstance(job, dict):
        return {"error": "job must be an object"}
    job_input = job.get("input") or {}
    if not isinstance(job_input, dict):
        return {"error": "input must be an object"}
    work = Path(tempfile.mkdtemp(prefix="recon-"))
    try:
        splat = work / "model.splat"
        selftest = job_input.get("selftest") is True

        if job_input.get("return_splat_b64") not in (None, False, True):
            raise ValueError("return_splat_b64 must be a boolean")
        if job_input.get("input_s3") or job_input.get("output_s3"):
            raise ValueError("direct S3 mode is disabled; use presigned URLs")
        if job_input.get("return_splat_b64") and not selftest:
            raise ValueError("return_splat_b64 is restricted to selftest jobs")
        if job_input.get("output_put_url"):
            _validate_presigned_s3_url(job_input["output_put_url"], "output_put_url")
        if not job_input.get("output_put_url") and not job_input.get("return_splat_b64"):
            raise ValueError("provide output_put_url (or return_splat_b64 for selftest)")

        if selftest:
            _progress(job, "selftest: writing demo splat")
            _write_demo_splat(splat)
            result = _emit(job_input, splat, selftest=True)
            size = splat.stat().st_size
            return {"gaussians": size // 32, "bytes": size,
                    "selftest": True, "disclosure": DISCLOSURE, **result}

        images = work / "images"
        images.mkdir()
        _progress(job, "downloading capture images")
        n = _gather(job_input, images)
        if n < MIN_IMAGES:
            return {"error": f"need >={MIN_IMAGES} images for reconstruction, got {n}"}

        iterations = _iterations(job_input.get("iters") or os.environ.get("RECON_ITERS", "7000"))
        _progress(job, f"reconstructing: COLMAP + splatfacto ({iterations} iters, {n} images)")
        rc, tail = _run_pipeline(images, splat, str(iterations))
        if rc != 0:
            return {"error": f"reconstruction failed (exit {rc})", "log": tail[-1500:]}
        if not splat.is_file() or splat.stat().st_size == 0 or splat.stat().st_size % 32 != 0:
            return {"error": "pipeline produced an invalid .splat", "log": tail[-1500:]}

        _progress(job, "uploading splat")
        result = _emit(job_input, splat, selftest=False)
        size = splat.stat().st_size
        return {"gaussians": size // 32, "bytes": size,
                "iters": iterations, "images": n,
                "disclosure": DISCLOSURE, **result}
    except Exception as exc:  # graceful FAILED without leaking presigned URLs
        return {"error": _safe_error(exc)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
