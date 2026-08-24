"""Per-point geometric features — the ONE place training and inference agree.

This module exists in this shape for a single reason: **train/serve skew is
silent**. If the trainer computes a feature one way and the backend computes it
another, the model still returns confident probabilities, the plan still looks
plausible, and nothing anywhere raises. Every other failure in this pipeline is
loud; that one is not. So both sides import `extract`, and the feature order is
fixed by `FEATURE_NAMES` rather than by whatever order a dict happens to iterate.

The features are deliberately interpretable rather than learned-from-raw-points.
A PointNet-family model over raw coordinates would need a GPU at inference and
would be a black box making a claim about someone's home. These are the
quantities a surveyor would name, and a small MLP over them runs on CPU in
milliseconds:

    height_norm     where the point sits between floor and ceiling
    verticality     is the local surface horizontal (floor/ceiling) or upright (wall)
    planarity       is the neighbourhood flat, or scattered like foliage/clutter
    linearity       is it an edge — a wall junction, a table leg
    column_span     how much of the FULL room height this point's column covers
    column_density  how much of the column is occupied rather than empty
    local_density   sample density, which separates surfaces from stray floaters

`column_span` is the load-bearing one, and it is why furniture and walls are
separable at all. A wall occupies its vertical column from floor to ceiling. A
wardrobe occupies three quarters of it, a worktop a third, a rug none of it.
The height histogram cannot see this because it collapses the x/y axes; the
column can.

All features are scale-free by construction — ratios and normalised heights, no
absolute lengths. That is required, not incidental: a reconstruction has no
metric scale, so a feature in metres would be meaningless, and a model trained
on one would learn the scale of its training set.
"""

from __future__ import annotations

from typing import Optional

#: Fixed order. The ONNX graph's input columns are positional, so reordering
#: this silently rewires every feature to the wrong weight.
FEATURE_NAMES = (
    "height_norm",
    "verticality",
    "planarity",
    "linearity",
    "column_span",
    "column_density",
    "local_density",
    # --- context, added in v2 ---------------------------------------------
    # v1 classified every point from its own column alone, which cannot express
    # the thing that most distinguishes a wall: a wall is a LINE of full-height
    # columns, a wardrobe is a blob of them. Two points with identical column
    # statistics are different if one sits in a straight run and the other does
    # not, and v1 had no way to say so.
    "run_length",
    "neighbour_span",
    "neighbour_agreement",
)

#: Class ids the segmenter predicts. `clutter` covers furniture, occupants,
#: plants and anything else that is in the room rather than part of it.
LABELS = ("floor", "ceiling", "wall", "clutter")
LABEL_FLOOR, LABEL_CEILING, LABEL_WALL, LABEL_CLUTTER = range(4)

#: Cells across the longest horizontal axis when building the column grid.
#: Fine enough that a 0.6 m column is its own cell in a typical room, coarse
#: enough that each cell holds enough samples to be a statistic.
COLUMN_CELLS = 64

#: Cells along the vertical axis for occupancy within a column.
COLUMN_LEVELS = 24


def _require_numpy():
    import numpy as np

    return np


def extract(xyz, up, *, floor: float, ceiling: Optional[float] = None):
    """Feature matrix (N, len(FEATURE_NAMES)) for a gravity-aligned cloud.

    `up` must be the unit up-vector from slicing.estimate_up_axis, and `floor`
    the height of the floor plane along it. `ceiling` may be None when the
    capture never saw one — heights are then normalised against the observed
    extent, which is the same convention slicing uses for its fallback band.
    """
    np = _require_numpy()

    xyz = np.asarray(xyz, dtype="float64")
    axis = np.asarray(up, dtype="float64")
    axis = axis / np.linalg.norm(axis)

    # Ground plane basis. Any pair orthogonal to up will do; the features are
    # rotation-invariant in the horizontal plane by construction.
    seed = np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    right = np.cross(axis, seed)
    right /= np.linalg.norm(right)
    forward = np.cross(axis, right)

    heights = xyz @ axis
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)

    top = float(ceiling) if ceiling is not None else float(np.percentile(heights, 99.0))
    extent = max(top - floor, 1e-6)
    height_norm = np.clip((heights - floor) / extent, -0.5, 1.5)

    # --- column statistics -------------------------------------------------
    x0, x1 = float(planar[:, 0].min()), float(planar[:, 0].max())
    y0, y1 = float(planar[:, 1].min()), float(planar[:, 1].max())
    span_x, span_y = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    cell = max(span_x, span_y) / COLUMN_CELLS

    cx = np.clip(((planar[:, 0] - x0) / cell).astype("int64"), 0, COLUMN_CELLS)
    cy = np.clip(((planar[:, 1] - y0) / cell).astype("int64"), 0, COLUMN_CELLS)
    column = cx * (COLUMN_CELLS + 1) + cy

    level = np.clip(
        ((heights - floor) / extent * (COLUMN_LEVELS - 1)).astype("int64"),
        0, COLUMN_LEVELS - 1,
    )

    n_columns = (COLUMN_CELLS + 1) ** 2
    # Occupancy of (column, level) without materialising the full grid: a
    # 65x65x24 dense array is fine, but the same trick keeps this linear if the
    # resolution is ever raised.
    occupancy = np.zeros((n_columns, COLUMN_LEVELS), dtype=bool)
    occupancy[column, level] = True
    occupied_levels = occupancy.sum(axis=1)

    # How tall is the occupied part of this column, and how solid is it?
    highest = np.where(occupancy.any(axis=1), occupancy.shape[1] - 1 -
                       np.argmax(occupancy[:, ::-1], axis=1), 0)
    lowest = np.where(occupancy.any(axis=1), np.argmax(occupancy, axis=1), 0)
    column_span_by_cell = (highest - lowest + 1) / COLUMN_LEVELS
    column_density_by_cell = occupied_levels / COLUMN_LEVELS

    column_span = column_span_by_cell[column]
    column_density = column_density_by_cell[column]

    counts = np.bincount(column, minlength=n_columns).astype("float64")
    local_density = counts[column] / max(1.0, counts.max())

    run_length, neighbour_span, neighbour_agreement = _context(
        np, column_span_by_cell, cx, cy, column
    )

    # --- local shape -------------------------------------------------------
    verticality, planarity, linearity = _local_shape(np, xyz, column, n_columns, axis)

    return np.stack([
        height_norm,
        verticality,
        planarity,
        linearity,
        column_span,
        column_density,
        local_density,
        run_length,
        neighbour_span,
        neighbour_agreement,
    ], axis=1).astype("float32")


def _context(np, span_by_cell, cx, cy, column):
    """Neighbourhood structure around each column.

    This is what v1 could not see. A wall and a wardrobe can have identical
    column statistics — both are tall, both are dense — and the difference is
    entirely in what surrounds them. A wall's neighbours continue in a straight
    line for metres; a wardrobe's stop after half of one.

    Three signals, all computed on the column grid the caller already built:

      run_length          longest straight run of tall columns through this
                          one, along either grid axis, normalised. The strongest
                          of the three, and the reason for the whole function.
      neighbour_span      how tall the surrounding columns are, which separates
                          a wall from an isolated tall object.
      neighbour_agreement how many neighbours are similarly tall, which
                          separates a flat surface from a noisy cluster.
    """
    size = COLUMN_CELLS + 1
    grid = span_by_cell.reshape(size, size)
    # "Tall" relative to the building rather than absolute: a bungalow and a
    # warehouse must give the same answer, and the features stay scale-free.
    tall = grid >= 0.6

    # Longest contiguous run of tall cells through each cell, per axis. Done
    # with cumulative counts rather than a scan so it stays vectorised.
    def _runs(mask):
        out = np.zeros_like(mask, dtype="float64")
        for axis_index in (0, 1):
            m = mask if axis_index == 0 else mask.T
            forward = np.zeros_like(m, dtype="int32")
            backward = np.zeros_like(m, dtype="int32")
            running = np.zeros(m.shape[1], dtype="int32")
            for row in range(m.shape[0]):
                running = np.where(m[row], running + 1, 0)
                forward[row] = running
            running[:] = 0
            for row in range(m.shape[0] - 1, -1, -1):
                running = np.where(m[row], running + 1, 0)
                backward[row] = running
            length = np.where(m, forward + backward - 1, 0).astype("float64")
            out = np.maximum(out, length if axis_index == 0 else length.T)
        return out

    run = _runs(tall) / size

    padded = np.pad(grid, 1, mode="edge")
    stacked = np.stack([
        padded[dy:dy + size, dx:dx + size]
        for dy in range(3) for dx in range(3)
    ])
    neighbour_mean = stacked.mean(axis=0)
    agreement = (np.abs(stacked - grid) < 0.15).mean(axis=0)

    flat_run = run.reshape(-1)
    flat_mean = neighbour_mean.reshape(-1)
    flat_agree = agreement.reshape(-1)
    return flat_run[column], flat_mean[column], flat_agree[column]


def _local_shape(np, xyz, column, n_columns, axis):
    """Per-column PCA → verticality, planarity, linearity for each point.

    Grouped by column rather than by k-nearest-neighbour on purpose. A k-NN
    pass over a few hundred thousand points needs a spatial index and dominates
    the runtime; grouping by a grid cell the pipeline has already computed is
    linear, needs no index, and gives the same discrimination at this scale —
    a column is a narrow vertical prism, so the points in it belong to the same
    surface far more often than not.
    """
    order = np.argsort(column, kind="stable")
    sorted_columns = column[order]
    boundaries = np.flatnonzero(np.diff(sorted_columns)) + 1
    groups = np.split(order, boundaries)

    verticality = np.zeros(len(xyz))
    planarity = np.zeros(len(xyz))
    linearity = np.zeros(len(xyz))

    for members in groups:
        if len(members) < 6:
            # Too few to describe a surface. Left at zero rather than guessed;
            # the model can learn that "no local shape" is itself a signal.
            continue
        block = xyz[members]
        centred = block - block.mean(axis=0)
        # Covariance eigenvalues, descending.
        values, vectors = np.linalg.eigh(centred.T @ centred / len(block))
        values = values[::-1]
        vectors = vectors[:, ::-1]
        total = float(values.sum()) or 1e-12
        e1, e2, e3 = (float(v) / total for v in values)

        normal = vectors[:, 2]
        verticality[members] = abs(float(np.dot(normal, axis)))
        planarity[members] = (e2 - e3) / max(e1, 1e-12)
        linearity[members] = (e1 - e2) / max(e1, 1e-12)

    return verticality, planarity, linearity
