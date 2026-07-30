"""Deterministic, source-backed real-estate intelligence calculations.

The functions in this module deliberately return reproducible inputs, formulas,
assumptions, and risks.  They never expose hidden reasoning and never convert an
unvalidated score into a probability.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence


DISTRESS_MODEL_VERSION = "public-pre-distress-2026.07"
HBU_MODEL_VERSION = "zoning-hbu-rules-2026.07"
FORECAST_MODEL_VERSION = "micro-market-linear-2026.07"
UNDERWRITING_MODEL_VERSION = "reproducible-underwriting-2026.07"
TITLE_MODEL_VERSION = "preliminary-title-linker-2026.07"


class IntelligenceInputError(ValueError):
    """Raised when a calculation would otherwise fabricate a result."""


def _decimal(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IntelligenceInputError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise IntelligenceInputError(f"{field} must be finite")
    if minimum is not None and parsed < minimum:
        raise IntelligenceInputError(f"{field} must be at least {minimum}")
    return parsed


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _median(values: Sequence[Decimal], field: str) -> Decimal:
    if not values:
        raise IntelligenceInputError(f"at least one {field} value is required")
    return Decimal(str(statistics.median(values)))


def calculate_underwriting(
    *,
    subject_sqft: Any,
    comparables: Sequence[Mapping[str, Any]],
    rehab_items: Sequence[Mapping[str, Any]],
    acquisition_ratio: Any = "0.70",
    explicit_arv: Any | None = None,
) -> dict[str, Any]:
    """Calculate ARV, rehab, and MAO with a structured underwriting trace."""
    sqft = _decimal(subject_sqft, "subject_sqft", minimum=Decimal("1"))
    ratio = _decimal(acquisition_ratio, "acquisition_ratio", minimum=Decimal("0"))
    if ratio > Decimal("1"):
        raise IntelligenceInputError("acquisition_ratio cannot exceed 1")

    comparable_evidence: list[dict[str, Any]] = []
    price_per_sqft: list[Decimal] = []
    for index, comparable in enumerate(comparables):
        sale_price = _decimal(
            comparable.get("sale_price"), f"comparables[{index}].sale_price", minimum=Decimal("1")
        )
        comp_sqft = _decimal(
            comparable.get("sqft"), f"comparables[{index}].sqft", minimum=Decimal("1")
        )
        ppsf = sale_price / comp_sqft
        price_per_sqft.append(ppsf)
        comparable_evidence.append(
            {
                "record_id": comparable.get("record_id"),
                "address": comparable.get("address"),
                "sale_price": _money(sale_price),
                "sqft": float(comp_sqft),
                "price_per_sqft": _money(ppsf),
                "sale_date": comparable.get("sale_date"),
                "source": comparable.get("source"),
            }
        )

    if explicit_arv is None:
        median_ppsf = _median(price_per_sqft, "comparable")
        arv = median_ppsf * sqft
        arv_formula = "ARV = subject_sqft × median(comparable_sale_price / comparable_sqft)"
        arv_assumption = "Comparable median price per square foot represents repaired condition."
    else:
        arv = _decimal(explicit_arv, "explicit_arv", minimum=Decimal("1"))
        median_ppsf = arv / sqft
        arv_formula = "ARV = agent-supplied reviewed ARV"
        arv_assumption = "ARV was supplied by the agent and must retain its cited valuation evidence."

    rehab_total = Decimal("0")
    normalized_rehab: list[dict[str, Any]] = []
    for index, item in enumerate(rehab_items):
        quantity = _decimal(item.get("quantity", 1), f"rehab_items[{index}].quantity", minimum=Decimal("0"))
        unit_cost = _decimal(item.get("unit_cost"), f"rehab_items[{index}].unit_cost", minimum=Decimal("0"))
        subtotal = quantity * unit_cost
        rehab_total += subtotal
        normalized_rehab.append(
            {
                "category": str(item.get("category") or "unspecified")[:120],
                "quantity": float(quantity),
                "unit_cost": _money(unit_cost),
                "subtotal": _money(subtotal),
                "basis": str(item.get("basis") or "agent estimate")[:240],
            }
        )

    mao = max(Decimal("0"), (ratio * arv) - rehab_total)
    risks: list[str] = []
    if len(comparables) < 3 and explicit_arv is None:
        risks.append("Fewer than three comparable sales; ARV evidence is thin.")
    if rehab_total == 0:
        risks.append("No rehab cost was entered; MAO may be overstated.")

    return {
        "model_version": UNDERWRITING_MODEL_VERSION,
        "arv": _money(arv),
        "rehab": _money(rehab_total),
        "mao": _money(mao),
        "acquisition_ratio": float(ratio),
        "trace": {
            "inputs": {
                "subject_sqft": float(sqft),
                "acquisition_ratio": float(ratio),
                "rehab_items": normalized_rehab,
            },
            "formulas": [
                arv_formula,
                "rehab = Σ(quantity × unit_cost)",
                "MAO = max(0, acquisition_ratio × ARV − rehab)",
            ],
            "comparable_evidence": comparable_evidence,
            "assumptions": [arv_assumption],
            "risks": risks,
            "outputs": {
                "median_comparable_price_per_sqft": _money(median_ppsf),
                "arv": _money(arv),
                "rehab": _money(rehab_total),
                "mao": _money(mao),
            },
        },
    }


_DISTRESS_WEIGHTS: dict[str, float] = {
    "tax_delinquency": 0.20,
    "vacancy": 0.14,
    "open_violations": 0.14,
    "eviction_aggregate": 0.06,
    "permit_activity": 0.07,
    "stalled_permits": 0.08,
    "foreclosure_filing": 0.20,
    "property_history": 0.06,
    "deferred_maintenance": 0.05,
}


def score_pre_distress(
    signals: Mapping[str, Any],
    *,
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a public-signal score and, only if validated, a probability."""
    unknown = sorted(set(signals) - set(_DISTRESS_WEIGHTS))
    if unknown:
        raise IntelligenceInputError(f"unsupported distress signals: {', '.join(unknown)}")

    contributions: list[dict[str, Any]] = []
    weighted = 0.0
    observed_weight = 0.0
    for name, weight in _DISTRESS_WEIGHTS.items():
        if name not in signals:
            continue
        raw = float(_decimal(signals[name], name, minimum=Decimal("0")))
        normalized = min(raw, 1.0)
        contribution = weight * normalized
        weighted += contribution
        observed_weight += weight
        contributions.append(
            {
                "signal": name,
                "normalized_value": round(normalized, 4),
                "weight": weight,
                "contribution": round(contribution, 4),
            }
        )

    evidence_score = weighted / observed_weight if observed_weight else 0.0
    coverage = observed_weight / sum(_DISTRESS_WEIGHTS.values())
    band_half_width = max(0.08, 0.32 * (1.0 - coverage))
    evidence_band = [
        round(max(0.0, evidence_score - band_half_width), 4),
        round(min(1.0, evidence_score + band_half_width), 4),
    ]

    calibration = dict(calibration or {})
    validated = bool(
        calibration.get("validated") is True
        and int(calibration.get("holdout_size", 0)) >= 1_000
        and float(calibration.get("brier_score", 1.0)) <= 0.25
        and calibration.get("geographic_bias_reviewed") is True
        and calibration.get("temporal_leakage_reviewed") is True
    )
    probability = round(evidence_score, 4) if validated else None
    warnings = []
    if not validated:
        warnings.append(
            "Model validation gate not satisfied; evidence score and confidence band are shown instead of a probability."
        )
    if coverage < 0.5:
        warnings.append("Less than half of the supported public-signal weight is observed.")

    return {
        "model_version": DISTRESS_MODEL_VERSION,
        "evidence_score": round(evidence_score, 4),
        "evidence_band": evidence_band,
        "calibrated_probability": probability,
        "is_probability_validated": validated,
        "signal_coverage": round(coverage, 4),
        "contributions": contributions,
        "warnings": warnings,
    }


def analyze_highest_best_use(
    *,
    lot_area_sqft: Any,
    building_area_sqft: Any,
    max_far: Any,
    max_lot_coverage: Any | None,
    allowed_uses: Sequence[str],
    dimensional_limits: Mapping[str, Any],
    land_comparables: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute zoning capacity without presenting it as a legal conclusion."""
    lot = _decimal(lot_area_sqft, "lot_area_sqft", minimum=Decimal("1"))
    building = _decimal(building_area_sqft, "building_area_sqft", minimum=Decimal("0"))
    far = _decimal(max_far, "max_far", minimum=Decimal("0"))
    coverage = None
    if max_lot_coverage is not None:
        coverage = _decimal(max_lot_coverage, "max_lot_coverage", minimum=Decimal("0"))
        if coverage > Decimal("1"):
            raise IntelligenceInputError("max_lot_coverage must be a 0–1 ratio")

    gross_buildable = lot * far
    remaining = max(Decimal("0"), gross_buildable - building)
    current_far = building / lot
    footprint = lot * coverage if coverage is not None else None

    land_values: list[Decimal] = []
    normalized_comps: list[dict[str, Any]] = []
    for index, comp in enumerate(land_comparables):
        sale_price = _decimal(comp.get("sale_price"), f"land_comparables[{index}].sale_price", minimum=Decimal("1"))
        comp_lot = _decimal(comp.get("lot_area_sqft"), f"land_comparables[{index}].lot_area_sqft", minimum=Decimal("1"))
        price_per_land_sqft = sale_price / comp_lot
        land_values.append(price_per_land_sqft)
        normalized_comps.append(
            {
                "record_id": comp.get("record_id"),
                "sale_price": _money(sale_price),
                "lot_area_sqft": float(comp_lot),
                "price_per_land_sqft": _money(price_per_land_sqft),
                "sale_date": comp.get("sale_date"),
                "source": comp.get("source"),
            }
        )

    indicated_land_value = None
    median_land_ppsf = None
    if land_values:
        median_land_ppsf = _median(land_values, "land comparable")
        indicated_land_value = median_land_ppsf * lot

    warnings = [
        "Planning/zoning staff or qualified counsel must verify allowed uses, overlays, dimensional rules, and effective dates.",
        "Buildable area is a zoning envelope, not a guarantee of design, utility, environmental, or permit feasibility.",
    ]
    if not allowed_uses:
        warnings.append("No allowed-use evidence was supplied.")
    if not land_comparables:
        warnings.append("No comparable land sales were supplied; no land value indication is shown.")

    return {
        "model_version": HBU_MODEL_VERSION,
        "current_far": round(float(current_far), 4),
        "max_far": float(far),
        "gross_buildable_area_sqft": round(float(gross_buildable), 2),
        "remaining_buildable_area_sqft": round(float(remaining), 2),
        "max_footprint_sqft": round(float(footprint), 2) if footprint is not None else None,
        "air_rights_indicator": remaining > Decimal("0"),
        "allowed_uses": sorted({str(value).strip() for value in allowed_uses if str(value).strip()}),
        "dimensional_limits": dict(dimensional_limits),
        "median_land_price_per_sqft": _money(median_land_ppsf) if median_land_ppsf is not None else None,
        "indicated_land_value": _money(indicated_land_value) if indicated_land_value is not None else None,
        "land_comparables": normalized_comps,
        "formulas": [
            "current FAR = building area / lot area",
            "gross buildable area = lot area × maximum FAR",
            "remaining buildable area = max(0, gross buildable area − existing building area)",
            "maximum footprint = lot area × maximum lot coverage",
        ],
        "warnings": warnings,
    }


def preliminary_title_summary(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize recorder/tax/court matches without claiming insured title."""
    normalized: list[dict[str, Any]] = []
    unresolved = 0
    chain_gaps = 0
    for finding in findings:
        status = str(finding.get("match_status") or "unresolved").lower()
        if status not in {"matched", "possible_match", "unresolved", "released"}:
            raise IntelligenceInputError(f"unsupported title match status: {status}")
        is_gap = bool(finding.get("chain_gap"))
        unresolved += int(status in {"possible_match", "unresolved"})
        chain_gaps += int(is_gap)
        normalized.append(
            {
                "kind": str(finding.get("kind") or "other")[:80],
                "record_id": finding.get("record_id"),
                "amount": finding.get("amount"),
                "recorded_at": finding.get("recorded_at"),
                "released_at": finding.get("released_at"),
                "match_status": status,
                "chain_gap": is_gap,
                "source": finding.get("source"),
                "notes": finding.get("notes"),
            }
        )

    return {
        "model_version": TITLE_MODEL_VERSION,
        "finding_count": len(normalized),
        "unresolved_matches": unresolved,
        "chain_gaps": chain_gaps,
        "findings": normalized,
        "review_status": "professional_review_required" if unresolved or chain_gaps else "preliminary_clear_no_guarantee",
        "warnings": [
            "This is preliminary public-record intelligence, not an abstract, legal opinion, or insured title search.",
            "A licensed title professional or attorney must verify identity matches, releases, priority, and chain of title.",
        ],
    }


def _linear_fit(points: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    if len(points) < 3:
        raise IntelligenceInputError("at least three annual observations are required")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        raise IntelligenceInputError("observation years must not all be the same")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    intercept = mean_y - (slope * mean_x)
    residuals = [y - (intercept + slope * x) for x, y in points]
    standard_error = math.sqrt(sum(value * value for value in residuals) / max(1, len(points) - 2))
    return slope, intercept, standard_error


def forecast_micro_market(
    observations: Sequence[Mapping[str, Any]],
    *,
    horizon_years: int = 5,
) -> dict[str, Any]:
    """Create a transparent five-year forecast with confidence intervals."""
    if horizon_years < 1 or horizon_years > 5:
        raise IntelligenceInputError("horizon_years must be between 1 and 5")

    required_indicators = {
        "permits",
        "crime_aggregate",
        "flood_exposure",
        "census_trends",
        "sales",
        "inventory",
        "commercial_activity",
    }
    aliases = {
        "permit": "permits",
        "crime": "crime_aggregate",
        "flood": "flood_exposure",
        "census": "census_trends",
        "commercial": "commercial_activity",
    }
    by_indicator: dict[str, dict[int, list[float]]] = {}
    for index, observation in enumerate(observations):
        try:
            year = int(observation["year"])
            value = float(observation["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IntelligenceInputError(f"observations[{index}] needs numeric year and value") from exc
        if not math.isfinite(value):
            raise IntelligenceInputError(f"observations[{index}].value must be finite")
        raw_indicator = str(observation.get("indicator") or "market_index").strip().lower()
        indicator = aliases.get(raw_indicator, raw_indicator)
        if indicator != "market_index" and indicator not in required_indicators:
            raise IntelligenceInputError(
                f"observations[{index}].indicator is unsupported: {raw_indicator}"
            )
        by_indicator.setdefault(indicator, {}).setdefault(year, []).append(value)

    usable: dict[str, dict[int, float]] = {}
    insufficient: list[str] = []
    indicator_trends: dict[str, dict[str, Any]] = {}
    for indicator, yearly_values in sorted(by_indicator.items()):
        annual = {
            year: statistics.fmean(values) for year, values in yearly_values.items()
        }
        if len(annual) < 3:
            insufficient.append(indicator)
            continue
        raw_points = sorted((float(year), value) for year, value in annual.items())
        raw_slope, _, raw_error = _linear_fit(raw_points)
        baseline = statistics.fmean(abs(value) for value in annual.values())
        if baseline == 0:
            insufficient.append(indicator)
            continue
        usable[indicator] = {
            year: (value / baseline) * 100.0 for year, value in annual.items()
        }
        indicator_trends[indicator] = {
            "annual_slope_raw_units": round(raw_slope, 6),
            "residual_standard_error": round(raw_error, 6),
            "historical_years": sorted(annual),
            "normalization_baseline": round(baseline, 6),
        }

    if not usable:
        raise IntelligenceInputError(
            "at least one indicator needs three annual observations"
        )

    composite_years = sorted({year for annual in usable.values() for year in annual})
    composite: list[tuple[float, float]] = []
    for year in composite_years:
        values = [annual[year] for annual in usable.values() if year in annual]
        if values:
            composite.append((float(year), statistics.fmean(values)))
    points = composite
    slope, intercept, residual_error = _linear_fit(points)
    last_year = int(points[-1][0])
    forecasts: list[dict[str, Any]] = []
    for step in range(1, horizon_years + 1):
        year = last_year + step
        estimate = intercept + slope * year
        observed_required = required_indicators.intersection(usable)
        coverage = len(observed_required) / len(required_indicators)
        coverage_penalty = (1.0 - coverage) * max(5.0, abs(estimate) * 0.15)
        uncertainty = (
            1.96 * residual_error * math.sqrt(1 + (step / len(points)))
            + coverage_penalty
        )
        forecasts.append(
            {
                "year": year,
                "estimate": round(estimate, 4),
                "confidence_interval_95": [
                    round(estimate - uncertainty, 4),
                    round(estimate + uncertainty, 4),
                ],
            }
        )

    return {
        "model_version": FORECAST_MODEL_VERSION,
        "method": "ordinary_least_squares_on_annual_aggregate",
        "target": "normalized_composite_market_index",
        "historical_years": [int(point[0]) for point in points],
        "annual_slope": round(slope, 6),
        "residual_standard_error": round(residual_error, 6),
        "forecast": forecasts,
        "indicator_trends": indicator_trends,
        "source_coverage": {
            "required_indicators": sorted(required_indicators),
            "observed_indicators": sorted(required_indicators.intersection(usable)),
            "missing_indicators": sorted(required_indicators - set(usable)),
            "insufficient_indicators": sorted(insufficient),
            "coverage": round(
                len(required_indicators.intersection(usable)) / len(required_indicators),
                4,
            ),
        },
        "fair_housing_review": {
            "status": "required_before_operational_use",
            "allowed_scope": "aggregate market planning only",
            "prohibited_scope": "individual targeting or service eligibility",
        },
        "warnings": [
            "Projection is a trend scenario, not a guarantee.",
            "Material policy, climate, financing, inventory, or data-definition changes can invalidate the fitted trend.",
            *(
                ["Required indicator coverage is incomplete; confidence intervals were widened."]
                if required_indicators - set(usable)
                else []
            ),
        ],
    }


def detect_public_sourcing_signals(signals: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Detect source-record patterns without inventing people or private contacts."""
    rules = (
        ("probate_or_heir_review", "probate_filing", "identity_verification_required"),
        ("tired_landlord", "landlord_pattern", "outreach_approval_required"),
        ("stalled_permit", "permit_stalled", "planning_record_review_required"),
        ("published_rezoning", "rezoning_agenda", "planning_review_required"),
    )
    detected: list[dict[str, Any]] = []
    for detector, field, gate in rules:
        value = signals.get(field)
        if value in (None, False, 0, ""):
            continue
        detected.append(
            {
                "detector": detector,
                "observed_signal": field,
                "value": value,
                "status": "candidate",
                "required_gate": gate,
                "private_contact_inferred": False,
            }
        )
    return detected


@dataclass(frozen=True)
class NegotiationThresholds:
    green_max: Decimal
    amber_max: Decimal


def negotiation_guidance(
    *,
    counter_offer: Any,
    arv: Any,
    rehab: Any,
    acquisition_ratio: Any = "0.70",
    amber_tolerance: Any = "0.05",
) -> dict[str, Any]:
    """Classify a counteroffer against a reproducible MAO; no emotion profiling."""
    counter = _decimal(counter_offer, "counter_offer", minimum=Decimal("0"))
    arv_d = _decimal(arv, "arv", minimum=Decimal("1"))
    rehab_d = _decimal(rehab, "rehab", minimum=Decimal("0"))
    ratio = _decimal(acquisition_ratio, "acquisition_ratio", minimum=Decimal("0"))
    tolerance = _decimal(amber_tolerance, "amber_tolerance", minimum=Decimal("0"))
    if ratio > 1 or tolerance > 1:
        raise IntelligenceInputError("ratios must not exceed 1")
    mao = max(Decimal("0"), (arv_d * ratio) - rehab_d)
    amber_max = mao * (Decimal("1") + tolerance)
    if counter <= mao:
        band = "green"
        factual_draft = "The counter is within the reviewed maximum allowable offer, subject to due diligence and approval."
    elif counter <= amber_max:
        band = "amber"
        factual_draft = "The counter is above the reviewed MAO but within the configured tolerance; verify assumptions before responding."
    else:
        band = "red"
        factual_draft = "The counter exceeds the reviewed MAO and tolerance; revise economics or decline rather than implying unsupported value."
    return {
        "counter_offer": _money(counter),
        "mao": _money(mao),
        "amber_max": _money(amber_max),
        "threshold": band,
        "objection_draft": factual_draft,
        "requires_agent_approval": True,
        "profiling_used": False,
        "formula": "MAO = acquisition_ratio × ARV − rehab",
    }
