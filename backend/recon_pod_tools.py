"""Quality tools that run ON the reconstruction pod, shipped inside its script.

`reconstruction_providers.POD_PIPELINE` embeds this file verbatim and writes it
to `/workspace/neoh_recon_tools.py`, so one script remains the only thing either
transport delivers. It lives here, as a module, so every piece that can be
tested without a GPU is tested on every commit (tests/test_recon_pod_tools.py)
rather than discovered at the last line of a paid run — all five early pod
failures were exactly that.

Rules this file keeps, because the pod is not this machine:

* Module scope imports only the standard library and numpy. torch,
  torchvision, cv2, PIL, imageio and gsplat are imported inside the function
  that needs them, so a missing one disables ONE feature, not the file.
* Python 3.10 syntax (the pod image's interpreter); no `match`, no 3.12 typing.
* Every command writes what it measured to a JSON report and exits non-zero on
  failure; the pod script treats each one as optional and continues without
  it. A quality tool must never be the reason a reconstruction is lost.

Subcommands (python neoh_recon_tools.py <cmd> ...):

  lens-groups IMAGES OUT_DIR      group frames by lens (EXIF + size) for COLMAP
  masks-check                     load the segmentation model (downloads weights)
  masks IMAGES MASK_DIR REPORT    mask people, pets and screens for COLMAP + training
  train MASK_DIR -- ARGS...       run gsplat's simple_trainer with those masks applied
  tag-antialiased PLY             mark a PLY as trained with --antialiased
  ply-count PLY                   print a PLY's vertex count
  selftest-render PLY             render a tiny PLY through gsplat (toolchain check)
  roundtrip DATA RAW PRUNED SOGPLY REPORT [--antialiased]
                                  render held-out views from three PLYs and compare
  qa WORKSPACE REPORT             gather every measurement into quality.json
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

# ── lens grouping ────────────────────────────────────────────────────────────

#: EXIF tags that identify the lens a frame came from. Phones switch between
#: wide, ultra-wide and tele modules mid-walk; each has its own intrinsics, and
#: forcing one camera model on all of them makes COLMAP solve a compromise
#: lens that fits none — soft, smeared geometry with no error reported.
_EXIF_MAKE, _EXIF_MODEL = 0x010F, 0x0110
_EXIF_FOCAL, _EXIF_FOCAL_35, _EXIF_LENS_MODEL = 0x920A, 0xA405, 0xA434
_EXIF_IFD = 0x8769


def lens_key(exif: Dict[int, Any], size: Tuple[int, int]) -> str:
    """A stable group key for one frame. Size always counts: COLMAP's
    single-camera mode skips frames whose dimensions differ from the camera's,
    so mixed-size uploads lost frames even when they came from one lens."""
    def text(tag: int) -> str:
        value = exif.get(tag)
        return str(value).strip().strip("\x00") if value not in (None, "") else ""

    def number(tag: int) -> str:
        value = exif.get(tag)
        try:
            return f"{float(value):.1f}" if value not in (None, "") else ""
        except (TypeError, ValueError, ZeroDivisionError):
            return ""

    parts = [f"{int(size[0])}x{int(size[1])}", text(_EXIF_MAKE), text(_EXIF_MODEL),
             text(_EXIF_LENS_MODEL), number(_EXIF_FOCAL), number(_EXIF_FOCAL_35)]
    return "|".join(parts)


def group_by_lens(entries: Iterable[Tuple[str, str]], *, min_group: int = 3) -> Dict[str, List[str]]:
    """{key: [names]} from (name, key) pairs, sorted for determinism.

    A group smaller than `min_group` frames cannot calibrate a lens on its own;
    it is folded into the largest group of the same image size (or left alone
    if none matches), which is what the old single-camera behaviour did."""
    groups: Dict[str, List[str]] = {}
    for name, key in entries:
        groups.setdefault(key, []).append(name)
    for names in groups.values():
        names.sort()
    small = [k for k, v in groups.items() if len(v) < min_group]
    for key in small:
        size = key.split("|", 1)[0]
        hosts = [k for k, v in groups.items()
                 if k != key and k.split("|", 1)[0] == size and len(v) >= min_group]
        if hosts:
            host = max(hosts, key=lambda k: (len(groups[k]), k))
            groups[host] = sorted(groups[host] + groups.pop(key))
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def _read_exif(path: str) -> Tuple[Dict[int, Any], Tuple[int, int]]:
    from PIL import Image

    with Image.open(path) as image:
        size = image.size
        exif: Dict[int, Any] = {}
        try:
            raw = image.getexif()
            exif.update(dict(raw))
            try:
                exif.update(dict(raw.get_ifd(_EXIF_IFD)))
            except Exception:  # noqa: BLE001 - no Exif sub-IFD is common
                pass
        except Exception:  # noqa: BLE001 - unreadable EXIF is "no EXIF"
            pass
    return exif, size


def cmd_lens_groups(images: str, out_dir: str) -> Dict[str, Any]:
    names = sorted(n for n in os.listdir(images) if not n.startswith("."))
    entries = []
    for name in names:
        try:
            exif, size = _read_exif(os.path.join(images, name))
        except Exception:  # noqa: BLE001 - still LISTED: an unlisted frame is never extracted
            entries.append((name, "unreadable"))
            continue
        entries.append((name, lens_key(exif, size)))
    groups = group_by_lens(entries)
    os.makedirs(out_dir, exist_ok=True)
    for old in glob.glob(os.path.join(out_dir, "lens_*.txt")):
        os.remove(old)
    files = []
    for i, (key, members) in enumerate(groups.items()):
        path = os.path.join(out_dir, f"lens_{i}.txt")
        with open(path, "w") as handle:
            handle.write("\n".join(members) + "\n")
        files.append(path)
    report = {"groups": [{"key": k, "frames": len(v)} for k, v in groups.items()],
              "lists": files}
    _write_json(os.path.join(out_dir, "lens_groups.json"), report)
    return report


# ── moving-object masks ──────────────────────────────────────────────────────

#: COCO classes that move or change between frames. People and pets walk
#: through the capture; screens show different content in every frame. Each
#: becomes a ghost of translucent splats if trained on. Mirrors and windows are
#: NOT here — no COCO class covers them — and are reported as a known limit.
DYNAMIC_CLASSES = ("person", "cat", "dog", "bird", "tv", "laptop", "cell phone")
MASK_SCORE_MIN = 0.6
MASK_DILATE_PX = 12


def keep_mask(instance_masks: np.ndarray, labels: Sequence[str], scores: Sequence[float],
              *, classes: Sequence[str] = DYNAMIC_CLASSES, score_min: float = MASK_SCORE_MIN,
              dilate_px: int = MASK_DILATE_PX, shape: Optional[Tuple[int, int]] = None) -> np.ndarray:
    """True where a pixel may be used; False under any confident dynamic object.

    `instance_masks` is [N, H, W] soft masks (0..1). Dilated, because a
    detector's edge sits inside the object's true silhouette and the half-lit
    rim is exactly where ghosts form."""
    if instance_masks.ndim != 3 or len(instance_masks) == 0:
        h, w = shape if shape else instance_masks.shape[-2:]
        return np.ones((h, w), dtype=bool)
    wanted = set(classes)
    hit = np.zeros(instance_masks.shape[1:], dtype=bool)
    for mask, label, score in zip(instance_masks, labels, scores):
        if label in wanted and float(score) >= score_min:
            hit |= mask >= 0.5
    if dilate_px > 0 and hit.any():
        hit = _dilate(hit, dilate_px)
    return ~hit


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    try:
        import cv2

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        return cv2.dilate(mask.astype(np.uint8), kernel) > 0
    except ImportError:
        out = mask.copy()
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius:
                    out |= np.roll(np.roll(mask, dy, 0), dx, 1)
        return out


def _load_segmenter():
    import torch
    from torchvision.models.detection import (MaskRCNN_ResNet50_FPN_V2_Weights,
                                              maskrcnn_resnet50_fpn_v2)

    weights = MaskRCNN_ResNet50_FPN_V2_Weights.DEFAULT
    model = maskrcnn_resnet50_fpn_v2(weights=weights).eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    return model.to(device), list(weights.meta["categories"]), device


def cmd_masks_check() -> Dict[str, Any]:
    _model, categories, device = _load_segmenter()
    missing = [c for c in DYNAMIC_CLASSES if c not in categories]
    if missing:
        raise RuntimeError(f"segmentation model lacks classes {missing}")
    return {"device": device, "classes": list(DYNAMIC_CLASSES)}


def cmd_masks(images: str, mask_dir: str, report_path: str) -> Dict[str, Any]:
    """COLMAP mask convention: <mask_dir>/<image name>.png, 0 = ignore.

    Only frames with something masked get a file; a frame without one is used
    whole by both COLMAP and the trainer."""
    import torch
    from PIL import Image

    model, categories, device = _load_segmenter()
    os.makedirs(mask_dir, exist_ok=True)
    per_frame: Dict[str, float] = {}
    names = sorted(n for n in os.listdir(images) if not n.startswith("."))
    with torch.no_grad():
        for name in names:
            with Image.open(os.path.join(images, name)) as image:
                rgb = np.array(image.convert("RGB"))   # a writable copy, for torch
            tensor = torch.from_numpy(rgb).permute(2, 0, 1).float().div(255.0).to(device)
            out = model([tensor])[0]
            labels = [categories[int(i)] for i in out["labels"].tolist()]
            masks = out["masks"][:, 0].float().cpu().numpy() if len(labels) else np.zeros((0,) + rgb.shape[:2])
            keep = keep_mask(masks, labels, out["scores"].tolist(), shape=rgb.shape[:2])
            masked = float(1.0 - keep.mean())
            if masked > 0.0:
                Image.fromarray(keep.astype(np.uint8) * 255).save(os.path.join(mask_dir, f"{name}.png"))
                per_frame[name] = round(masked, 4)
    report = {
        "frames": len(names),
        "frames_masked": len(per_frame),
        "mean_masked_fraction": round(sum(per_frame.values()) / max(len(names), 1), 4),
        "max_masked_fraction": max(per_frame.values()) if per_frame else 0.0,
        "classes": list(DYNAMIC_CLASSES),
        "per_frame": per_frame,
    }
    _write_json(report_path, report)
    return report


def transform_mask(keep: np.ndarray, mapx: Optional[np.ndarray] = None,
                   mapy: Optional[np.ndarray] = None,
                   roi: Optional[Sequence[int]] = None) -> np.ndarray:
    """Apply the SAME undistortion the trainer's Dataset applies to the image,
    so the mask lands on the pixels it was drawn on. Nearest-neighbour: a mask
    must stay binary."""
    out = keep
    if mapx is not None and mapy is not None:
        import cv2

        out = cv2.remap(keep.astype(np.uint8), mapx, mapy, cv2.INTER_NEAREST) > 0
        if roi is not None:
            x, y, w, h = (int(v) for v in roi)
            out = out[y:y + h, x:x + w]
    return out.astype(bool)


def patch_dataset_with_masks(mask_dir: str) -> None:
    """Make gsplat 1.5.3's colmap Dataset drop supervision under our masks.

    The trainer already honours `data["mask"]`, but only on the RENDER
    (`render_colors[~masks] = 0`); the ground-truth pixels stay. So the image is
    zeroed there too — both sides black means no gradient, which is the point.
    Random patch crops cannot be replayed onto a mask, so with patch_size set the
    masks are skipped and that is said loudly rather than applied misaligned."""
    import torch
    from datasets import colmap as gs_colmap  # gsplat examples, on sys.path

    original = gs_colmap.Dataset.__getitem__
    cache: Dict[str, Optional[np.ndarray]] = {}
    warned = {"patch": False}

    def load(name: str) -> Optional[np.ndarray]:
        if name not in cache:
            path = os.path.join(mask_dir, f"{name}.png")
            if os.path.exists(path):
                from PIL import Image

                with Image.open(path) as mask_image:
                    cache[name] = np.asarray(mask_image.convert("L")) > 127
            else:
                cache[name] = None
        return cache[name]

    def getitem(self, item: int) -> Dict[str, Any]:
        data = original(self, item)
        if self.patch_size is not None:
            if not warned["patch"]:
                print(">>> masks skipped: patch_size crops cannot be replayed", file=sys.stderr)
                warned["patch"] = True
            return data
        index = self.indices[item]
        keep = load(self.parser.image_names[index])
        if keep is None:
            return data
        camera_id = self.parser.camera_ids[index]
        if len(self.parser.params_dict[camera_id]) > 0:
            keep = transform_mask(keep, self.parser.mapx_dict[camera_id],
                                  self.parser.mapy_dict[camera_id],
                                  self.parser.roi_undist_dict[camera_id])
        image = data["image"]
        if keep.shape != tuple(image.shape[:2]):
            print(f">>> mask/image size mismatch for {self.parser.image_names[index]}; unmasked",
                  file=sys.stderr)
            return data
        keep_t = torch.from_numpy(keep)
        data["image"] = image * keep_t[..., None].to(image.dtype)
        data["mask"] = (data["mask"] & keep_t) if "mask" in data else keep_t
        return data

    gs_colmap.Dataset.__getitem__ = getitem


def cmd_train(mask_dir: str, trainer_args: List[str]) -> None:
    import runpy

    examples = os.getcwd()
    if examples not in sys.path:
        sys.path.insert(0, examples)
    if mask_dir and os.path.isdir(mask_dir) and os.listdir(mask_dir):
        patch_dataset_with_masks(mask_dir)
        print(f">>> training with {len(os.listdir(mask_dir))} masked frame(s)", file=sys.stderr)
    sys.argv = ["simple_trainer.py"] + trainer_args
    runpy.run_path(os.path.join(examples, "simple_trainer.py"), run_name="__main__")


# ── PLY helpers ──────────────────────────────────────────────────────────────

_PLY_TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
              "uchar": "u1", "uint8": "u1", "char": "i1", "int8": "i1",
              "ushort": "<u2", "uint16": "<u2", "short": "<i2", "int16": "<i2",
              "uint": "<u4", "uint32": "<u4", "int": "<i4", "int32": "<i4"}


def ply_header(path: str) -> Tuple[List[str], int, int]:
    """(header lines, vertex count, byte offset of the body)."""
    with open(path, "rb") as handle:
        head = handle.read(65536)
    end = head.find(b"end_header")
    if not head.startswith(b"ply") or end < 0:
        raise ValueError(f"{path} is not a PLY")
    body = head.find(b"\n", end) + 1
    lines = head[:body].decode("ascii", "replace").splitlines()
    count = 0
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and parts[0] == "element" and parts[1] == "vertex":
            count = int(parts[2])
    return lines, count, body


def tag_antialiased(path: str) -> bool:
    """Add Postshot's `comment antialiased 1` so splat-transform carries
    `"model": "antialiased"` into the .sog. gsplat's exporter writes no tag at
    all, and a scene trained antialiased but drawn in classic mode renders
    small splats too opaque — worse than not training it that way."""
    lines, _count, body = ply_header(path)
    if any(line.strip() == "comment antialiased 1" for line in lines):
        return False
    new_lines = [lines[0], lines[1], "comment antialiased 1"] + lines[2:]
    tmp = path + ".tag"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(("\n".join(new_lines) + "\n").encode("ascii"))
        src.seek(body)
        while True:
            chunk = src.read(1 << 24)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(tmp, path)
    return True


def load_ply_splats(path: str) -> Dict[str, np.ndarray]:
    """Gaussians from a 3DGS PLY as gsplat's rasterizer wants them.

    Stored values are raw parameters: opacity is a logit, scales are logs,
    and f_rest is channel-major ([3, K] per splat, as gsplat's exporter and
    splat-transform both write it)."""
    lines, count, body = ply_header(path)
    fields: List[Tuple[str, str]] = []
    in_vertex = False
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "element":
            in_vertex = parts[1] == "vertex"
        elif parts[0] == "property" and in_vertex and parts[1] != "list":
            fields.append((parts[2], _PLY_TYPES[parts[1]]))
    dtype = np.dtype(fields)
    table = np.fromfile(path, dtype=dtype, count=count, offset=body)
    names = dtype.names or ()
    rest = sorted((n for n in names if n.startswith("f_rest_")), key=lambda n: int(n[7:]))
    k = len(rest) // 3
    degree = int(round(math.sqrt(k + 1))) - 1
    if len(rest) % 3 or (degree + 1) ** 2 - 1 != k:
        raise ValueError(f"{path}: {len(rest)} f_rest columns is not a full SH band set")
    sh0 = np.stack([table[f"f_dc_{i}"] for i in range(3)], axis=1)[:, None, :]
    if k:
        shn = np.stack([table[n] for n in rest], axis=1).reshape(count, 3, k).transpose(0, 2, 1)
        sh = np.concatenate([sh0, shn], axis=1)
    else:
        sh = sh0
    return {
        "means": np.stack([table["x"], table["y"], table["z"]], axis=1).astype(np.float32),
        "quats": np.stack([table[f"rot_{i}"] for i in range(4)], axis=1).astype(np.float32),
        "scales": np.exp(np.stack([table[f"scale_{i}"] for i in range(3)], axis=1)).astype(np.float32),
        "opacities": (1.0 / (1.0 + np.exp(-np.clip(table["opacity"].astype(np.float64), -30, 30)))).astype(np.float32),
        "sh": sh.astype(np.float32),
        "sh_degree": np.array(degree),
    }


# ── held-out rendering: raw vs pruned vs compressed ─────────────────────────

def psnr(a: np.ndarray, b: np.ndarray) -> float:
    """PSNR of two images in [0, 1]. Identical images report 99 dB rather
    than infinity so the number survives JSON."""
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return 99.0 if mse <= 1e-12 else round(10.0 * math.log10(1.0 / mse), 3)


def _render(splats: Dict[str, Any], camtoworld: np.ndarray, K: np.ndarray,
            width: int, height: int, antialiased: bool) -> np.ndarray:
    import torch
    from gsplat.rendering import rasterization

    device = "cuda"
    t = {k: torch.from_numpy(np.asarray(v)).to(device) for k, v in splats.items() if k != "sh_degree"}
    view = torch.linalg.inv(torch.from_numpy(camtoworld).float().to(device))[None]
    colors, _alphas, _info = rasterization(
        means=t["means"], quats=t["quats"], scales=t["scales"], opacities=t["opacities"],
        colors=t["sh"], viewmats=view, Ks=torch.from_numpy(K).float().to(device)[None],
        width=int(width), height=int(height), sh_degree=int(splats["sh_degree"]),
        rasterize_mode="antialiased" if antialiased else "classic",
    )
    return torch.clamp(colors[0], 0.0, 1.0).cpu().numpy()


def cmd_selftest_render(ply: str) -> Dict[str, Any]:
    splats = load_ply_splats(ply)
    c2w = np.eye(4, dtype=np.float32)
    c2w[2, 3] = -3.0
    K = np.array([[50.0, 0, 32], [0, 50.0, 32], [0, 0, 1]], dtype=np.float32)
    image = _render(splats, c2w, K, 64, 64, antialiased=True)
    if not np.isfinite(image).all():
        raise RuntimeError("render produced non-finite pixels")
    return {"ok": True, "shape": list(image.shape)}


def cmd_roundtrip(data_dir: str, raw: str, pruned: str, sog_ply: str, report_path: str,
                  antialiased: bool, max_views: int = 12) -> Dict[str, Any]:
    """Render the trainer's own held-out views from the trained PLY, the pruned
    PLY and the .sog decoded back to PLY, through ONE renderer.

    That separates three causes the eye cannot: raw vs ground truth is capture
    + training; raw vs pruned is what floater removal cost; pruned vs decoded
    .sog is what compression cost. Smear in the .sog but not the PLY means the
    converter, not the camera."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gs", "examples"))
    from datasets.colmap import Dataset, Parser

    parser = Parser(data_dir=data_dir, factor=1, normalize=True, test_every=8)
    valset = Dataset(parser, split="val")
    models = {"raw": load_ply_splats(raw), "pruned": load_ply_splats(pruned),
              "sog": load_ply_splats(sog_ply)}
    rows = []
    step = max(1, len(valset) // max_views)
    for item in range(0, len(valset), step)[:max_views]:
        sample = valset[item]
        gt = sample["image"].numpy() / 255.0
        h, w = gt.shape[:2]
        K = sample["K"].numpy()
        c2w = sample["camtoworld"].numpy()
        renders = {name: _render(m, c2w, K, w, h, antialiased) for name, m in models.items()}
        rows.append({
            "raw_vs_photo": psnr(renders["raw"], gt),
            "pruned_vs_photo": psnr(renders["pruned"], gt),
            "sog_vs_photo": psnr(renders["sog"], gt),
            "pruned_vs_raw": psnr(renders["pruned"], renders["raw"]),
            "sog_vs_pruned": psnr(renders["sog"], renders["pruned"]),
        })
    report = {"views": len(rows), **summarise_roundtrip(rows)}
    _write_json(report_path, report)
    return report


def summarise_roundtrip(rows: List[Dict[str, float]]) -> Dict[str, Any]:
    """Mean PSNR per comparison, plus the worst view of each — a single
    smeared doorway matters even when the average looks fine."""
    if not rows:
        return {"mean": {}, "worst": {}}
    keys = rows[0].keys()
    return {
        "mean": {k: round(float(np.mean([r[k] for r in rows])), 3) for k in keys},
        "worst": {k: round(float(np.min([r[k] for r in rows])), 3) for k in keys},
    }


# ── QA aggregation ───────────────────────────────────────────────────────────

_ANALYZER = {
    "cameras": r"Cameras:\s*(\d+)",
    "registered_images": r"Registered images:\s*(\d+)",
    "points": r"Points:\s*(\d+)",
    "observations": r"Observations:\s*(\d+)",
    "mean_track_length": r"Mean track length:\s*([\d.]+)",
    "mean_reprojection_error_px": r"Mean reprojection error:\s*([\d.]+)\s*px",
}


def parse_model_analyzer(text: str) -> Dict[str, float]:
    """COLMAP's `model_analyzer` summary as numbers. Only what it printed."""
    out: Dict[str, float] = {}
    for key, pattern in _ANALYZER.items():
        found = re.findall(pattern, text)
        if found:
            value = found[-1]
            out[key] = float(value) if "." in value else int(value)
    return out


def latest_val_stats(stats_dir: str) -> Dict[str, Any]:
    """gsplat's held-out metrics at the LAST evaluated step (every 8th photo
    is never trained on; PSNR/SSIM/LPIPS compare the render to it)."""
    best: Tuple[int, Optional[str]] = (-1, None)
    for path in glob.glob(os.path.join(stats_dir, "val_step*.json")):
        found = re.search(r"val_step(\d+)\.json$", path)
        if found and int(found.group(1)) > best[0]:
            best = (int(found.group(1)), path)
    if best[1] is None:
        return {}
    with open(best[1]) as handle:
        stats = json.load(handle)
    keep = ("psnr", "ssim", "lpips", "cc_psnr", "cc_ssim", "cc_lpips", "num_GS")
    out = {k: stats[k] for k in keep if k in stats}
    out["step"] = best[0] + 1
    return out


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def build_quality_report(workspace: str) -> Dict[str, Any]:
    """Everything measured on this run, in one file the worker reads. Absent
    measurements are absent — never zero, never guessed."""
    report: Dict[str, Any] = {"version": 1}
    images = os.path.join(workspace, "images")
    if os.path.isdir(images):
        report["input_images"] = len([n for n in os.listdir(images) if not n.startswith(".")])
    analyzer = os.path.join(workspace, "model_analyzer.txt")
    if os.path.exists(analyzer):
        with open(analyzer, errors="replace") as handle:
            report["colmap"] = parse_model_analyzer(handle.read())
        registered = report["colmap"].get("registered_images")
        if registered is not None and report.get("input_images"):
            report["colmap"]["registration_ratio"] = round(registered / report["input_images"], 4)
    held_out = latest_val_stats(os.path.join(workspace, "out", "stats"))
    if held_out:
        report["held_out"] = held_out
    for name in ("lens_groups", "masks", "prune", "roundtrip", "training"):
        found = _read_json(os.path.join(workspace, f"{name}.json"))
        if found is not None:
            if name == "masks":
                found = {k: v for k, v in found.items() if k != "per_frame"}
            report[name] = found
    return report


def cmd_qa(workspace: str, report_path: str) -> Dict[str, Any]:
    report = build_quality_report(workspace)
    _write_json(report_path, report)
    return report


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "lens-groups":
            result: Any = cmd_lens_groups(args[0], args[1])
        elif cmd == "masks-check":
            result = cmd_masks_check()
        elif cmd == "masks":
            result = cmd_masks(args[0], args[1], args[2])
        elif cmd == "train":
            split = args.index("--") if "--" in args else len(args)
            cmd_train(args[0] if split > 0 else "", args[split + 1:])
            return 0
        elif cmd == "tag-antialiased":
            result = {"tagged": tag_antialiased(args[0])}
        elif cmd == "ply-count":
            print(ply_header(args[0])[1])
            return 0
        elif cmd == "selftest-render":
            result = cmd_selftest_render(args[0])
        elif cmd == "roundtrip":
            aa = "--antialiased" in args
            pos = [a for a in args if a != "--antialiased"]
            result = cmd_roundtrip(pos[0], pos[1], pos[2], pos[3], pos[4], antialiased=aa)
        elif cmd == "qa":
            result = cmd_qa(args[0], args[1])
        else:
            print(f"unknown command {cmd!r}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001 - report, let the pod script decide
        import traceback

        traceback.print_exc()
        print(f">>> {cmd} failed: {exc}", file=sys.stderr)
        return 1
    print(">>> " + json.dumps(result, sort_keys=True, default=str)[:600], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
