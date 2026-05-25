"""
Spatial Agent — converts 2D listing images into 3D Gaussian Splats.

Pipeline:
  1. Download listing images for a property address
  2. Run DUSt3R for multi-view 3D reconstruction (point cloud + camera poses)
  3. Convert to 3D Gaussian Splatting format (.splat)
  4. Save to public/splats/{property_id}.splat
  5. Notify frontend via WebSocket SPLAT_READY message
"""

import asyncio
import hashlib
import json
import os
import subprocess
import shutil
from pathlib import Path

SPLAT_OUTPUT_DIR = Path(__file__).parent.parent / "oracle-app" / "public" / "splats"
SPLAT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

RECONSTRUCTION_WORK_DIR = Path("/tmp/oracle_spatial_work")
RECONSTRUCTION_WORK_DIR.mkdir(parents=True, exist_ok=True)

DUST3R_PATH = os.environ.get("DUST3R_PATH", "/opt/dust3r")
GAUSSIAN_SPLATTING_PATH = os.environ.get("GS_PATH", "/opt/gaussian-splatting")

NOVELTY_THRESHOLD = 90


def property_id_from_address(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()[:16]


async def download_listing_images(address: str, work_dir: Path) -> list[Path]:
    """
    Download 2D listing photos for a property.
    In production this would hit MLS/Zillow/Redfin APIs.
    For now, looks for pre-staged images or returns empty.
    """
    images_dir = work_dir / "images"
    images_dir.mkdir(exist_ok=True)

    staged_dir = Path(__file__).parent / "staged_images" / property_id_from_address(address)
    if staged_dir.exists():
        images = []
        for img in staged_dir.glob("*.jpg"):
            dest = images_dir / img.name
            shutil.copy2(img, dest)
            images.append(dest)
        for img in staged_dir.glob("*.png"):
            dest = images_dir / img.name
            shutil.copy2(img, dest)
            images.append(dest)
        return images

    return []


async def run_dust3r_reconstruction(images_dir: Path, output_dir: Path) -> Path | None:
    """
    Run DUSt3R to produce a dense point cloud from multi-view images.
    Returns path to the point cloud output, or None on failure.
    """
    pointcloud_path = output_dir / "scene.ply"

    cmd = [
        "python3", f"{DUST3R_PATH}/demo.py",
        "--input_dir", str(images_dir),
        "--output_dir", str(output_dir),
        "--model_name", "DUSt3R_ViTLarge_BaseDecoder_512_dpt",
        "--batch_size", "1",
        "--device", "cuda",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            print(f"[SpatialAgent] DUSt3R failed: {stderr.decode()}")
            return None

        if pointcloud_path.exists():
            return pointcloud_path

    except asyncio.TimeoutError:
        print("[SpatialAgent] DUSt3R timed out (300s)")
    except FileNotFoundError:
        print(f"[SpatialAgent] DUSt3R not found at {DUST3R_PATH}")

    return None


async def run_gaussian_splatting(pointcloud_path: Path, output_dir: Path) -> Path | None:
    """
    Train 3D Gaussian Splatting from the DUSt3R point cloud.
    Uses a fast training config (7k iterations) for near-realtime results.
    Returns path to the .splat file.
    """
    splat_output = output_dir / "point_cloud.splat"

    cmd = [
        "python3", f"{GAUSSIAN_SPLATTING_PATH}/train.py",
        "--source_path", str(pointcloud_path.parent),
        "--model_path", str(output_dir / "gs_model"),
        "--iterations", "7000",
        "--densify_until_iter", "5000",
        "--sh_degree", "2",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

        if proc.returncode != 0:
            print(f"[SpatialAgent] GS training failed: {stderr.decode()}")
            return None

        ply_output = output_dir / "gs_model" / "point_cloud" / "iteration_7000" / "point_cloud.ply"
        if ply_output.exists():
            convert_cmd = [
                "python3", "-c",
                f"from plyfile import PlyData; "
                f"import struct; "
                f"ply = PlyData.read('{ply_output}'); "
                f"# Convert PLY gaussians to .splat binary format\n"
                f"with open('{splat_output}', 'wb') as f:\n"
                f"    for v in ply['vertex']:\n"
                f"        f.write(struct.pack('fff', v['x'], v['y'], v['z']))\n"
                f"        f.write(struct.pack('ffff', v['scale_0'], v['scale_1'], v['scale_2'], v['opacity']))\n"
                f"        f.write(struct.pack('ffff', v['rot_0'], v['rot_1'], v['rot_2'], v['rot_3']))\n"
                f"        f.write(struct.pack('fff', v['f_dc_0'], v['f_dc_1'], v['f_dc_2']))\n"
            ]
            conv_proc = await asyncio.create_subprocess_exec(
                *convert_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await conv_proc.communicate()

        if splat_output.exists():
            return splat_output

    except asyncio.TimeoutError:
        print("[SpatialAgent] GS training timed out (600s)")
    except FileNotFoundError:
        print(f"[SpatialAgent] Gaussian Splatting not found at {GAUSSIAN_SPLATTING_PATH}")

    return None


async def reconstruct_property(address: str, websocket=None) -> str | None:
    """
    Full pipeline: images → DUSt3R → Gaussian Splatting → .splat file.
    Returns the public URL path for the splat, or None on failure.
    """
    prop_id = property_id_from_address(address)
    final_splat = SPLAT_OUTPUT_DIR / f"{prop_id}.splat"

    if final_splat.exists():
        url = f"/public/splats/{prop_id}.splat"
        if websocket:
            await websocket.send_text(json.dumps({
                "type": "SPLAT_READY",
                "url": url,
                "property_id": prop_id,
            }))
        return url

    work_dir = RECONSTRUCTION_WORK_DIR / prop_id
    work_dir.mkdir(parents=True, exist_ok=True)

    if websocket:
        await websocket.send_text(json.dumps({
            "type": "STATUS_UPDATE",
            "agent": f"SPATIAL AGENT — downloading listing images for {address}",
        }))

    images = await download_listing_images(address, work_dir)
    if not images:
        if websocket:
            await websocket.send_text(json.dumps({
                "type": "STATUS_UPDATE",
                "agent": "SPATIAL AGENT — no images available, skipping reconstruction",
            }))
        return None

    if websocket:
        await websocket.send_text(json.dumps({
            "type": "STATUS_UPDATE",
            "agent": f"SPATIAL AGENT — running DUSt3R on {len(images)} images",
        }))

    pointcloud = await run_dust3r_reconstruction(work_dir / "images", work_dir / "dust3r_out")
    if not pointcloud:
        return None

    if websocket:
        await websocket.send_text(json.dumps({
            "type": "STATUS_UPDATE",
            "agent": "SPATIAL AGENT — training Gaussian Splat model (7k iterations)",
        }))

    splat_path = await run_gaussian_splatting(pointcloud, work_dir / "gs_out")
    if not splat_path:
        return None

    shutil.copy2(splat_path, final_splat)

    url = f"/public/splats/{prop_id}.splat"
    if websocket:
        await websocket.send_text(json.dumps({
            "type": "SPLAT_READY",
            "url": url,
            "property_id": prop_id,
        }))

    shutil.rmtree(work_dir, ignore_errors=True)
    return url


def should_trigger_reconstruction(novelty_score: int) -> bool:
    return novelty_score >= NOVELTY_THRESHOLD
