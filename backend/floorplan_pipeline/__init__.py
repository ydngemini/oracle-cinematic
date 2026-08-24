"""Automated floor-plan extraction pipeline.

Turns one of three input classes into the FloorplanDocument schema that
floorplan_api.py persists and the Pascal editor loads:

    1. RASTER FLOOR PLANS  (scanned/marketing floor-plan images)
       -> classical CV works well. Walls are high-contrast straight strokes.
          This is the only path with genuinely good accuracy today.

    2. PARCEL / FOOTPRINT VECTORS  (county GIS building outlines)
       -> exact exterior footprint, zero interior. Honest ceiling: a shell.

    3. INTERIOR LISTING PHOTOS
       -> requires monocular layout estimation, not OpenCV. Currently returns
          a low-confidence estimate or declines. See STRATEGY below.

    4. PHOTOGRAMMETRIC RECONSTRUCTION  (slicing.py)
       -> the answer to (3) that does not need a layout model: run the photos
          through COLMAP + splatfacto and you have real 3D structure, so the
          plan comes from measured geometry rather than a guess. Slice it
          horizontally, hand the projection to the raster path above.
          Interior only — the exterior of record stays the parcel vector,
          which is exact where a photogrammetric footprint is ~90%.

DELIBERATE HONESTY CONSTRAINT
-----------------------------
The vault rule for [[Neoh_Walkable_Tours]] applies here verbatim: we do not
fabricate interior geometry that the source data does not support. A pipeline
that "imagines" a plausible 3-bed layout from 8 marketing photos produces a
document indistinguishable from a measured one, which would flow straight into
ARV/MAO through useRehabCalculator. Every output therefore carries
`provenance.ai_generated=True`, a model version, and a confidence score, and
the API refuses machine output with no model version.

MODEL STRATEGY (per input class)
--------------------------------
Raster floor plans — implemented here, no GPU, no model download:
  * Binarise (adaptive threshold) -> wall strokes separate from text/hatching.
  * Morphological opening with long horizontal and vertical kernels isolates
    wall runs and drops furniture glyphs, dimension text, and north arrows.
  * HoughLinesP over the wall mask -> candidate segments.
  * Collinear merge + endpoint snapping -> a clean wall graph.
  * Connected components on the inverted wall mask -> room polygons.
  * Openings: gaps in otherwise-continuous wall runs, width-classified into
    doors (~0.7-1.0 m) vs windows.
  Accuracy is good on clean vector-exported plans, mediocre on photographs of
  paper plans, and poor on heavily stylised marketing renders.

  Optional accuracy upgrade, not required: Segment Anything (SAM, Apache-2.0)
  for room segmentation instead of connected components. Better on plans with
  broken wall strokes; costs a GPU and ~2.4 GB of weights. Gated behind
  FLOORPLAN_USE_SAM so the default path stays CPU-only and dependency-light.

  Explicitly REJECTED: ControlNet and any diffusion model. They generate
  plausible geometry rather than measure it — exactly the fabrication this
  pipeline exists to avoid.

Parcel vectors — implemented here:
  * Simplify the footprint ring (Douglas-Peucker), emit one exterior wall loop
    and a single unnamed zone. No interior walls are invented.

Interior photos — NOT implemented, deliberately:
  * The credible open-source route is monocular room-layout estimation
    (e.g. LGT-Net / HorizonNet for panoramas) plus depth (Depth Anything V2,
    Apache-2.0), then per-room registration. That yields per-room boxes, not a
    connected multi-room plan, because ordinary listing photos do not
    constrain how rooms join. Until that ships, `extract_from_photos` raises
    UnsupportedInput rather than guessing.

SCALE
-----
Everything downstream is metric. A pixel plan has no intrinsic scale, so a
scale reference is REQUIRED: either an explicit `metres_per_pixel`, or a
detected dimension annotation, or a known total square footage from the MLS
record to solve for scale. Without one, extraction raises rather than assuming
a default — a wrong scale silently multiplies every rehab line item.
"""

from .schema import (
    FloorplanDocument,
    FloorplanLevel,
    FloorplanOpening,
    FloorplanRoom,
    FloorplanWall,
    Provenance,
)
from .errors import ExtractionError, MissingScale, UnsupportedInput
from .raster import extract_from_floorplan_image
from .parcel import extract_from_parcel_geometry

__all__ = [
    "FloorplanDocument",
    "FloorplanLevel",
    "FloorplanOpening",
    "FloorplanRoom",
    "FloorplanWall",
    "Provenance",
    "ExtractionError",
    "MissingScale",
    "UnsupportedInput",
    "extract_from_floorplan_image",
    "extract_from_parcel_geometry",
]

MODEL_VERSION = "floorplan-cv-1.0.0"
