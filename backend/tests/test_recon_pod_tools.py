"""The quality tools that run on the reconstruction pod.

Everything here is testable without a GPU, and is tested, because the pod is
the most expensive place to find a bug: all five early pod failures were
discovered at the last line of a paid run. The GPU-only paths (segmentation,
rasterisation) are exercised on the pod by `selftest-render` / `masks-check`
BEFORE COLMAP, and are optional there.
"""

from __future__ import annotations

import ast
import json
import shutil
import struct
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import recon_pod_tools as tools  # noqa: E402
from reconstruction_providers import POD_PIPELINE, render_pod_pipeline  # noqa: E402

SETTINGS = {"steps": 30000, "matcher": "sequential"}


# ── shipping: the file the pod receives ─────────────────────────────────────

def test_module_scope_imports_only_stdlib_and_numpy():
    """A missing torch/cv2/PIL on the pod must disable one feature, never the
    whole file — so heavy imports live inside the functions that need them."""
    tree = ast.parse((BACKEND / "recon_pod_tools.py").read_text())
    allowed = {"__future__", "glob", "json", "math", "os", "re", "sys", "typing", "numpy"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert names <= allowed, f"module-scope import of {names - allowed}"


def test_parses_as_python_3_10():
    """The pod image's interpreter, not this machine's."""
    ast.parse((BACKEND / "recon_pod_tools.py").read_text(), feature_version=(3, 10))


def test_the_rendered_script_embeds_the_tools_and_is_valid_bash(tmp_path):
    script = render_pod_pipeline(SETTINGS)
    assert "__RECON_TOOLS__" not in script
    assert "def build_quality_report" in script
    # The heredoc opens and closes exactly once — the tools cannot end it early.
    assert script.count("\nNEOH_TOOLS_PY\n") == 1
    for placeholder in ("__STEPS__", "__ST__", "__GSPLAT__", "__NODE__", "__MATCH_CMD__"):
        assert placeholder not in script, placeholder
    path = tmp_path / "pipeline.sh"
    path.write_text(script)
    if shutil.which("bash"):
        checked = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


def test_the_embedded_tools_are_byte_identical_to_the_module(tmp_path):
    script = render_pod_pipeline(SETTINGS)
    start = script.index("<<'NEOH_TOOLS_PY'\n") + len("<<'NEOH_TOOLS_PY'\n")
    end = script.index("\nNEOH_TOOLS_PY\n", start)
    assert script[start:end] == (BACKEND / "recon_pod_tools.py").read_text().rstrip("\n")


def test_every_quality_step_on_the_pod_is_optional():
    """No tool invocation may sit where `set -e` would let it end the job."""
    for line in POD_PIPELINE.splitlines():
        stripped = line.strip()
        if '"$TOOLS"' not in stripped or stripped.startswith("#"):
            continue
        guarded = (stripped.startswith(("if ", "elif ")) or "||" in stripped or "&&" in stripped
                   or stripped.startswith("BEFORE=") or "then" in stripped)
        assert guarded, f"unguarded quality tool: {stripped}"


def test_training_falls_back_to_the_proven_configuration():
    assert "retrying the plain trainer" in POD_PIPELINE
    fallback = POD_PIPELINE[POD_PIPELINE.index("retrying the plain trainer"):]
    fallback = fallback[:fallback.index("fi\n")]
    assert "python simple_trainer.py default $TRAIN_ARGS\n" in fallback
    assert "$TRAIN_EXTRA" not in fallback and '"$TOOLS"' not in fallback


def test_floater_removal_has_a_sanity_bound_and_can_be_reverted():
    assert "--filter-floaters 0.05,0.1,0.004" in POD_PIPELINE
    assert "(b-a)/b<=0.25" in POD_PIPELINE
    assert "delivering the unpruned scene" in POD_PIPELINE


# ── lens grouping ───────────────────────────────────────────────────────────

def test_frames_from_different_lenses_get_different_cameras():
    wide = {0x010F: "Apple", 0x0110: "iPhone 15", 0xA434: "back camera 6.86mm", 0x920A: 6.86}
    ultra = {0x010F: "Apple", 0x0110: "iPhone 15", 0xA434: "back ultra wide 2.22mm", 0x920A: 2.22}
    entries = ([(f"w{i}.jpg", tools.lens_key(wide, (4032, 3024))) for i in range(10)]
               + [(f"u{i}.jpg", tools.lens_key(ultra, (4032, 3024))) for i in range(5)])
    groups = tools.group_by_lens(entries)
    assert sorted(len(v) for v in groups.values()) == [5, 10]


def test_a_lens_group_too_small_to_calibrate_joins_the_main_one():
    main = {0x0110: "Pixel 8", 0x920A: 6.9}
    stray = {0x0110: "Pixel 8", 0x920A: 18.0}
    entries = ([(f"m{i}.jpg", tools.lens_key(main, (4000, 3000))) for i in range(12)]
               + [("tele.jpg", tools.lens_key(stray, (4000, 3000)))])
    groups = tools.group_by_lens(entries)
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 13


def test_no_exif_means_one_camera_per_image_size():
    """Video frames carry no EXIF and come from one lens: one group, as before.
    Mixed sizes split, because COLMAP's single-camera mode skips frames whose
    size differs from the camera's."""
    same = tools.group_by_lens([(f"f{i}.jpg", tools.lens_key({}, (1920, 1080))) for i in range(30)])
    assert len(same) == 1
    mixed = tools.group_by_lens([(f"a{i}.jpg", tools.lens_key({}, (1920, 1080))) for i in range(5)]
                                + [(f"b{i}.jpg", tools.lens_key({}, (1080, 1920))) for i in range(5)])
    assert len(mixed) == 2


def test_lens_lists_cover_every_frame_even_unreadable_ones(tmp_path):
    from PIL import Image

    images = tmp_path / "images"
    images.mkdir()
    for i in range(4):
        exif = Image.Exif()
        exif[0x0110] = "CamA"
        Image.new("RGB", (64, 48)).save(images / f"a{i}.jpg", exif=exif)
    for i in range(4):
        exif = Image.Exif()
        exif[0x0110] = "CamB"
        Image.new("RGB", (64, 48)).save(images / f"b{i}.jpg", exif=exif)
    (images / "broken.jpg").write_bytes(b"not an image")
    report = tools.cmd_lens_groups(str(images), str(tmp_path / "lens"))
    listed = []
    for path in report["lists"]:
        listed += Path(path).read_text().split()
    assert sorted(listed) == sorted(p.name for p in images.iterdir())
    assert len(report["groups"]) == 3   # CamA, CamB, and the unreadable frame


# ── masks ───────────────────────────────────────────────────────────────────

def test_people_and_screens_are_masked_furniture_is_not():
    h, w = 40, 60
    person = np.zeros((h, w)); person[10:20, 10:20] = 0.9
    sofa = np.zeros((h, w)); sofa[25:35, 30:50] = 0.9
    tv = np.zeros((h, w)); tv[5:10, 40:55] = 0.9
    stack, labels, scores = np.stack([person, sofa, tv]), ["person", "couch", "tv"], [0.95, 0.99, 0.8]
    keep = tools.keep_mask(stack, labels, scores, dilate_px=0)
    assert not keep[15, 15], "people move"
    assert keep[30, 40], "furniture is the room, not a moving object"
    assert keep[7, 45], "a screen is static unless asked: masked, a switched-off TV becomes a hole"
    with_screens = tools.keep_mask(stack, labels, scores, dilate_px=0,
                                   classes=tools.DYNAMIC_CLASSES + tools.SCREEN_CLASSES)
    assert not with_screens[7, 45]


def test_a_low_confidence_detection_is_not_masked_and_masks_are_dilated():
    h, w = 40, 40
    blob = np.zeros((h, w)); blob[18:22, 18:22] = 1.0
    unsure = tools.keep_mask(blob[None], ["person"], [0.3], dilate_px=0)
    assert unsure.all()
    sure = tools.keep_mask(blob[None], ["person"], [0.9], dilate_px=3)
    assert not sure[16, 20], "the rim around a person is where ghosts form"
    assert sure[5, 5]


def test_no_detections_keeps_everything():
    keep = tools.keep_mask(np.zeros((0, 10, 12)), [], [], shape=(10, 12))
    assert keep.shape == (10, 12) and keep.all()


def test_a_mask_follows_the_images_undistortion():
    cv2 = pytest.importorskip("cv2")
    keep = np.ones((30, 40), dtype=bool)
    keep[:, :20] = False
    mapx, mapy = np.meshgrid(np.arange(40, dtype=np.float32), np.arange(30, dtype=np.float32))
    out = tools.transform_mask(keep, mapx, mapy, roi=(5, 2, 30, 20))
    assert out.shape == (20, 30)
    assert not out[:, :15].any() and out[:, 15:].all()
    del cv2


def _fake_gsplat_dataset(monkeypatch, image, patch_size=None, distorted=False):
    torch = pytest.importorskip("torch")

    class Parser:
        image_names = ["f0.jpg"]
        camera_ids = [1]
        params_dict = {1: np.array([0.1]) if distorted else np.array([])}
        mapx_dict = {1: None}
        mapy_dict = {1: None}
        roi_undist_dict = {1: None}

    class Dataset:
        def __init__(self):
            self.parser = Parser()
            self.indices = np.array([0])
            self.patch_size = patch_size

        def __getitem__(self, item):
            return {"image": torch.from_numpy(image.copy()).float()}

    colmap = types.ModuleType("datasets.colmap")
    colmap.Dataset = Dataset
    package = types.ModuleType("datasets")
    package.colmap = colmap
    monkeypatch.setitem(sys.modules, "datasets", package)
    monkeypatch.setitem(sys.modules, "datasets.colmap", colmap)
    return Dataset, torch


def test_masked_pixels_give_no_supervision(monkeypatch, tmp_path):
    from PIL import Image

    image = np.full((8, 10, 3), 200, dtype=np.uint8)
    Dataset, torch = _fake_gsplat_dataset(monkeypatch, image)
    keep = np.ones((8, 10), dtype=np.uint8) * 255
    keep[2:5, 3:7] = 0
    Image.fromarray(keep).save(tmp_path / "f0.jpg.png")
    tools.patch_dataset_with_masks(str(tmp_path))
    data = Dataset()[0]
    assert data["image"][3, 4].sum().item() == 0, "ground truth must be zeroed under the mask"
    assert data["image"][0, 0].sum().item() == 600
    assert data["mask"].dtype == torch.bool and not data["mask"][3, 4] and data["mask"][0, 0]


def test_a_frame_without_a_mask_is_used_whole(monkeypatch, tmp_path):
    image = np.full((8, 10, 3), 50, dtype=np.uint8)
    Dataset, _torch = _fake_gsplat_dataset(monkeypatch, image)
    tools.patch_dataset_with_masks(str(tmp_path))
    data = Dataset()[0]
    assert "mask" not in data and data["image"].sum().item() == 50 * 8 * 10 * 3


def test_masks_are_skipped_not_misapplied_under_random_patches(monkeypatch, tmp_path):
    from PIL import Image

    image = np.full((8, 10, 3), 50, dtype=np.uint8)
    Dataset, _torch = _fake_gsplat_dataset(monkeypatch, image, patch_size=4)
    Image.fromarray(np.zeros((8, 10), dtype=np.uint8)).save(tmp_path / "f0.jpg.png")
    tools.patch_dataset_with_masks(str(tmp_path))
    data = Dataset()[0]
    assert "mask" not in data


# ── PLY ─────────────────────────────────────────────────────────────────────

def _write_ply(path: Path, n: int = 3, degree: int = 1, comment: str = "") -> np.ndarray:
    k = (degree + 1) ** 2 - 1
    names = (["x", "y", "z"] + [f"f_dc_{i}" for i in range(3)] + [f"f_rest_{i}" for i in range(3 * k)]
             + ["opacity"] + [f"scale_{i}" for i in range(3)] + [f"rot_{i}" for i in range(4)])
    rows = np.arange(n * len(names), dtype=np.float32).reshape(n, len(names)) / 100.0
    header = "ply\nformat binary_little_endian 1.0\n" + (f"comment {comment}\n" if comment else "")
    header += f"element vertex {n}\n" + "".join(f"property float {m}\n" for m in names) + "end_header\n"
    path.write_bytes(header.encode() + rows.tobytes())
    return rows


def test_splats_load_with_channel_major_spherical_harmonics(tmp_path):
    rows = _write_ply(tmp_path / "a.ply", n=2, degree=1)
    s = tools.load_ply_splats(str(tmp_path / "a.ply"))
    assert int(s["sh_degree"]) == 1 and s["sh"].shape == (2, 4, 3)
    # f_rest is stored [3, K] per splat: f_rest_0..2 are band coefficients of RED.
    rest = rows[0, 6:15]
    assert np.allclose(s["sh"][0, 1:, 0], rest[0:3])
    assert np.allclose(s["sh"][0, 1:, 1], rest[3:6])
    assert np.allclose(s["opacities"][0], 1 / (1 + np.exp(-rows[0, 15])), atol=1e-6)
    assert np.allclose(s["scales"][0], np.exp(rows[0, 16:19]), atol=1e-5)


def test_a_partial_sh_band_set_is_refused(tmp_path):
    path = tmp_path / "bad.ply"
    names = ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2", "f_rest_0", "opacity",
             "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
    header = ("ply\nformat binary_little_endian 1.0\nelement vertex 1\n"
              + "".join(f"property float {m}\n" for m in names) + "end_header\n")
    path.write_bytes(header.encode() + struct.pack(f"<{len(names)}f", *([0.0] * len(names))))
    with pytest.raises(ValueError):
        tools.load_ply_splats(str(path))


def test_antialiased_tag_is_added_once_and_the_body_is_untouched(tmp_path):
    path = tmp_path / "t.ply"
    _write_ply(path, n=5, degree=0)
    _lines, _count, body = tools.ply_header(str(path))
    before = path.read_bytes()[body:]
    assert tools.tag_antialiased(str(path)) is True
    lines, count, body2 = tools.ply_header(str(path))
    assert lines[2] == "comment antialiased 1" and count == 5
    assert path.read_bytes()[body2:] == before
    assert tools.tag_antialiased(str(path)) is False


def test_psnr():
    a = np.zeros((4, 4, 3))
    assert tools.psnr(a, a) == 99.0
    assert tools.psnr(a, a + 0.1) == pytest.approx(20.0, abs=0.01)


# ── QA aggregation ──────────────────────────────────────────────────────────

ANALYZER = """I20261006 model.cc:123] Cameras: 2
I20261006 model.cc:124] Images: 150
I20261006 model.cc:125] Registered images: 138
I20261006 model.cc:126] Points: 81234
I20261006 model.cc:127] Observations: 512000
I20261006 model.cc:128] Mean track length: 6.302
I20261006 model.cc:129] Mean observations per image: 3710.1
I20261006 model.cc:130] Mean reprojection error: 0.612px
"""


def test_model_analyzer_numbers_are_parsed():
    out = tools.parse_model_analyzer(ANALYZER)
    assert out["registered_images"] == 138 and out["cameras"] == 2
    assert out["mean_reprojection_error_px"] == pytest.approx(0.612)
    assert tools.parse_model_analyzer("nothing useful") == {}


def test_the_last_evaluated_step_wins(tmp_path):
    stats = tmp_path / "stats"
    stats.mkdir()
    (stats / "val_step6999.json").write_text(json.dumps({"psnr": 20.0, "ssim": 0.7}))
    (stats / "val_step29999.json").write_text(json.dumps({"psnr": 26.5, "ssim": 0.86, "num_GS": 2_000_000}))
    (stats / "train_step29999_rank0.json").write_text("{}")
    out = tools.latest_val_stats(str(stats))
    assert out["psnr"] == 26.5 and out["step"] == 30000 and out["num_GS"] == 2_000_000


def test_quality_report_gathers_what_was_measured_and_nothing_else(tmp_path):
    (tmp_path / "images").mkdir()
    for i in range(150):
        (tmp_path / "images" / f"{i:04d}.jpg").write_bytes(b"x")
    (tmp_path / "model_analyzer.txt").write_text(ANALYZER)
    (tmp_path / "out" / "stats").mkdir(parents=True)
    (tmp_path / "out" / "stats" / "val_step29999.json").write_text(json.dumps({"psnr": 25.0, "ssim": 0.84}))
    (tmp_path / "training.json").write_text(json.dumps({"antialiased": True, "masks": False}))
    (tmp_path / "masks.json").write_text(json.dumps({"frames_masked": 3, "per_frame": {"a": 0.1}}))
    report = tools.build_quality_report(str(tmp_path))
    assert report["colmap"]["registration_ratio"] == pytest.approx(0.92)
    assert report["held_out"]["psnr"] == 25.0
    assert report["training"]["antialiased"] is True
    assert "per_frame" not in report["masks"]
    assert "roundtrip" not in report and "prune" not in report, "absent stays absent"


def test_roundtrip_summary_reports_mean_and_worst_view():
    rows = [{"sog_vs_pruned": 35.0, "raw_vs_photo": 25.0},
            {"sog_vs_pruned": 27.0, "raw_vs_photo": 21.0}]
    out = tools.summarise_roundtrip(rows)
    assert out["mean"]["sog_vs_pruned"] == 31.0 and out["worst"]["sog_vs_pruned"] == 27.0
    assert tools.summarise_roundtrip([]) == {"mean": {}, "worst": {}}


def test_cli_failures_are_reported_not_raised(tmp_path, capsys):
    assert tools.main(["ply-count", str(tmp_path / "missing.ply")]) == 1
    assert tools.main(["no-such-command"]) == 2


# ── the paths that only fully run on a GPU, exercised with stand-ins ────────

def test_train_launches_the_real_trainer_file_with_masks_patched_in(monkeypatch, tmp_path):
    """runpy re-executes simple_trainer.py as __main__; the class patch must
    survive that, and the trainer must receive exactly the CLI it was given."""
    pytest.importorskip("torch")
    from PIL import Image

    examples = tmp_path / "examples"
    (examples / "datasets").mkdir(parents=True)
    (examples / "datasets" / "__init__.py").write_text("")
    (examples / "datasets" / "colmap.py").write_text(
        "import numpy as np, torch\n"
        "class _P:\n"
        "    image_names=['f0.jpg']; camera_ids=[1]; params_dict={1: np.array([])}\n"
        "class Dataset:\n"
        "    def __init__(self): self.parser=_P(); self.indices=np.array([0]); self.patch_size=None\n"
        "    def __getitem__(self, i): return {'image': torch.ones(4, 4, 3) * 9}\n")
    (examples / "simple_trainer.py").write_text(
        "import sys, json\n"
        "from datasets.colmap import Dataset\n"
        "if __name__ == '__main__':\n"
        "    d = Dataset()[0]\n"
        "    json.dump({'argv': sys.argv[1:], 'masked': 'mask' in d,\n"
        "               'zero': float(d['image'][0, 0].sum())}, open(sys.argv[-1], 'w'))\n")
    masks = tmp_path / "masks"
    masks.mkdir()
    keep = np.full((4, 4), 255, dtype=np.uint8)
    keep[0, 0] = 0
    Image.fromarray(keep).save(masks / "f0.jpg.png")
    for name in [m for m in sys.modules if m == "datasets" or m.startswith("datasets.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.chdir(examples)
    monkeypatch.setattr(sys, "path", [str(examples)] + sys.path)
    out = tmp_path / "result.json"
    tools.cmd_train(str(masks), ["default", "--max-steps", "30000", str(out)])
    result = json.loads(out.read_text())
    assert result["argv"] == ["default", "--max-steps", "30000", str(out)]
    assert result["masked"] is True and result["zero"] == 0.0


def test_roundtrip_compares_three_models_on_held_out_views(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")

    class Parser:
        def __init__(self, **kw):
            assert kw["test_every"] == 8 and kw["normalize"] is True

    class Dataset:
        def __init__(self, parser, split):
            assert split == "val"

        def __len__(self):
            return 3

        def __getitem__(self, i):
            return {"image": torch.full((6, 8, 3), 127.5), "K": torch.eye(3),
                    "camtoworld": torch.eye(4)}

    colmap = types.ModuleType("datasets.colmap")
    colmap.Parser, colmap.Dataset = Parser, Dataset
    package = types.ModuleType("datasets")
    package.colmap = colmap
    monkeypatch.setitem(sys.modules, "datasets", package)
    monkeypatch.setitem(sys.modules, "datasets.colmap", colmap)
    for name in ("raw", "pruned", "sog"):
        _write_ply(tmp_path / f"{name}.ply", n=2, degree=0)
    seen = []

    def fake_render(splats, c2w, K, w, h, antialiased):
        seen.append((w, h, antialiased))
        return np.full((h, w, 3), 0.5)

    monkeypatch.setattr(tools, "_render", fake_render)
    report = tools.cmd_roundtrip(str(tmp_path), str(tmp_path / "raw.ply"), str(tmp_path / "pruned.ply"),
                                 str(tmp_path / "sog.ply"), str(tmp_path / "rt.json"), antialiased=True)
    assert report["views"] == 3 and len(seen) == 9
    assert all(s == (8, 6, True) for s in seen), "renders must match the photo size and mode"
    assert report["mean"]["sog_vs_pruned"] == 99.0
    assert json.loads((tmp_path / "rt.json").read_text())["views"] == 3
