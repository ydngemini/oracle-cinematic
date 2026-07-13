"""Objective buyer-request matching for internal disposition inventory."""

from __future__ import annotations

from typing import Any, Mapping


def _set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip().lower() for item in value if str(item).strip()}


def rank_buyer_request(
    property_facts: Mapping[str, Any],
    buyer_profile: Mapping[str, Any],
    request_criteria: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a 0–1 match and a field-by-field trace.

    Only explicit buy-box criteria and verified acquisition history contribute.
    Demographics, inferred personality, and private financial data are absent by
    construction.
    """
    facts = dict(property_facts)
    profile = dict(buyer_profile)
    criteria = {**profile, **dict(request_criteria)}
    trace: list[dict[str, Any]] = []
    earned = 0.0
    possible = 0.0

    def compare(label: str, weight: float, passed: bool, detail: str) -> None:
        nonlocal earned, possible
        possible += weight
        if passed:
            earned += weight
        trace.append({"criterion": label, "passed": passed, "weight": weight, "detail": detail})

    states = _set(criteria.get("states"))
    if states:
        actual = str(facts.get("state") or facts.get("state_code") or "").lower()
        compare("state", 0.18, actual in states, f"property={actual or 'unknown'}")

    counties = _set(criteria.get("counties"))
    if counties:
        actual = str(facts.get("county") or "").lower()
        compare("county", 0.10, actual in counties, f"property={actual or 'unknown'}")

    property_types = _set(criteria.get("property_types"))
    if property_types:
        actual = str(facts.get("property_type") or "").lower()
        compare("property_type", 0.14, actual in property_types, f"property={actual or 'unknown'}")

    price = facts.get("asking_price")
    min_price = criteria.get("min_price")
    max_price = criteria.get("max_price")
    if min_price is not None or max_price is not None:
        price_n = float(price) if price is not None else None
        passed = price_n is not None
        if passed and min_price is not None:
            passed = price_n >= float(min_price)
        if passed and max_price is not None:
            passed = price_n <= float(max_price)
        compare("price", 0.22, passed, f"asking_price={price_n}")

    min_beds = criteria.get("min_beds")
    if min_beds is not None:
        beds = facts.get("beds")
        compare(
            "minimum_beds",
            0.08,
            beds is not None and float(beds) >= float(min_beds),
            f"beds={beds}",
        )

    min_sqft = criteria.get("min_sqft")
    if min_sqft is not None:
        sqft = facts.get("sqft")
        compare(
            "minimum_sqft",
            0.08,
            sqft is not None and float(sqft) >= float(min_sqft),
            f"sqft={sqft}",
        )

    max_rehab = criteria.get("max_rehab")
    if max_rehab is not None:
        rehab = facts.get("rehab")
        compare(
            "maximum_rehab",
            0.10,
            rehab is not None and float(rehab) <= float(max_rehab),
            f"rehab={rehab}",
        )

    strategies = _set(criteria.get("strategies"))
    if strategies:
        actual = _set(facts.get("strategies"))
        compare("strategy", 0.10, bool(strategies & actual), f"property={sorted(actual)}")

    # Verification is a small trust/ranking signal, never a substitute for fit.
    verified_history = bool(profile.get("acquisition_history_verified"))
    possible += 0.05
    if verified_history:
        earned += 0.05
    trace.append(
        {
            "criterion": "verified_acquisition_history",
            "passed": verified_history,
            "weight": 0.05,
            "detail": "public recorder/acquisition history only",
        }
    )
    funds_verified = profile.get("verification_status") == "funds_verified"
    possible += 0.05
    if funds_verified:
        earned += 0.05
    trace.append(
        {
            "criterion": "funds_verification",
            "passed": funds_verified,
            "weight": 0.05,
            "detail": "verification status, no private balance used",
        }
    )

    score = earned / possible if possible else 0.0
    return {
        "match_score": round(score, 4),
        "criteria_trace": trace,
        "acquisition_history_verified": verified_history,
        "basis": "explicit_buy_box_and_verified_public_acquisition_history",
    }
