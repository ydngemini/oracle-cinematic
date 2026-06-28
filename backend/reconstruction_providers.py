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
import os
import shutil
import struct
from pathlib import Path
from typing import Optional

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


class ProviderError(RuntimeError):
    """Reconstruction failed in a way the worker should record + surface."""


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
        if not images:
            raise ProviderError("no capture images provided")
        img_dir = work_dir / "images"
        img_dir.mkdir(exist_ok=True)
        for p in images:
            shutil.copy(p, img_dir / p.name)
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

    if not images:
        raise ProviderError("no capture images provided")
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        form = aiohttp.FormData()
        for p in images:
            form.add_field("images", p.read_bytes(), filename=p.name, content_type="image/jpeg")
        async with s.post(url, data=form) as r:
            if r.status >= 400:
                raise ProviderError(f"submit HTTP {r.status}: {(await r.text())[:300]}")
            job = await r.json()
        job_id = job.get("job_id") or job.get("id")
        if not job_id:
            raise ProviderError(f"no job id in response: {job}")
        # poll
        for _ in range(REQUEST_TIMEOUT // 15):
            await asyncio.sleep(15)
            async with s.get(f"{url.rstrip('/')}/{job_id}") as r:
                st = await r.json()
            status = (st.get("status") or "").lower()
            if status in ("succeeded", "completed", "done"):
                dl = st.get("output_url") or st.get("ply_url") or st.get("splat_url")
                if not dl:
                    raise ProviderError("job done but no output_url")
                async with s.get(dl) as r:
                    data = await r.read()
                ext = ".splat" if dl.endswith(".splat") else ".ply"
                out = work_dir / f"remote{ext}"
                out.write_bytes(data)
                return out
            if status in ("failed", "error", "cancelled"):
                raise ProviderError(f"remote job failed: {st.get('error', status)}")
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


_PROVIDERS = {
    "stub": StubProvider,
    "local": LocalGpuProvider,
    "cloud": CloudGpuProvider,
    "serverless": ServerlessProvider,
}


def get_provider() -> ReconstructionProvider:
    """The configured provider (RECONSTRUCTION_PROVIDER, default 'stub' for dev)."""
    name = os.environ.get("RECONSTRUCTION_PROVIDER", "stub").lower()
    cls = _PROVIDERS.get(name, StubProvider)
    return cls()
