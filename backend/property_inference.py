"""Confidence-tagged property, rehab, terrain, and tour-variant inference."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any, Mapping, Sequence


CHARACTERISTIC_MODEL_VERSION = "property-imputation-2026.07"
PHOTO_REHAB_MODEL_VERSION = "photo-rehab-band-2026.07"
GEOSPATIAL_MODEL_VERSION = "source-backed-terrain-2026.07"
TOUR_VARIANT_MODEL_VERSION = "post-rehab-tour-manifest-2026.07"
TOUR_DISCLOSURE = (
    "AI-generated post-rehabilitation concept. It is not a photograph, scope of work, "
    "cost guarantee, permit approval, or representation of future condition."
)


class InferenceInputError(ValueError):
    pass


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InferenceInputError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise InferenceInputError(f"{name} is outside the supported range")
    return number


def impute_characteristics(
    missing_fields: Sequence[str],
    comparable_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Impute only requested missing fields from explicit comparable records."""
    if not missing_fields:
        raise InferenceInputError("at least one missing field is required")
    if len(comparable_records) < 3:
        raise InferenceInputError("at least three comparable records are required")
    inferences: list[dict[str, Any]] = []
    for field in sorted(set(str(value) for value in missing_fields)):
        values = [record.get(field) for record in comparable_records if record.get(field) is not None]
        if len(values) < 3:
            inferences.append(
                {
                    "characteristic": field,
                    "inferred_value": None,
                    "confidence": 0.0,
                    "sample_size": len(values),
                    "status": "insufficient_evidence",
                }
            )
            continue
        numeric: list[float] = []
        for value in values:
            try:
                number = float(value)
                if math.isfinite(number):
                    numeric.append(number)
            except (TypeError, ValueError):
                pass
        if len(numeric) == len(values):
            inferred: Any = statistics.median(numeric)
            spread = statistics.pstdev(numeric) if len(numeric) > 1 else 0.0
            denominator = abs(float(inferred)) or 1.0
            consistency = max(0.0, 1.0 - min(1.0, spread / denominator))
        else:
            counts = Counter(str(value) for value in values)
            inferred, frequency = counts.most_common(1)[0]
            consistency = frequency / len(values)
        sample_factor = min(1.0, len(values) / 10.0)
        confidence = round(0.35 + 0.4 * consistency + 0.2 * sample_factor, 4)
        inferences.append(
            {
                "characteristic": field,
                "inferred_value": inferred,
                "confidence": min(confidence, 0.95),
                "sample_size": len(values),
                "status": "inferred",
            }
        )
    return {
        "model_version": CHARACTERISTIC_MODEL_VERSION,
        "method": "statistical",
        "inferences": inferences,
        "warnings": [
            "Imputed characteristics are not observed facts and must be verified before underwriting or marketing."
        ],
    }


def estimate_photo_rehab(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate confidence-aware component estimates into a cost band."""
    if not findings:
        raise InferenceInputError("at least one photo finding is required")
    items: list[dict[str, Any]] = []
    total_low = 0.0
    total_high = 0.0
    weighted_confidence = 0.0
    for index, finding in enumerate(findings):
        component = str(finding.get("component") or "").strip()
        if not component:
            raise InferenceInputError(f"findings[{index}].component is required")
        quantity = _number(finding.get("quantity", 1), f"findings[{index}].quantity", minimum=0)
        low = _number(finding.get("unit_cost_low"), f"findings[{index}].unit_cost_low", minimum=0)
        high = _number(finding.get("unit_cost_high"), f"findings[{index}].unit_cost_high", minimum=low)
        confidence = _number(finding.get("confidence"), f"findings[{index}].confidence", minimum=0)
        if confidence > 1:
            raise InferenceInputError(f"findings[{index}].confidence must be 0–1")
        # Wider uncertainty for low-confidence visual classifications.
        uncertainty = 1.0 - confidence
        item_low = quantity * low * max(0.5, 1.0 - 0.25 * uncertainty)
        item_high = quantity * high * (1.0 + 0.5 * uncertainty)
        total_low += item_low
        total_high += item_high
        weighted_confidence += confidence
        items.append(
            {
                "component": component,
                "condition": finding.get("condition"),
                "photo_id": finding.get("photo_id"),
                "quantity": quantity,
                "cost_band": [round(item_low, 2), round(item_high, 2)],
                "finding_confidence": confidence,
            }
        )
    return {
        "model_version": PHOTO_REHAB_MODEL_VERSION,
        "method": "photo_estimate",
        "rehab_cost_band": [round(total_low, 2), round(total_high, 2)],
        "confidence": round(weighted_confidence / len(items), 4),
        "items": items,
        "warnings": [
            "Photo estimates can miss concealed conditions, code requirements, labor variation, and permit costs.",
            "A qualified on-site inspection and contractor scope are required.",
        ],
    }


def analyze_topography(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe measured terrain without fabricating a viewshed simulation."""
    if len(samples) < 3:
        raise InferenceInputError("at least three geospatial samples are required")
    normalized: list[dict[str, float]] = []
    for index, sample in enumerate(samples):
        normalized.append(
            {
                "elevation_ft": _number(sample.get("elevation_ft"), f"samples[{index}].elevation_ft"),
                "distance_ft": _number(sample.get("distance_ft"), f"samples[{index}].distance_ft", minimum=0),
                "bearing_deg": _number(sample.get("bearing_deg"), f"samples[{index}].bearing_deg", minimum=0) % 360,
            }
        )
    elevations = [sample["elevation_ft"] for sample in normalized]
    grades: list[float] = []
    ordered = sorted(normalized, key=lambda value: value["distance_ft"])
    for left, right in zip(ordered, ordered[1:]):
        run = right["distance_ft"] - left["distance_ft"]
        if run > 0:
            grades.append(abs(right["elevation_ft"] - left["elevation_ft"]) / run)
    relief = max(elevations) - min(elevations)
    return {
        "model_version": GEOSPATIAL_MODEL_VERSION,
        "method": "geospatial",
        "sample_count": len(normalized),
        "elevation_range_ft": [round(min(elevations), 2), round(max(elevations), 2)],
        "local_relief_ft": round(relief, 2),
        "median_sample_grade_pct": round((statistics.median(grades) if grades else 0) * 100, 2),
        "viewshed_status": "source_samples_only_no_line_of_sight_guarantee",
        "warnings": [
            "Terrain samples do not account for buildings, trees, seasonal foliage, or future development.",
            "A survey-grade elevation model and planning review are required for a viewshed conclusion.",
        ],
    }


def tour_variant_manifest(
    *,
    variant_name: str,
    style: str,
    rehab_scope: Sequence[Mapping[str, Any]],
    source_media_ids: Sequence[str],
) -> dict[str, Any]:
    allowed_styles = {"neutral", "modern", "traditional", "industrial", "luxury", "accessible"}
    if style not in allowed_styles:
        raise InferenceInputError("unsupported tour variant style")
    if not rehab_scope or not source_media_ids:
        raise InferenceInputError("rehab scope and source media are required")
    return {
        "model_version": TOUR_VARIANT_MODEL_VERSION,
        "variant_name": variant_name,
        "style": style,
        "source_media_ids": list(source_media_ids),
        "proposed_changes": [dict(item) for item in rehab_scope],
        "render_kind": "conceptual_post_rehab_tour",
        "disclosure": TOUR_DISCLOSURE,
        "observed_geometry_preserved": True,
        "status": "manifest_ready_for_renderer",
    }
