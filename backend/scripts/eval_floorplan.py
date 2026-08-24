#!/usr/bin/env python3
"""End-to-end floor plan benchmark: does a change produce BETTER PLANS?

Per-point validation accuracy is not the question this pipeline is judged on,
and the two come apart badly. A retrained segmenter measured 0.984 wall
precision and 0.948 recall — better, on paper, than the model it was meant to
replace — and produced 61% correct room counts against that model's 95%, with
nine outright failures in sixty houses. It had learned to call wall points
clutter, which costs almost nothing per point and takes the whole plan with it,
because a wall mask with holes does not close a room.

So: measure plans. Room count, area against known truth, and refusals.

    python scripts/eval_floorplan.py --mode auto --houses 60
    python scripts/eval_floorplan.py --mode segmenter --model /tmp/candidate.onnx

Everything here is SYNTHETIC. Rooms are rectilinear, walls are clean planes,
and coverage falls off from a walked path. It measures whether a change helps
relative to another change; it does not measure accuracy on a real capture, and
no number it prints should be quoted as if it did.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from floorplan_pipeline import segmentation, slicing
from floorplan_pipeline.errors import DegenerateGeometry, MissingScale, UnsupportedInput

#: Points per square metre of surface. Roughly what a photogrammetric
#: reconstruction of an interior yields once the low-opacity haze is dropped.
DENSITY = 320.0


def _plane(rng, origin, u, v, jitter=0.004):
    u, v = np.asarray(u, dtype="float64"), np.asarray(v, dtype="float64")
    area = float(np.linalg.norm(np.cross(u, v)))
    count = max(150, int(area * DENSITY))
    a, b = rng.random((count, 1)), rng.random((count, 1))
    return np.asarray(origin, dtype="float64") + a * u + b * v + rng.normal(
        0, jitter, (count, 3)
    )


def house(rng):
    """A grid-partitioned house with floor-standing clutter, and its truth."""
    width = float(rng.uniform(6.0, 14.0))
    depth = float(rng.uniform(5.0, 11.0))
    height = float(rng.uniform(2.35, 2.9))
    across = int(rng.integers(1, 4))
    along = int(rng.integers(1, 3))

    xs = [0.0, width]
    if across > 1:
        xs = [0.0] + sorted(rng.uniform(0.25, 0.75, across - 1) * width) + [width]
    ys = [0.0, depth]
    if along > 1:
        ys = [0.0] + sorted(rng.uniform(0.25, 0.75, along - 1) * depth) + [depth]

    parts = [
        _plane(rng, [0, 0, 0], [width, 0, 0], [0, depth, 0]),
        _plane(rng, [0, 0, height], [width, 0, 0], [0, depth, 0]),
    ]
    for x in xs:
        parts.append(_plane(rng, [x, 0, 0], [0, depth, 0], [0, 0, height]))
    for y in ys:
        parts.append(_plane(rng, [0, y, 0], [width, 0, 0], [0, 0, height]))

    # Floor-standing clutter, which is what the ceiling-adjacent band and the
    # segmenter both exist to see past.
    for _ in range(int(rng.integers(2, 6))):
        px = float(rng.uniform(0.5, width - 1.5))
        py = float(rng.uniform(0.5, depth - 1.5))
        tall = float(rng.uniform(0.7, 1.95))
        wide = float(rng.uniform(0.6, 1.8))
        parts.append(_plane(rng, [px, py, 0], [wide, 0, 0], [0, 0, tall]))
        parts.append(_plane(rng, [px, py, tall], [wide, 0, 0],
                            [0, float(rng.uniform(0.4, 0.9)), 0]))

    area = sum(
        (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
        for i in range(len(xs) - 1) for j in range(len(ys) - 1)
    )
    truth = {"rooms": (len(xs) - 1) * (len(ys) - 1), "area": area,
             "width": width, "depth": depth, "height": height}
    return np.vstack(parts), truth


def rotate(rng, points):
    """Into an arbitrary frame — structure-from-motion has no idea about gravity."""
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    if np.linalg.det(q) < 0:
        q[:, 0] = -q[:, 0]
    return points @ q.T, q


def ply(points) -> bytes:
    header = (
        f"ply\nformat binary_little_endian 1.0\nelement vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\nend_header\n"
    ).encode()
    return header + points.astype("<f4").tobytes()


def benchmark(houses: int, seed: int, mode):
    results = []
    for index in range(houses):
        rng = np.random.default_rng(seed + index)
        points, truth = house(rng)
        rotated, _ = rotate(rng, points)
        record = {"truth": truth, "index": index}
        try:
            document = slicing.extract_from_reconstruction(
                ply(rotated), use_segmenter=mode, metres_per_unit=1.0
            )
        except (DegenerateGeometry, UnsupportedInput, MissingScale) as exc:
            record["failure"] = type(exc).__name__
        else:
            record["rooms"] = len(document.rooms)
            record["area"] = document.total_area_m2
            record["coverage"] = slicing._coverage(document)
            record["error"] = abs(document.total_area_m2 - truth["area"]) / truth["area"]
        results.append(record)
    return results


def report(label: str, results) -> dict:
    scored = [r for r in results if "error" in r]
    failures = [r for r in results if "failure" in r]
    exact = [r for r in scored if r["rooms"] == r["truth"]["rooms"]]
    accuracy = 100 * float(np.mean([1 - r["error"] for r in scored])) if scored else 0.0
    coverage = [r["coverage"] for r in scored if r["coverage"]]

    print(
        f"{label:22s} n={len(results):3d}  refused={len(failures):2d}  "
        f"room-exact={100 * len(exact) / max(1, len(results)):5.1f}%  "
        f"area={accuracy:5.2f}%  "
        f"median-coverage={np.median(coverage) if coverage else float('nan'):.3f}"
    )
    if failures:
        counts = defaultdict(int)
        for r in failures:
            counts[r["failure"]] += 1
        print(f"{'':22s}   refusals: {dict(counts)}")

    # Signed bias by room count: a uniform offset error shows up here as a
    # trend, where the aggregate average hides it.
    bias = defaultdict(list)
    for r in exact:
        bias[r["truth"]["rooms"]].append(
            (r["area"] - r["truth"]["area"]) / r["truth"]["area"]
        )
    if bias:
        trend = "  ".join(
            f"{k}r {100 * float(np.mean(v)):+.2f}%" for k, v in sorted(bias.items())
        )
        print(f"{'':22s}   signed bias by room count: {trend}")

    return {"room_exact": len(exact) / max(1, len(results)),
            "area": accuracy, "refused": len(failures)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--houses", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--mode", default="all",
                        choices=("all", "geometry", "segmenter", "auto"))
    parser.add_argument("--model", type=Path, default=None,
                        help="segmenter .onnx to evaluate (defaults to the installed one)")
    args = parser.parse_args()

    if args.model is not None:
        os.environ[segmentation.MODEL_ENV] = str(args.model)
        segmentation.reset_cache()
    ready, reason = segmentation.available()
    print(f"segmenter: {'installed' if ready else reason}")
    print("SYNTHETIC houses — relative comparison only, not real-capture accuracy.\n")

    modes = {"geometry": False, "segmenter": True, "auto": "auto"}
    chosen = modes if args.mode == "all" else {args.mode: modes[args.mode]}
    for label, mode in chosen.items():
        report(label, benchmark(args.houses, args.seed, mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
