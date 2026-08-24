"""Train the floor-plan point segmenter and export it to ONNX.

    python -m scripts.train_point_segmenter --houses 400 --out \\
        floorplan_pipeline/models/point_segmenter.onnx

Runs on CPU in a few minutes; `--device cuda` on a Colab GPU if the dataset is
scaled up. Needs torch, which is a TRAINING dependency only — the backend
imports onnxruntime and never torch.

Why synthetic data rather than captured rooms: the label is per-point and there
is no way to hand-label a hundred thousand points per house. A generator knows
the answer by construction, so every point is labelled exactly, and the
distribution can be steered — more clutter, missing ceilings, odd aspect ratios
— to cover the cases that actually break the geometric path.

The obvious risk with synthetic training is a model that learns the generator
rather than the world. Three things push against it here:

  * the features are scale-free ratios, so the model cannot key on room sizes;
  * every house is rotated into a random frame, so it cannot key on axes;
  * noise, dropout and partial captures are sampled per house, so it cannot
    assume clean complete coverage.

It remains a real limitation and is stated in the model card rather than hidden:
this is trained on synthetic geometry, and its accuracy on real reconstructions
is unmeasured until someone evaluates it on one.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from floorplan_pipeline.pointfeatures import (  # noqa: E402
    FEATURE_NAMES,
    LABEL_CEILING,
    LABEL_CLUTTER,
    LABEL_FLOOR,
    LABEL_WALL,
    LABELS,
    extract,
)


# ---------------------------------------------------------------------------
# Synthetic houses, labelled by construction
# ---------------------------------------------------------------------------

def _plane(rng, origin, u, v, n, jitter):
    a, b = rng.random((n, 1)), rng.random((n, 1))
    pts = np.asarray(origin, dtype="float64") + a * np.asarray(u) + b * np.asarray(v)
    return pts + rng.normal(0, jitter, pts.shape)


def synth_house(rng):
    """One labelled house. Returns (xyz, labels, up, floor, ceiling_or_None)."""
    width = rng.uniform(4.0, 16.0)
    depth = rng.uniform(3.5, 13.0)
    height = rng.uniform(2.3, 3.2)
    jitter = rng.uniform(0.002, 0.012)
    density = rng.uniform(0.6, 1.6)

    def count(base):
        return max(64, int(base * density))

    clouds, labels = [], []

    def add(points, label):
        clouds.append(points)
        labels.append(np.full(len(points), label, dtype="int64"))

    add(_plane(rng, [0, 0, 0], [width, 0, 0], [0, depth, 0], count(9000), jitter), LABEL_FLOOR)

    # A capture that never looked up is common with hand-held photo sweeps, and
    # it is exactly the case the geometric fallback exists for — so the model
    # has to see it during training.
    saw_ceiling = rng.random() > 0.3
    if saw_ceiling:
        add(_plane(rng, [0, 0, height], [width, 0, 0], [0, depth, 0],
                   count(int(9000 * rng.uniform(0.3, 1.0))), jitter), LABEL_CEILING)

    for origin, u in (
        ([0, 0, 0], [width, 0, 0]),
        ([0, depth, 0], [width, 0, 0]),
        ([0, 0, 0], [0, depth, 0]),
        ([width, 0, 0], [0, depth, 0]),
    ):
        wall = _plane(rng, origin, u, [0, 0, height], count(5000), jitter)
        # Punch a doorway or window. A real wall is not a solid rectangle of
        # points, and a model that has only seen solid ones treats the gap
        # around a door as evidence of clutter.
        for _ in range(rng.integers(0, 3)):
            along = wall @ np.asarray(u, dtype="float64") / (np.linalg.norm(u) ** 2)
            level = (wall[:, 2] - 0) / height
            start = rng.uniform(0.05, 0.75)
            hole = ((along > start) & (along < start + rng.uniform(0.08, 0.22))
                    & (level < rng.uniform(0.6, 0.95)))
            wall = wall[~hole]
        add(wall, LABEL_WALL)

    for _ in range(rng.integers(0, 3)):
        if rng.random() < 0.5:
            x = rng.uniform(width * 0.25, width * 0.75)
            add(_plane(rng, [x, 0, 0], [0, depth, 0], [0, 0, height], count(4000), jitter),
                LABEL_WALL)
        else:
            y = rng.uniform(depth * 0.25, depth * 0.75)
            add(_plane(rng, [0, y, 0], [width, 0, 0], [0, 0, height], count(4000), jitter),
                LABEL_WALL)

    # Clutter: the thing that must not become a wall. Heights span worktops
    # (0.9), islands, counters, wardrobes and full-height units that reach
    # almost to the ceiling — the hardest case, and the one a height band
    # cannot separate at all.
    for _ in range(rng.integers(2, 9)):
        top = rng.uniform(0.4, height * rng.uniform(0.7, 0.98))
        w = rng.uniform(0.5, 2.4)
        d = rng.uniform(0.4, 1.6)
        x = rng.uniform(0.2, max(0.3, width - w - 0.2))
        y = rng.uniform(0.2, max(0.3, depth - d - 0.2))
        add(_plane(rng, [x, y, 0], [w, 0, 0], [0, 0, top], count(900), jitter), LABEL_CLUTTER)
        add(_plane(rng, [x, y, 0], [0, d, 0], [0, 0, top], count(700), jitter), LABEL_CLUTTER)
        add(_plane(rng, [x, y, top], [w, 0, 0], [0, d, 0], count(700), jitter), LABEL_CLUTTER)

    # Reconstruction floaters: low-opacity haze in open space. Labelled clutter
    # because that is what they are — not part of the building.
    floaters = rng.uniform([-1, -1, 0], [width + 1, depth + 1, height],
                           size=(count(int(1200 * rng.uniform(0, 1.5))), 3))
    if len(floaters):
        add(floaters, LABEL_CLUTTER)

    xyz = np.vstack(clouds)
    label = np.concatenate(labels)

    # Coverage is NOT uniform. A photographer walks a path and shoots outward,
    # so density falls off with distance from that path and whole corners go
    # unobserved. Uniform dropout trains a model that assumes even sampling,
    # which is the one thing a real capture never is.
    path = np.stack([
        rng.uniform(0.2, 0.8, 6) * width,
        rng.uniform(0.2, 0.8, 6) * depth,
    ], axis=1)
    nearest = np.min(
        np.linalg.norm(xyz[:, None, :2] - path[None, :, :], axis=2), axis=1
    )
    reach = rng.uniform(0.25, 1.0) * max(width, depth)
    seen = np.exp(-nearest / max(reach, 1e-6))
    keep = rng.random(len(xyz)) < np.clip(seen * rng.uniform(0.7, 1.0), 0.05, 1.0)
    if keep.sum() < 2000:                      # never starve the sample entirely
        keep = rng.random(len(xyz)) > 0.4
    xyz, label = xyz[keep], label[keep]

    # Into an arbitrary frame, the way structure-from-motion delivers it.
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    if np.linalg.det(q) < 0:
        q[:, 0] = -q[:, 0]
    xyz = xyz @ q.T
    up = q @ np.array([0.0, 0.0, 1.0])
    floor_h = float((xyz @ up).min())
    return xyz, label, up, floor_h, (floor_h + height if saw_ceiling else None)


def build_dataset(houses: int, seed: int):
    rng = np.random.default_rng(seed)
    features, targets = [], []
    for index in range(houses):
        xyz, label, up, floor, ceiling = synth_house(rng)
        # Identical call to the one the backend makes. This is the whole
        # defence against train/serve skew.
        features.append(extract(xyz, up, floor=floor, ceiling=ceiling))
        targets.append(label)
        if (index + 1) % 25 == 0:
            print(f"  {index + 1}/{houses} houses", flush=True)
    return np.vstack(features), np.concatenate(targets)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def train(features, targets, *, device: str, epochs: int, seed: int,
          columns: int | None = None):
    """`columns` trims to the first N features, for ablation."""
    if columns is not None:
        features = features[:, :columns]

    import torch
    from torch import nn

    torch.manual_seed(seed)
    x = torch.tensor(features, dtype=torch.float32)
    y = torch.tensor(targets, dtype=torch.long)

    split = int(len(x) * 0.85)
    order = torch.randperm(len(x))
    train_idx, val_idx = order[:split], order[split:]

    # Small on purpose. Seven interpretable inputs do not need capacity, they
    # need a decision boundary — and this has to run on CPU inside a request.
    width = features.shape[1]
    model = nn.Sequential(
        nn.Linear(width, 64), nn.ReLU(),
        nn.Linear(64, 64), nn.ReLU(),
        nn.Linear(64, len(LABELS)),
    ).to(device)

    # Clutter vastly outnumbers ceiling in a partial capture, and an unweighted
    # loss happily predicts "never ceiling" for a good score.
    counts = torch.bincount(y, minlength=len(LABELS)).float()
    weights = (counts.sum() / (len(LABELS) * counts.clamp(min=1))).to(device)

    loss_fn = nn.CrossEntropyLoss(weight=weights)
    optimiser = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)

    x_train, y_train = x[train_idx].to(device), y[train_idx].to(device)
    x_val, y_val = x[val_idx].to(device), y[val_idx].to(device)

    batch = 8192
    for epoch in range(epochs):
        model.train()
        perm = torch.randperm(len(x_train), device=device)
        for start in range(0, len(perm), batch):
            idx = perm[start:start + batch]
            optimiser.zero_grad()
            loss = loss_fn(model(x_train[idx]), y_train[idx])
            loss.backward()
            optimiser.step()

        model.eval()
        with torch.no_grad():
            predicted = model(x_val).argmax(dim=1)
            accuracy = (predicted == y_val).float().mean().item()
        print(f"  epoch {epoch + 1:3d}/{epochs}  val acc {accuracy:.4f}", flush=True)

    model.eval()
    with torch.no_grad():
        predicted = model(x_val).argmax(dim=1)
    report = {}
    for index, name in enumerate(LABELS):
        actual = y_val == index
        called = predicted == index
        hit = (actual & called).sum().item()
        report[name] = {
            "support": int(actual.sum().item()),
            "recall": round(hit / max(1, actual.sum().item()), 4),
            "precision": round(hit / max(1, called.sum().item()), 4),
        }
    return model.cpu(), report


def export(model, out: Path, report: dict, *, houses: int, epochs: int) -> None:
    import torch

    out.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, len(FEATURE_NAMES), dtype=torch.float32)
    # dynamo=False keeps the legacy exporter, which INLINES the weights. The
    # dynamo path externalises them into a sibling .onnx.data, and a 19 KB model
    # split across two files is a deployment trap: copy the .onnx alone and it
    # loads, then fails at the first inference.
    torch.onnx.export(
        model, dummy, str(out),
        input_names=["features"], output_names=["logits"],
        # Batch is the point count, which differs per capture.
        dynamic_axes={"features": {0: "points"}, "logits": {0: "points"}},
        opset_version=17,
        dynamo=False,
    )
    stray = out.with_suffix(".onnx.data")
    if stray.exists():
        stray.unlink()

    # A model card beside the weights. The training distribution is the single
    # most important fact about this artifact and the easiest to lose.
    card = {
        "features": list(FEATURE_NAMES),
        "labels": list(LABELS),
        "trained_on": "synthetic rooms only — accuracy on real reconstructions is UNMEASURED",
        "houses": houses,
        "epochs": epochs,
        "validation": report,
        "caveats": [
            "Classifies points; never produces a dimension or a scale.",
            "Features are scale-free ratios, so the model cannot key on room size.",
            "Every training house is randomly rotated, so it cannot key on axes.",
            "Rectilinear rooms only — no curved or non-Manhattan walls were generated.",
            "Coverage is modelled as fall-off from a walked path, not uniform dropout.",
            "Walls carry punched door/window openings; solid-wall training taught the "
            "v1 model to read the gap beside a door as clutter.",
        ],
        "real_data": {
            "status": "NOT USED — accuracy on real reconstructions remains unmeasured",
            "why": (
                "S3DIS and ScanNet carry the exact classes this model predicts, but both "
                "are research-license datasets whose commercial terms are not publicly "
                "stated, and this ships inside a paid product."
            ),
            "candidate": (
                "3DSES (zenodo.org/records/13323342) is CC-BY-SA-4.0, so commercial use is "
                "permitted with attribution. EVALUATING on it creates no derivative work "
                "and would convert 'unmeasured' into a number. TRAINING on it raises a "
                "share-alike question about the resulting weights that needs a decision, "
                "not an assumption."
            ),
        },
    }
    out.with_suffix(".json").write_text(json.dumps(card, indent=2))
    print(f"\nWrote {out} and {out.with_suffix('.json')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--houses", type=int, default=200)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--ablate", action="store_true",
                        help="train with and without the v2 context features and compare")
    parser.add_argument(
        "--out", type=Path,
        default=Path(__file__).resolve().parents[1]
        / "floorplan_pipeline" / "models" / "point_segmenter.onnx",
    )
    args = parser.parse_args()

    print(f"Generating {args.houses} synthetic houses…", flush=True)
    features, targets = build_dataset(args.houses, args.seed)
    print(f"  {len(features):,} points, {features.shape[1]} features")
    for index, name in enumerate(LABELS):
        print(f"    {name:8s} {int((targets == index).sum()):>10,}")

    if args.ablate:
        # Same data, same seed, same schedule — only the feature set differs.
        # Without this, "the new features helped" is an assumption dressed as a
        # result, since the generator changed at the same time.
        print("\nAblation: does the v2 context actually help?", flush=True)
        for columns, label in ((7, "v1  own column only"), (None, "v2  + neighbourhood")):
            _, scores = train(features, targets, device=args.device,
                              epochs=args.epochs, seed=args.seed, columns=columns)
            wall = scores["wall"]
            clutter = scores["clutter"]
            print(f"  {label:24s} wall P {wall['precision']:.3f} R {wall['recall']:.3f}"
                  f" | clutter P {clutter['precision']:.3f} R {clutter['recall']:.3f}",
                  flush=True)

    print(f"\nTraining on {args.device}…", flush=True)
    model, report = train(
        features, targets, device=args.device, epochs=args.epochs, seed=args.seed
    )

    print("\nValidation:")
    for name, scores in report.items():
        print(f"  {name:8s} precision {scores['precision']:.3f}  "
              f"recall {scores['recall']:.3f}  (n={scores['support']:,})")

    export(model, args.out, report, houses=args.houses, epochs=args.epochs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
