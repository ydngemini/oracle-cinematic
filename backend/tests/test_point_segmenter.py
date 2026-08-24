"""The learned point segmenter: optional, scale-free, and never a measurement.

It classifies each point as floor / ceiling / wall / clutter so the wall mask
comes from a decision about every point rather than from a height band that
hopes furniture is short.

Two properties are defended here above all else.

**It must be optional.** A model file is a deployment artifact. If the plan only
works where somebody remembered to ship a .onnx, the plan silently stops
working — so a missing model returns None and the geometric path runs, and it
must never raise.

**It must not touch scale.** A classifier being wrong costs a misplaced wall,
which is visible in the plan. A learned SCALE being wrong multiplies every
length and area by a constant and looks entirely correct. So no output of this
model may produce a dimension, and MissingScale still refuses with it enabled.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from floorplan_pipeline import segmentation, slicing
from floorplan_pipeline.errors import MissingScale
from floorplan_pipeline.pointfeatures import FEATURE_NAMES, LABELS, extract


W, D, H = 8.0, 6.0, 2.5
PARTITION_X = 4.6


def _plane(rng, p0, u, v, n=7000, jitter=0.004):
    a, b = rng.random((n, 1)), rng.random((n, 1))
    pts = np.asarray(p0) + a * np.asarray(u) + b * np.asarray(v)
    return pts + rng.normal(0, jitter, pts.shape)


def _house(rng, *, furniture=True, ceiling=True, partition=True):
    parts = [
        _plane(rng, [0, 0, 0], [W, 0, 0], [0, D, 0], 12000),
        _plane(rng, [0, 0, 0], [W, 0, 0], [0, 0, H], 7000),
        _plane(rng, [0, D, 0], [W, 0, 0], [0, 0, H], 7000),
        _plane(rng, [0, 0, 0], [0, D, 0], [0, 0, H], 6000),
        _plane(rng, [W, 0, 0], [0, D, 0], [0, 0, H], 6000),
    ]
    if ceiling:
        parts.append(_plane(rng, [0, 0, H], [W, 0, 0], [0, D, 0], 12000))
    if partition:
        parts.append(_plane(rng, [PARTITION_X, 0, 0], [0, D, 0], [0, 0, H], 6000))
    if furniture:
        parts.append(_plane(rng, [1.0, 1.0, 0], [1.2, 0, 0], [0, 0, 1.9], 3500))
        parts.append(_plane(rng, [1.0, 1.0, 0], [0, 0.6, 0], [0, 0, 1.9], 2500))
        parts.append(_plane(rng, [5.5, 3.0, 0], [2.0, 0, 0], [0, 0, 0.95], 3500))
        parts.append(_plane(rng, [5.5, 3.0, 0.95], [2.0, 0, 0], [0, 1.0, 0], 3500))
    return np.vstack(parts)


def _rotate(rng, pts):
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    if np.linalg.det(q) < 0:
        q[:, 0] = -q[:, 0]
    return pts @ q.T, q


def _ply(pts):
    header = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(pts)}\n"
              "property float x\nproperty float y\nproperty float z\nend_header\n").encode()
    return header + pts.astype("<f4").tobytes()


@pytest.fixture(autouse=True)
def _clean_session():
    segmentation.reset_cache()
    yield
    segmentation.reset_cache()


def _aligned_house(seed=7, **kwargs):
    """A house plus the orientation the feature extractor needs."""
    rng = np.random.default_rng(seed)
    pts, _ = _rotate(rng, _house(rng, **kwargs))
    xyz, _ = slicing.parse_ply(_ply(pts))
    up = slicing.estimate_up_axis(xyz)
    axis = np.asarray(up.vector)
    right, forward = slicing._ground_basis(np, axis)
    planar = np.stack([xyz @ right, xyz @ forward], axis=1)
    profile = slicing.vertical_profile(xyz @ axis, planar)
    return xyz, up, profile


# ---------------------------------------------------------------------------
# Features — the contract between training and serving
# ---------------------------------------------------------------------------

def test_features_are_scale_free():
    """The property that makes training on synthetic rooms legitimate.

    A reconstruction has NO metric scale, so a feature measured in metres would
    be meaningless — and a model trained on one would learn the size of its
    training houses rather than the shape of a wall. Every feature is a ratio,
    so scaling the whole cloud must change nothing.
    """
    xyz, up, profile = _aligned_house()

    base = extract(xyz, up.vector, floor=profile.floor, ceiling=profile.ceiling)
    scaled = extract(
        xyz * 3.7, up.vector,
        floor=profile.floor * 3.7,
        ceiling=None if profile.ceiling is None else profile.ceiling * 3.7,
    )

    assert base.shape == scaled.shape
    assert np.allclose(base, scaled, atol=1e-5), "a feature is carrying absolute size"


def test_features_are_deterministic():
    """Train/serve skew is silent — the model still returns confident
    probabilities on wrong inputs and nothing raises."""
    xyz, up, profile = _aligned_house()
    kwargs = dict(floor=profile.floor, ceiling=profile.ceiling)

    assert np.array_equal(extract(xyz, up.vector, **kwargs),
                          extract(xyz, up.vector, **kwargs))


def test_the_feature_width_matches_the_declared_names():
    """ONNX inputs are positional, so a name list that drifts from the matrix
    silently rewires every feature to the wrong weight."""
    xyz, up, profile = _aligned_house()
    features = extract(xyz, up.vector, floor=profile.floor, ceiling=profile.ceiling)

    assert features.shape[1] == len(FEATURE_NAMES)
    assert features.dtype == np.float32


def test_column_span_separates_a_wall_from_furniture():
    """The load-bearing feature, tested directly rather than through the model.

    A wall occupies its vertical column floor-to-ceiling; a wardrobe occupies
    three quarters of it. The height histogram cannot see this because it
    collapses x and y — the column can.
    """
    span = FEATURE_NAMES.index("column_span")
    height = 2.5
    rng = np.random.default_rng(3)

    wall = np.stack([np.zeros(4000), rng.uniform(0, 6, 4000), rng.uniform(0, height, 4000)], axis=1)
    stub = np.stack([np.full(4000, 3.0), rng.uniform(2, 3, 4000), rng.uniform(0, 1.0, 4000)], axis=1)
    floor = np.stack([rng.uniform(0, 8, 6000), rng.uniform(0, 6, 6000), np.zeros(6000)], axis=1)
    xyz = np.vstack([wall, stub, floor])

    features = extract(xyz, (0.0, 0.0, 1.0), floor=0.0, ceiling=height)
    wall_span = features[:len(wall), span].mean()
    stub_span = features[len(wall):len(wall) + len(stub), span].mean()

    assert wall_span > stub_span * 1.5, f"wall {wall_span:.2f} vs furniture {stub_span:.2f}"


# ---------------------------------------------------------------------------
# Optionality — the model must never be load-bearing
# ---------------------------------------------------------------------------

def test_a_missing_model_returns_none_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.setenv(segmentation.MODEL_ENV, str(tmp_path / "absent.onnx"))
    segmentation.reset_cache()
    xyz, up, profile = _aligned_house()

    assert segmentation.segment(xyz, up.vector, floor=profile.floor) is None


def test_availability_distinguishes_its_two_failures(monkeypatch, tmp_path):
    """"No segmenter installed" and "onnxruntime is missing" call for different
    actions; one message for both teaches nobody anything."""
    monkeypatch.setenv(segmentation.MODEL_ENV, str(tmp_path / "absent.onnx"))
    segmentation.reset_cache()
    ready, reason = segmentation.available()

    assert ready is False
    assert "segmenter" in reason and str(tmp_path) in reason


def test_a_corrupt_model_degrades_instead_of_failing_the_plan(monkeypatch, tmp_path):
    """An accelerator that breaks must not take the plan with it."""
    broken = tmp_path / "broken.onnx"
    broken.write_bytes(b"not an onnx graph at all")
    monkeypatch.setenv(segmentation.MODEL_ENV, str(broken))
    segmentation.reset_cache()
    xyz, up, profile = _aligned_house()

    assert segmentation.segment(xyz, up.vector, floor=profile.floor) is None


def test_the_plan_is_still_produced_with_the_segmenter_disabled():
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    doc = slicing.extract_from_reconstruction(
        _ply(pts), metres_per_unit=1.0, use_segmenter=False)

    assert len(doc.rooms) >= 1


# ---------------------------------------------------------------------------
# It classifies; it does not measure
# ---------------------------------------------------------------------------

def test_the_segmenter_cannot_supply_a_missing_scale():
    """The line that must not move. However well it performs, it produces no
    dimension — so a plan with no anchor is still refused."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    with pytest.raises(MissingScale):
        slicing.extract_from_reconstruction(_ply(pts), use_segmenter=True)


# ---------------------------------------------------------------------------
# What it buys, when it is installed
# ---------------------------------------------------------------------------

_INSTALLED = segmentation.available()[0]
_needs_model = pytest.mark.skipif(not _INSTALLED, reason="no point segmenter installed")


@_needs_model
def test_labels_cover_every_point_and_use_the_declared_classes():
    xyz, up, profile = _aligned_house()

    labels = segmentation.segment(
        xyz, up.vector, floor=profile.floor, ceiling=profile.ceiling)

    assert labels is not None and len(labels) == len(xyz)
    # -1 is "not confident enough to assert", which is a deliberate outcome.
    assert set(np.unique(labels)) <= set(range(len(LABELS))) | {-1}


@_needs_model
def test_it_recovers_a_capture_that_never_saw_the_ceiling():
    """The case the geometric path cannot do, and the reason this model earns
    its place: a hand-held photo sweep at eye level often never points up, and
    the height band has nothing to aim below."""
    rng = np.random.default_rng(9)
    pts, _ = _rotate(rng, _house(rng, ceiling=False))
    ply = _ply(pts)

    # Not asserted as "geometry must fail" — whether the band copes depends on
    # how much of the wall tops a capture happened to catch, and pinning that
    # would make this a tripwire on the generator rather than a statement about
    # the model. What IS asserted: the segmenter needs no ceiling at all.
    doc = slicing.extract_from_reconstruction(ply, metres_per_unit=1.0, use_segmenter=True)
    assert "segmented" in (doc.provenance.notes or ""), "the model path did not run"

    xs = [p[0] for w in doc.walls for p in (w.start, w.end)]
    ys = [p[1] for w in doc.walls for p in (w.start, w.end)]
    got = sorted([max(xs) - min(xs), max(ys) - min(ys)])
    for actual, expected in zip(got, sorted([W, D])):
        assert abs(actual - expected) / expected < 0.10


@_needs_model
def test_the_segmented_path_is_recorded_in_the_provenance():
    """Which path produced a plan changes how much to trust it, so it is not
    left to be inferred."""
    rng = np.random.default_rng(7)
    pts, _ = _rotate(rng, _house(rng))

    doc = slicing.extract_from_reconstruction(
        _ply(pts), metres_per_unit=1.0, use_segmenter=True)

    assert "segmented" in (doc.provenance.notes or "")


@_needs_model
def test_furniture_is_classified_as_clutter_not_wall():
    """Stated as a majority rather than an absolute: this is a learned model on
    synthetic data, and pinning it to perfection would make the test a
    tripwire on retraining rather than a statement about behaviour."""
    height = 2.5
    rng = np.random.default_rng(5)
    island = np.stack([
        rng.uniform(3.0, 5.0, 3000), rng.uniform(2.0, 3.0, 3000), rng.uniform(0, 0.95, 3000),
    ], axis=1)
    walls = np.vstack([
        np.stack([np.zeros(6000), rng.uniform(0, 6, 6000), rng.uniform(0, height, 6000)], axis=1),
        np.stack([np.full(6000, 8.0), rng.uniform(0, 6, 6000), rng.uniform(0, height, 6000)], axis=1),
    ])
    floor = np.stack([
        rng.uniform(0, 8, 9000), rng.uniform(0, 6, 9000), np.zeros(9000)], axis=1)
    ceiling = np.stack([
        rng.uniform(0, 8, 9000), rng.uniform(0, 6, 9000), np.full(9000, height)], axis=1)

    xyz = np.vstack([island, walls, floor, ceiling])
    labels = segmentation.segment(xyz, (0.0, 0.0, 1.0), floor=0.0, ceiling=height)

    from floorplan_pipeline.pointfeatures import LABEL_WALL

    island_labels = labels[:len(island)]
    called_wall = float((island_labels == LABEL_WALL).mean())
    assert called_wall < 0.35, f"{called_wall:.0%} of a kitchen island was called wall"


def test_a_model_of_the_wrong_version_is_named_rather_than_swallowed(monkeypatch, tmp_path, caplog):
    """The guard used to compare `extract`'s output width against the constant
    `extract` itself stacks — a branch that can never be taken — while the real
    train/serve skew went unchecked.

    The feature count went 7 -> 10 on this branch, so a deployment pointing
    ORACLE_FLOORPLAN_SEGMENTER at a mounted v1 model (the documented reason the
    override exists) fed a 10-wide tensor to a 7-input graph. onnxruntime raised
    inside `session.run`, the blanket except swallowed it, and the operator saw
    a generic failure naming neither the version nor the width.
    """
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnxruntime")

    stale = tmp_path / "v1.onnx"
    torch.onnx.export(
        torch.nn.Linear(len(FEATURE_NAMES) - 3, len(LABELS)),
        torch.zeros(1, len(FEATURE_NAMES) - 3),
        str(stale),
        input_names=["features"], output_names=["logits"],
        dynamic_axes={"features": {0: "points"}, "logits": {0: "points"}},
    )
    monkeypatch.setenv(segmentation.MODEL_ENV, str(stale))
    segmentation.reset_cache()
    xyz, up, profile = _aligned_house()

    with caplog.at_level("WARNING"):
        assert segmentation.segment(xyz, up.vector, floor=profile.floor) is None

    assert "different version" in caplog.text
    assert str(len(FEATURE_NAMES) - 3) in caplog.text


def test_an_empty_column_has_no_span(monkeypatch):
    """`(highest - lowest + 1) / COLUMN_LEVELS` credited an EMPTY column with a
    level holding nothing. A point's own column is never empty, so `column_span`
    was unaffected — but `_context` reads the whole grid, so every empty
    neighbour biased `neighbour_span` upward and counted as agreeing with any
    cell below 0.19. Sparse captures have the most empty cells, and they are the
    partial sweeps this model exists to serve.
    """
    from floorplan_pipeline import pointfeatures

    rng = np.random.default_rng(3)
    # One dense pillar in a corner: almost every grid cell is empty.
    pillar = _plane(rng, [0.0, 0.0, 0.0], [0.2, 0, 0], [0, 0, H], 4000)
    floor = _plane(rng, [0, 0, 0], [W, 0, 0], [0, D, 0], 400)
    xyz = np.vstack([pillar, floor])
    up = np.array([0.0, 0.0, 1.0])

    features = pointfeatures.extract(xyz, up, floor=0.0, ceiling=H)
    span = features[:, FEATURE_NAMES.index("neighbour_span")]

    # `neighbour_span` is the mean over a 3x3 patch, and a point's OWN cell is
    # occupied by definition, so it cannot reach zero — the floor of one
    # isolated occupied cell is (1 / COLUMN_LEVELS) / 9. What must not survive
    # is the phantom: with empty columns credited a level, all eight empty
    # neighbours contributed 1 / COLUMN_LEVELS each and the patch could never
    # read below that value.
    empty_column_phantom = 1.0 / pointfeatures.COLUMN_LEVELS
    assert float(span.min()) < empty_column_phantom * 0.5, (
        "empty neighbours are still contributing a phantom span"
    )
    assert float(span.min()) == pytest.approx(empty_column_phantom / 9.0, rel=0.05)


def test_the_model_card_records_how_it_was_judged():
    """The card is the only thing telling a reader how far to trust these
    weights, and the two facts that matter most are the ones it used to omit:
    how train and validation were split — a per-point split leaks
    near-duplicates and inflates every figure — and what the model scored on
    PLANS rather than on points. Per-point accuracy and plan quality come apart
    badly enough that one model beat another 0.984 to 0.964 on wall precision
    while producing 61% correct room counts against 95%.
    """
    import json
    from pathlib import Path

    card = Path(segmentation.DEFAULT_MODEL_PATH).with_suffix(".json")
    if not card.is_file():
        pytest.skip("no model installed")
    payload = json.loads(card.read_text())

    assert payload["features"] == list(FEATURE_NAMES), "card drifted from the feature list"
    assert "HOUSES" in payload.get("validation_protocol", "")
    assert "NOT MEASURED" not in payload.get("plan_quality", "NOT MEASURED")
