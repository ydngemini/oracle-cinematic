"""
Spatial Agent — Full-Auto 2D→3D Property Reconstruction Pipeline.

Pipeline:
  1. Source listing images (Zillow/Redfin scrape or staged photos)
  2. Run DUSt3R/MASt3R for multi-view dense reconstruction
  3. Train 3D Gaussian Splatting (7k iterations fast mode)
  4. Export .splat binary for the WebGL viewer
  5. Push SPLAT_READY via WebSocket

Supports:
  - GPU mode (CUDA) — DUSt3R ViTLarge, full-resolution, ~90s
  - CPU mode — DUSt3R ViTBase with reduced resolution, ~8min
  - Staged images — pre-downloaded photos in backend/staged_images/{id}/
  - Auto-source — Zillow/Redfin static image scraping (rate-limited)

Env vars:
  DUST3R_PATH         /opt/dust3r (default)
  GS_PATH             /opt/gaussian-splatting
  SPATIAL_DEVICE      cuda | cpu (auto-detected)
  SPATIAL_RESOLUTION  512 (default for GPU) | 224 (CPU)
  ZILLOW_COOKIE       Optional: session cookie for image scraping
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("oracle.spatial")

# Host layout: <repo>/oracle-app/public/splats. In the Docker image __file__
# is /app/spatial_agent.py, so the relative default resolves to /oracle-app —
# unwritable at container root. Env override + tmp fallback so a side feature
# can never crash the API at import time.
_DEFAULT_SPLAT_DIR = Path(__file__).parent.parent / "oracle-app" / "public" / "splats"
SPLAT_OUTPUT_DIR = Path(os.environ.get("ORACLE_SPLAT_DIR", str(_DEFAULT_SPLAT_DIR)))
try:
    SPLAT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    SPLAT_OUTPUT_DIR = Path("/tmp/oracle_splats")
    SPLAT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.warning(
        "Splat output dir not writable at default location — falling back to %s "
        "(set ORACLE_SPLAT_DIR to override).",
        SPLAT_OUTPUT_DIR,
    )

WORK_DIR = Path("/tmp/oracle_spatial_work")
WORK_DIR.mkdir(parents=True, exist_ok=True)

DUST3R_PATH = Path(os.environ.get("DUST3R_PATH", "/opt/dust3r"))
GS_PATH = Path(os.environ.get("GS_PATH", "/opt/gaussian-splatting"))

NOVELTY_THRESHOLD = 85
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0"
MIN_IMAGES_FOR_RECONSTRUCTION = 4
MAX_IMAGES = 20

# Lawful-by-default media sourcing. The product promise is "agents upload their
# own photos." The Zillow/Redfin scrape fallback builds a derivative 3D product
# from third-party COPYRIGHTED listing photos and trips both sites' anti-scraping
# ToS — so it stays OFF unless an operator explicitly opts in (internal use only,
# with rights to the images). Default off = the code matches the marketing claim.
ALLOW_WEB_SCRAPE = os.environ.get("SPATIAL_ALLOW_WEB_SCRAPE", "0").lower() in (
    "1", "true", "yes", "on",
)

# Every reconstruction is AI-GENERATED and may hallucinate or omit geometry the
# model never observed. This disclosure must travel with the asset to the viewer
# so a buyer is never shown a fabricated room as if it were a survey of record.
SPATIAL_AI_DISCLOSURE = (
    "AI-generated 3D reconstruction. Created automatically from 2D photos; "
    "geometry may be incomplete or inaccurate. Not a measured survey and not a "
    "substitute for an in-person showing or inspection."
)


def _detect_device() -> str:
    explicit = os.environ.get("SPATIAL_DEVICE", "").lower()
    if explicit in ("cuda", "cpu"):
        return explicit
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return "cuda" if result.returncode == 0 else "cpu"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "cpu"


DEVICE = _detect_device()
RESOLUTION = int(os.environ.get("SPATIAL_RESOLUTION", "512" if DEVICE == "cuda" else "224"))

logger.info("Spatial Agent initialized: device=%s resolution=%d", DEVICE, RESOLUTION)


def property_id(address: str) -> str:
    return hashlib.sha256(address.lower().strip().encode()).hexdigest()[:16]


# ─── Image Sourcing ───────────────────────────────────────────────────────────

async def source_listing_images(address: str, work_dir: Path) -> tuple[list[Path], str]:
    """Return (images, source) where source is 'agent_upload', 'web_scrape', or
    'none'. Provenance is recorded so it can travel with the generated asset."""
    images_dir = work_dir / "images"
    images_dir.mkdir(exist_ok=True)

    # 1. Agent-uploaded / staged images — the lawful, product-promised source.
    staged = Path(__file__).parent / "staged_images" / property_id(address)
    if staged.exists():
        images = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for img in staged.glob(ext):
                dest = images_dir / img.name
                shutil.copy2(img, dest)
                images.append(dest)
        if images:
            logger.info("[Spatial] %d staged images found for %s", len(images), address)
            return images[:MAX_IMAGES], "agent_upload"

    # 2/3. Web-scrape fallback — OFF by default. Building a buyer-facing 3D product
    # from Zillow/Redfin listing photos is copyright-infringing and violates their
    # anti-scraping ToS. Only enable for internal testing with owned content.
    if not ALLOW_WEB_SCRAPE:
        logger.warning(
            "[Spatial] No uploaded images for %s and web scraping is disabled "
            "(set SPATIAL_ALLOW_WEB_SCRAPE=1 to override). Skipping reconstruction.",
            address,
        )
        return [], "none"

    logger.warning(
        "[Spatial] SCRAPING third-party listing photos for %s — copyright/ToS "
        "risk. Do not use the result for buyer-facing output without rights to "
        "the source images.", address,
    )
    images = await _scrape_zillow_images(address, images_dir)
    if len(images) >= MIN_IMAGES_FOR_RECONSTRUCTION:
        return images[:MAX_IMAGES], "web_scrape"

    images = await _scrape_redfin_images(address, images_dir)
    if len(images) >= MIN_IMAGES_FOR_RECONSTRUCTION:
        return images[:MAX_IMAGES], "web_scrape"

    logger.warning("[Spatial] Insufficient images for %s (found %d, need %d)",
                   address, len(images), MIN_IMAGES_FOR_RECONSTRUCTION)
    return images, "web_scrape"


async def _scrape_zillow_images(address: str, output_dir: Path) -> list[Path]:
    """Attempt to fetch listing photos from Zillow's static CDN."""
    try:
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', address.strip()).strip('-').lower()
        search_url = f"https://www.zillow.com/homes/{urllib.parse.quote(slug)}_rb/"

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
        }
        cookie = os.environ.get("ZILLOW_COOKIE", "")
        if cookie:
            headers["Cookie"] = cookie

        req = urllib.request.Request(search_url, headers=headers)
        html = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
        )

        # Extract image URLs from Zillow's preloaded image data
        # Use non-capturing group so findall returns full URL strings, not just extensions
        img_pattern = re.compile(r'https://photos\.zillowstatic\.com/fp/[a-f0-9]+-[a-z_]+\.(?:jpg|webp)')
        urls = list(set(img_pattern.findall(html)))[:MAX_IMAGES]

        if not urls:
            # Fallback: look for any large CDN images
            img_pattern2 = re.compile(r'(https://[^"]+\.(?:jpg|jpeg|png|webp))')
            all_imgs = img_pattern2.findall(html)
            urls = [(u, 'jpg') for u in all_imgs
                    if ('zillow' in u or 'zillowstatic' in u) and 'cc_ft_' in u][:MAX_IMAGES]

        images = []
        for i, match in enumerate(urls):
            url = match if isinstance(match, str) else match[0]
            try:
                img_req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                data = await asyncio.to_thread(
                    lambda u=img_req: urllib.request.urlopen(u, timeout=10).read()
                )
                ext = "webp" if url.endswith(".webp") else "jpg"
                path = output_dir / f"zillow_{i:03d}.{ext}"
                path.write_bytes(data)
                images.append(path)
                await asyncio.sleep(0.5)  # Rate limit
            except Exception:
                continue

        logger.info("[Spatial] Zillow scrape: %d images for '%s'", len(images), address)
        return images

    except Exception as e:
        logger.debug("[Spatial] Zillow scrape failed: %s", e)
        return []


async def _scrape_redfin_images(address: str, output_dir: Path) -> list[Path]:
    """Attempt Redfin's stingray CDN for listing photos."""
    try:
        slug = urllib.parse.quote(address.strip())
        url = f"https://www.redfin.com/stingray/do/api-get-photos?listingId={slug}&numPhotos=20"

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        body = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=15).read().decode()
        )

        img_pattern = re.compile(r'(https://ssl\.cdn-redfin\.com/photo/[^"]+\.(?:jpg|jpeg|png))')
        urls = img_pattern.findall(body)[:MAX_IMAGES]

        images = []
        for i, url in enumerate(urls):
            try:
                img_req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                data = await asyncio.to_thread(
                    lambda u=img_req: urllib.request.urlopen(u, timeout=10).read()
                )
                path = output_dir / f"redfin_{i:03d}.jpg"
                path.write_bytes(data)
                images.append(path)
                await asyncio.sleep(0.3)
            except Exception:
                continue

        logger.info("[Spatial] Redfin scrape: %d images for '%s'", len(images), address)
        return images

    except Exception as e:
        logger.debug("[Spatial] Redfin scrape failed: %s", e)
        return []


# ─── DUSt3R Reconstruction ────────────────────────────────────────────────────

async def run_dust3r(images_dir: Path, output_dir: Path) -> Optional[Path]:
    """Multi-view 3D reconstruction via DUSt3R."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pointcloud = output_dir / "scene.ply"

    model = "DUSt3R_ViTLarge_BaseDecoder_512_dpt" if DEVICE == "cuda" else "DUSt3R_ViTBase_BaseDecoder_224"

    cmd = [
        "python3", str(DUST3R_PATH / "demo.py"),
        "--input_dir", str(images_dir),
        "--output_dir", str(output_dir),
        "--model_name", model,
        "--batch_size", "1",
        "--device", DEVICE,
        "--image_size", str(RESOLUTION),
    ]

    timeout = 180 if DEVICE == "cuda" else 600

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode != 0:
            logger.error("[Spatial] DUSt3R failed (rc=%d): %s", proc.returncode, stderr.decode()[:500])
            return None

        if pointcloud.exists():
            logger.info("[Spatial] DUSt3R produced %s (%.1f MB)",
                        pointcloud, pointcloud.stat().st_size / 1e6)
            return pointcloud

        # Check for alternative output paths
        for alt in output_dir.rglob("*.ply"):
            return alt

    except asyncio.TimeoutError:
        logger.error("[Spatial] DUSt3R timed out (%ds)", timeout)
        proc.kill()
        await proc.wait()
    except FileNotFoundError:
        logger.error("[Spatial] DUSt3R not installed at %s", DUST3R_PATH)

    return None


# ─── Gaussian Splatting Training ──────────────────────────────────────────────

async def run_gaussian_splatting(pointcloud: Path, output_dir: Path) -> Optional[Path]:
    """Train 3D Gaussian Splatting from point cloud — fast 7k iteration config."""
    output_dir.mkdir(parents=True, exist_ok=True)
    model_dir = output_dir / "gs_model"
    splat_path = output_dir / "output.splat"

    iterations = 7000 if DEVICE == "cuda" else 3000

    cmd = [
        "python3", str(GS_PATH / "train.py"),
        "--source_path", str(pointcloud.parent),
        "--model_path", str(model_dir),
        "--iterations", str(iterations),
        "--densify_until_iter", str(min(5000, iterations - 500)),
        "--sh_degree", "2",
    ]

    timeout = 300 if DEVICE == "cuda" else 900

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode != 0:
            logger.error("[Spatial] GS training failed: %s", stderr.decode()[:500])
            return None

        # Find the final PLY and convert to .splat binary
        ply_path = model_dir / "point_cloud" / f"iteration_{iterations}" / "point_cloud.ply"
        if not ply_path.exists():
            # Try any PLY in the model dir
            for p in model_dir.rglob("*.ply"):
                ply_path = p
                break

        if ply_path.exists():
            await _convert_ply_to_splat(ply_path, splat_path)
            if splat_path.exists():
                logger.info("[Spatial] GS produced %s (%.1f MB)",
                            splat_path, splat_path.stat().st_size / 1e6)
                return splat_path

    except asyncio.TimeoutError:
        logger.error("[Spatial] GS training timed out (%ds)", timeout)
        proc.kill()
        await proc.wait()
    except FileNotFoundError:
        logger.error("[Spatial] Gaussian Splatting not installed at %s", GS_PATH)

    return None


async def _convert_ply_to_splat(ply_path: Path, splat_path: Path) -> None:
    """Convert PLY gaussians to .splat binary format (position + scale/opacity + rotation + SH)."""
    def _blocking():
        try:
            from plyfile import PlyData
        except ImportError:
            logger.error("[Spatial] plyfile not installed — cannot convert PLY to splat")
            return

        ply = PlyData.read(str(ply_path))
        vertices = ply['vertex']

        with open(splat_path, 'wb') as f:
            for v in vertices:
                # Position (3 floats)
                f.write(struct.pack('fff', float(v['x']), float(v['y']), float(v['z'])))
                # Scale + opacity (4 floats)
                f.write(struct.pack('ffff',
                    float(v['scale_0']), float(v['scale_1']), float(v['scale_2']),
                    float(v['opacity'])))
                # Rotation quaternion (4 floats)
                f.write(struct.pack('ffff',
                    float(v['rot_0']), float(v['rot_1']), float(v['rot_2']), float(v['rot_3'])))
                # Spherical harmonics DC (3 floats — base color)
                f.write(struct.pack('fff',
                    float(v['f_dc_0']), float(v['f_dc_1']), float(v['f_dc_2'])))

    await asyncio.to_thread(_blocking)


# ─── Full Pipeline Orchestration ──────────────────────────────────────────────

async def reconstruct_property(address: str, websocket=None) -> Optional[str]:
    """
    Full auto pipeline: source images → DUSt3R → GS → .splat
    Returns the public URL path, or None on failure.
    """
    prop_id_val = property_id(address)
    final_splat = SPLAT_OUTPUT_DIR / f"{prop_id_val}.splat"

    # Cache hit — already reconstructed
    if final_splat.exists():
        url = f"/public/splats/{prop_id_val}.splat"
        if websocket:
            await websocket.send_text(json.dumps({
                "type": "SPLAT_READY",
                "splatUrl": f"http://localhost:8000{url}",
                "propertyId": prop_id_val,
                "address": address,
                "cached": True,
                "generated": True,
                "disclosure": SPATIAL_AI_DISCLOSURE,
            }))
        return url

    work_dir = WORK_DIR / prop_id_val
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    async def status(msg):
        logger.info("[Spatial] %s", msg)
        if websocket:
            await websocket.send_text(json.dumps({
                "type": "STATUS_UPDATE",
                "agent": f"SPATIAL AGENT — {msg}",
            }))

    await status(f"sourcing listing images for {address}")
    images, image_source = await source_listing_images(address, work_dir)

    if len(images) < MIN_IMAGES_FOR_RECONSTRUCTION:
        await status(f"insufficient images ({len(images)}/{MIN_IMAGES_FOR_RECONSTRUCTION}) — cannot reconstruct")
        return None

    await status(f"running DUSt3R on {len(images)} images ({DEVICE}, {RESOLUTION}px)")
    pointcloud = await run_dust3r(work_dir / "images", work_dir / "dust3r_out")
    if not pointcloud:
        await status("DUSt3R reconstruction failed")
        return None

    await status(f"training Gaussian Splat model ({7000 if DEVICE == 'cuda' else 3000} iterations)")
    splat = await run_gaussian_splatting(pointcloud, work_dir / "gs_out")
    if not splat:
        await status("Gaussian Splatting training failed")
        return None

    shutil.copy2(splat, final_splat)
    elapsed = time.monotonic() - t0

    url = f"/public/splats/{prop_id_val}.splat"
    await status(f"reconstruction complete in {elapsed:.0f}s — {final_splat.stat().st_size / 1e6:.1f} MB")

    # Provenance + disclosure manifest travels next to the .splat so the viewer
    # (and any audit) always knows the asset is AI-generated and where the source
    # images came from.
    manifest = {
        "propertyId": prop_id_val,
        "address": address,
        "generated": True,
        "generator": "dust3r+gaussian-splatting",
        "imageSource": image_source,
        "disclosure": SPATIAL_AI_DISCLOSURE,
    }
    try:
        (SPLAT_OUTPUT_DIR / f"{prop_id_val}.json").write_text(json.dumps(manifest, indent=2))
    except OSError as exc:  # never let a side feature crash on a write error
        logger.warning("[Spatial] Could not write manifest for %s: %s", prop_id_val, exc)

    if websocket:
        await websocket.send_text(json.dumps({
            "type": "SPLAT_READY",
            "splatUrl": f"http://localhost:8000{url}",
            "propertyId": prop_id_val,
            "address": address,
            "elapsed_s": round(elapsed, 1),
            "generated": True,
            "imageSource": image_source,
            "disclosure": SPATIAL_AI_DISCLOSURE,
        }))

    shutil.rmtree(work_dir, ignore_errors=True)
    return url


def should_trigger_reconstruction(novelty_score: int) -> bool:
    return novelty_score >= NOVELTY_THRESHOLD
