"""Execute the WHOLE rendered pod script, end to end, against stub tools.

`bash -n` proves the script parses; it cannot prove the script survives
`set -euo pipefail`. A finished 30 000-step run — COLMAP 150/150, held-out
PSNR 29.6 dB, its .sog already built — died on its very last line because
`phase ""` returned 1 (2026-10-06, $0.30). That is the most expensive place
there is to find a one-line bug, and it is the same class as all five early pod
failures. So this runs the real script, every line, with:

* stub `colmap`, `splat-transform`, `Xvfb`, `pip` and a stub trainer — the GPU
  parts — that write the files the real ones write;
* the REAL quality tools (lens-groups, tag-antialiased, ply-count, qa) and
  real python for every inline snippet; the GPU-only tool commands
  (selftest-render, masks-check, masks, train, roundtrip) are stubbed;
* failure injection (FAKE_FAIL) to prove each optional step is optional.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from reconstruction_providers import render_pod_pipeline  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STEPS = 500

ANALYZER = """Cameras: 1
Registered images: 6
Points: 1200
Observations: 9000
Mean track length: 5.1
Mean reprojection error: 0.52px
"""

_PLY_FIELDS = (["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2"] + [f"f_rest_{i}" for i in range(9)]
               + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])

FAKE_PYTHON = r'''#!__REAL__
"""Stands in for `python` on the pod: GPU-only work is faked, the rest is real."""
import json, os, struct, sys
REAL = "__REAL__"
W = os.environ["NEOH_W"]
FAIL = set(filter(None, os.environ.get("FAKE_FAIL", "").split(",")))
FIELDS = __FIELDS__
argv = sys.argv[1:]

def ply(path, n=40):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head = ("ply\nformat binary_little_endian 1.0\nelement vertex %d\n" % n
            + "".join("property float %s\n" % f for f in FIELDS) + "end_header\n")
    with open(path, "wb") as h:
        h.write(head.encode())
        for i in range(n):
            h.write(struct.pack("<%df" % len(FIELDS), *([i * 0.01] * len(FIELDS))))

def trainer(args, how):
    if how in FAIL:
        sys.exit(1)
    out = args[args.index("--result-dir") + 1]
    steps = int(args[args.index("--max-steps") + 1])
    open(os.path.join(W, "trainer_calls.log"), "a").write(how + " " + " ".join(args) + "\n")
    ply(os.path.join(out, "ply", "point_cloud_%d.ply" % (steps - 1)))
    os.makedirs(os.path.join(out, "stats"), exist_ok=True)
    stats = {"psnr": 27.4, "ssim": 0.88, "lpips": 0.11, "num_GS": 40}
    if "--use-bilateral-grid" in args:
        stats.update({"cc_psnr": 28.1, "cc_ssim": 0.9})
    json.dump(stats, open(os.path.join(out, "stats", "val_step%d.json" % (steps - 1)), "w"))
    sys.exit(0)

if argv[:1] == ["-c"] and any(m in argv[1] for m in ("import gsplat", "import simple_trainer", "import lib_bilagrid")):
    sys.exit(1 if ("import lib_bilagrid" in argv[1] and "bilagrid" in FAIL) else 0)
if argv[:1] == ["simple_trainer.py"]:
    trainer(argv[1:], "plain")
if argv and argv[0].endswith("neoh_recon_tools.py"):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd in FAIL:
        sys.exit(1)
    if cmd in ("selftest-render", "masks-check"):
        sys.exit(0)
    if cmd == "masks":
        os.makedirs(argv[3], exist_ok=True)
        open(os.path.join(argv[3], "0000.jpg.png"), "wb").write(b"png")
        json.dump({"frames": 6, "frames_masked": 1, "mean_masked_fraction": 0.05,
                   "screens": "--screens" in argv}, open(argv[4], "w"))
        sys.exit(0)
    if cmd == "train":
        trainer(argv[argv.index("--") + 2:], "masked")
    if cmd == "roundtrip":
        hurt = "prune_hurts" in FAIL
        json.dump({"views": 3, "mean": {"raw_vs_photo": 27.0, "pruned_vs_photo": 26.0 if hurt else 27.0,
                                        "sog_vs_photo": 26.8, "pruned_vs_raw": 45.0, "sog_vs_pruned": 33.0},
                   "worst": {}}, open(argv[6], "w"))
        sys.exit(0)
os.execv(REAL, [REAL] + argv)
'''

FAKE_COLMAP = r'''#!/bin/bash
echo "colmap $*" >> "$NEOH_W/colmap_calls.log"
case "$1" in
  -h) echo "COLMAP 3.9 (stub)";;
  mapper) out=""; while [ $# -gt 0 ]; do [ "$1" = "--output_path" ] && out="$2"; shift; done; mkdir -p "$out/0";;
  model_analyzer) printf '%s' "$NEOH_ANALYZER" >&2;;
esac
exit 0
'''

FAKE_SPLAT_TRANSFORM = r'''#!/bin/bash
# Last argument is the output, first non-flag argument the input.
echo "splat-transform $*" >> "$NEOH_W/st_calls.log"
args=("$@"); out="${args[-1]}"; in=""
i=0
while [ $i -lt $((${#args[@]} - 1)) ]; do
  a="${args[$i]}"
  case "$a" in
    -g|--gpu|--filter-floaters|-F) i=$((i+2)); continue;;
    -*) i=$((i+1)); continue;;
    *) [ -z "$in" ] && in="$a"; i=$((i+1));;
  esac
done
case "$out" in
  *.sog) printf 'PK\003\004SOG-from-%s' "$(basename "$in")" > "$out";;
  *.ply) case "$in" in *.ply) cp "$in" "$out";; *) cp "$NEOH_W/../template.ply" "$out";; esac;;
esac
'''


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, fail: str = "", screens: bool = False) -> tuple[subprocess.CompletedProcess, Path]:
    from PIL import Image

    w = tmp_path / "workspace"
    bin_dir = tmp_path / "bin"
    (w / "images").mkdir(parents=True)
    (w / "gs" / "examples" / "datasets").mkdir(parents=True)
    bin_dir.mkdir()
    for i in range(6):
        Image.new("RGB", (32, 24), (i * 30, 80, 120)).save(w / "images" / f"{i:04d}.jpg")

    fake_python = (FAKE_PYTHON.replace("__REAL__", sys.executable)
                   .replace("__FIELDS__", json.dumps(_PLY_FIELDS)))
    _write_exec(bin_dir / "python", fake_python)
    _write_exec(bin_dir / "colmap", FAKE_COLMAP)
    _write_exec(bin_dir / "splat-transform", FAKE_SPLAT_TRANSFORM)
    _write_exec(bin_dir / "xvfb-run", "#!/bin/bash\nexit 0\n")
    _write_exec(bin_dir / "pip", "#!/bin/bash\nexit 0\n")
    _write_exec(bin_dir / "Xvfb", '#!/bin/bash\ntouch "$NEOH_X_SOCKET"\nsleep 0.1\n')
    # A valid PLY for "decode the .sog back" to copy.
    subprocess.run([str(bin_dir / "python"), "-c", "pass"], check=True,
                   env={**os.environ, "NEOH_W": str(w)})
    header = ("ply\nformat binary_little_endian 1.0\nelement vertex 1\n"
              + "".join(f"property float {f}\n" for f in _PLY_FIELDS) + "end_header\n")
    (tmp_path / "template.ply").write_bytes(header.encode() + b"\x00" * 4 * len(_PLY_FIELDS))

    script = render_pod_pipeline({"steps": STEPS, "matcher": "sequential", "mask_screens": screens})
    # Via a placeholder: the temp path itself contains "/workspace".
    script = (script.replace("/workspace", "@@W@@")
                    .replace("/tmp/.X11-unix/X99", "@@W@@/X99")
                    .replace("/tmp/.X99-lock", "@@W@@/X99-lock")
                    .replace("@@W@@", str(w)))
    (tmp_path / "pipeline.sh").write_text(script)
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path), "NEOH_W": str(w), "NEOH_X_SOCKET": str(w / "X99"),
        "NEOH_ANALYZER": ANALYZER, "FAKE_FAIL": fail,
    }
    result = subprocess.run(["bash", str(tmp_path / "pipeline.sh")], capture_output=True,
                            text=True, env=env, cwd=str(w), timeout=180)
    return result, w


def _quality(w: Path) -> dict:
    return json.loads((w / "quality.json").read_text())


def test_the_whole_script_runs_to_its_last_line(tmp_path):
    result, w = _run(tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().splitlines()[-1].startswith("OK "), result.stdout[-500:]
    assert (w / "model.sog").stat().st_size > 0
    q = _quality(w)
    assert q["held_out"]["psnr"] == 27.4 and q["held_out"]["step"] == STEPS
    assert q["colmap"]["registration_ratio"] == 1.0
    assert q["training"] == {"steps": STEPS, "antialiased": True, "bilateral_grid": True,
                             "masks": True, "fallback": False}
    assert q["prune"]["accepted"] is True
    assert q["roundtrip"]["mean"]["sog_vs_pruned"] == 33.0
    # The final phase is closed, not left open for the provider to guess.
    phases = [json.loads(l)["phase"] for l in (w / "phases.jsonl").read_text().splitlines()]
    assert phases[-1] == "quality_check"
    # Masks reached COLMAP; one camera per lens group; PLY tagged antialiased.
    calls = (w / "colmap_calls.log").read_text()
    assert "--ImageReader.mask_path" in calls and "--image_list_path" in calls
    assert "comment antialiased 1" in (w / "out" / "ply" / f"point_cloud_{STEPS - 1}.ply").read_bytes()[:400].decode("latin-1")
    assert "masked" in (w / "trainer_calls.log").read_text()


def test_every_optional_quality_step_can_fail_and_the_space_still_ships(tmp_path):
    result, w = _run(tmp_path, fail="selftest-render,masks-check,lens-groups,qa,bilagrid")
    assert result.returncode == 0, result.stderr[-3000:]
    assert (w / "model.sog").stat().st_size > 0
    calls = (w / "colmap_calls.log").read_text()
    assert "--ImageReader.mask_path" not in calls and "--image_list_path" not in calls
    assert "--use-bilateral-grid" not in (w / "trainer_calls.log").read_text()
    assert not (w / "quality.json").exists()


def test_a_failed_enhanced_training_falls_back_to_the_proven_trainer(tmp_path):
    result, w = _run(tmp_path, fail="masked")
    assert result.returncode == 0, result.stderr[-3000:]
    log = (w / "trainer_calls.log").read_text()
    assert log.startswith("plain ") and "--antialiased" not in log
    q = _quality(w)
    assert q["training"]["fallback"] is True and q["training"]["antialiased"] is False
    assert q["training"]["masks"] is False


def test_pruning_that_costs_detail_is_reverted(tmp_path):
    result, w = _run(tmp_path, fail="prune_hurts")
    assert result.returncode == 0, result.stderr[-3000:]
    prune = json.loads((w / "prune.json").read_text())
    assert prune["accepted"] is False and prune["reverted"]
    assert (w / "model.sog").read_bytes().endswith(f"point_cloud_{STEPS - 1}.ply".encode())


def test_a_failed_round_trip_is_not_fatal(tmp_path):
    result, w = _run(tmp_path, fail="roundtrip")
    assert result.returncode == 0, result.stderr[-3000:]
    assert "roundtrip" not in _quality(w)


def test_screens_are_masked_only_on_request(tmp_path):
    result, w = _run(tmp_path)
    assert json.loads((w / "masks.json").read_text())["screens"] is False
    result, w = _run(tmp_path / "s", screens=True)
    assert result.returncode == 0
    assert json.loads((w / "masks.json").read_text())["screens"] is True


def test_the_plain_trainer_failing_is_still_fatal(tmp_path):
    """The one step that must fail loudly: no training, no space."""
    result, _w = _run(tmp_path, fail="masked,plain")
    assert result.returncode != 0
