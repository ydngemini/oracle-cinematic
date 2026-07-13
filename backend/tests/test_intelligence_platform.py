import asyncio
import base64
import hashlib
import hmac
from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from data_integrations.cache import canonical_request_hash
from data_integrations.periodic import build_default_scheduler
from command_providers import (
    ProviderConfigurationError,
    ProviderRequestError,
    create_google_calendar_event,
    place_twilio_call,
    send_ses_email,
    verify_twilio_signature,
)
from graph_engine import PropertyGraph
from intelligence_engine import (
    IntelligenceInputError,
    analyze_highest_best_use,
    calculate_underwriting,
    forecast_micro_market,
    negotiation_guidance,
    preliminary_title_summary,
    score_pre_distress,
)
from marketplace_engine import rank_buyer_request
from ml_forge.synthetic_lawyer import (
    BUILTIN_CONTRACT_TEMPLATES,
    defensive_redline,
    render_approved_contract_template,
    validate_contract_template,
)
from platform_policy import (
    EvidenceStatus,
    Feature,
    IntelligenceEnvelope,
    SourceCitation,
    enforce_public_property_data,
    feature_enabled,
    prohibited_fields,
    require_feature,
)
from property_inference import (
    InferenceInputError,
    analyze_topography,
    estimate_photo_rehab,
    impute_characteristics,
    tour_variant_manifest,
)


def test_policy_rejects_nested_protected_and_private_inputs_without_value_scanning():
    payload = {
        "property": {"address": "12 Church Street", "tax_delinquency": True},
        "targeting": {"consumer-credit-utilization": 0.7, "children_count": 2},
    }
    assert prohibited_fields(payload) == [
        "targeting.consumer_credit_utilization",
        "targeting.children_count",
    ]
    with pytest.raises(HTTPException) as exc:
        enforce_public_property_data(payload)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "PROHIBITED_DATA"
    enforce_public_property_data({"address": "12 Church Street", "zoning": "R5"})


def test_intelligence_envelope_requires_observed_source():
    inferred = SourceCitation(
        source="derived feature",
        observed_at=date(2026, 7, 1),
        evidence_status=EvidenceStatus.INFERRED,
    )
    with pytest.raises(ValidationError):
        IntelligenceEnvelope(
            analysis_type="forecast",
            subject_id="market-1",
            evidence_status=EvidenceStatus.INFERRED,
            observation_date=date(2026, 7, 1),
            confidence=0.5,
            model_version="test-v1",
            sources=[inferred],
            result={},
        )


def test_distress_score_never_claims_probability_until_validation_gate_passes():
    signals = {"tax_delinquency": 1, "vacancy": 0.5, "open_violations": 1}
    unvalidated = score_pre_distress(signals)
    assert unvalidated["calibrated_probability"] is None
    assert unvalidated["is_probability_validated"] is False
    validated = score_pre_distress(
        signals,
        calibration={
            "validated": True,
            "holdout_size": 5000,
            "brier_score": 0.16,
            "geographic_bias_reviewed": True,
            "temporal_leakage_reviewed": True,
        },
    )
    assert validated["is_probability_validated"] is True
    assert validated["calibrated_probability"] == validated["evidence_score"]
    with pytest.raises(IntelligenceInputError):
        score_pre_distress({"credit_score": 1})


def test_underwriting_and_hbu_are_reproducible_fixed_case_calculations():
    result = calculate_underwriting(
        subject_sqft=1000,
        comparables=[
            {"record_id": "a", "sale_price": 180000, "sqft": 900},
            {"record_id": "b", "sale_price": 220000, "sqft": 1100},
            {"record_id": "c", "sale_price": 200000, "sqft": 1000},
        ],
        rehab_items=[{"category": "roof", "quantity": 1, "unit_cost": 20000}],
        acquisition_ratio=0.70,
    )
    assert result["arv"] == 200000.0
    assert result["rehab"] == 20000.0
    assert result["mao"] == 120000.0
    assert "MAO = max(0" in result["trace"]["formulas"][2]
    assert len(result["trace"]["comparable_evidence"]) == 3

    hbu = analyze_highest_best_use(
        lot_area_sqft=5000,
        building_area_sqft=3000,
        max_far=1.5,
        max_lot_coverage=0.6,
        allowed_uses=["residential", "residential"],
        dimensional_limits={"height_ft": 35},
        land_comparables=[{"record_id": "land-1", "sale_price": 500000, "lot_area_sqft": 5000}],
    )
    assert hbu["gross_buildable_area_sqft"] == 7500.0
    assert hbu["remaining_buildable_area_sqft"] == 4500.0
    assert hbu["max_footprint_sqft"] == 3000.0
    assert hbu["air_rights_indicator"] is True


def test_title_forecast_and_negotiation_keep_review_boundaries():
    title = preliminary_title_summary(
        [{"kind": "lien", "record_id": "L1", "match_status": "possible_match", "chain_gap": True}]
    )
    assert title["unresolved_matches"] == 1
    assert title["chain_gaps"] == 1
    assert title["review_status"] == "professional_review_required"
    assert "not an abstract" in title["warnings"][0]

    forecast = forecast_micro_market(
        [{"year": 2022, "value": 100}, {"year": 2023, "value": 105}, {"year": 2024, "value": 110}],
        horizon_years=5,
    )
    assert len(forecast["forecast"]) == 5
    assert forecast["fair_housing_review"]["prohibited_scope"] == "individual targeting or service eligibility"
    assert all(len(row["confidence_interval_95"]) == 2 for row in forecast["forecast"])

    assert negotiation_guidance(counter_offer=120000, arv=200000, rehab=20000)["threshold"] == "green"
    red = negotiation_guidance(counter_offer=150000, arv=200000, rehab=20000)
    assert red["threshold"] == "red"
    assert red["profiling_used"] is False
    assert red["requires_agent_approval"] is True


def test_entity_graph_preserves_public_roles_and_never_invents_beneficial_owner():
    async def build():
        graph = PropertyGraph()
        await graph.ingest_public_record(
            {
                "record_type": "DEED",
                "record_id": "D-100",
                "parcel_id": "PIN-1",
                "address": "1 Main St",
                "source": "County Recorder",
                "acquisition_entity": {"name": "Example Homes LLC", "beneficial_owner": "Hidden Person"},
                "officers": [{"name": "Alex Officer", "title": "Manager", "email": "private@example.com"}],
                "phone": "+15555555555",
            }
        )
        # A second record with no owner must not synthesize one.
        await graph.ingest_public_record(
            {"record_type": "ASSESSOR", "record_id": "A-2", "parcel_id": "PIN-2", "address": "2 Main St"}
        )
        return graph.export()

    exported = asyncio.run(build())
    flattened = repr(exported)
    assert "Hidden Person" not in flattened
    assert "private@example.com" not in flattened
    assert "+15555555555" not in flattened
    assert sum(node["type"] == "AcquisitionEntity" for node in exported["nodes"]) == 1
    assert sum(node["type"] == "Officer" for node in exported["nodes"]) == 1
    assert any(edge["type"] == "OFFICER_OF" for edge in exported["edges"])
    assert not any(edge["type"] == "BENEFICIAL_OWNER_OF" for edge in exported["edges"])
    assert exported["policy"]["beneficial_ownership_inferred"] is False


def test_property_inference_outputs_confidence_bands_and_disclosures():
    inferred = impute_characteristics(
        ["beds", "construction"],
        [
            {"beds": 3, "construction": "brick"},
            {"beds": 3, "construction": "brick"},
            {"beds": 4, "construction": "frame"},
        ],
    )
    assert {row["status"] for row in inferred["inferences"]} == {"inferred"}
    assert all(0 <= row["confidence"] <= 0.95 for row in inferred["inferences"])

    rehab = estimate_photo_rehab(
        [{"component": "roof", "quantity": 1, "unit_cost_low": 10000, "unit_cost_high": 15000, "confidence": 0.5}]
    )
    assert rehab["rehab_cost_band"][0] < rehab["rehab_cost_band"][1]
    assert "on-site inspection" in rehab["warnings"][1]

    terrain = analyze_topography(
        [
            {"elevation_ft": 100, "distance_ft": 0, "bearing_deg": 0},
            {"elevation_ft": 110, "distance_ft": 100, "bearing_deg": 90},
            {"elevation_ft": 105, "distance_ft": 200, "bearing_deg": 180},
        ]
    )
    assert terrain["viewshed_status"] == "source_samples_only_no_line_of_sight_guarantee"

    tour = tour_variant_manifest(
        variant_name="Post rehab", style="accessible", rehab_scope=[{"room": "kitchen"}], source_media_ids=["m1"]
    )
    assert tour["observed_geometry_preserved"] is True
    assert "AI-generated" in tour["disclosure"]
    with pytest.raises(InferenceInputError):
        tour_variant_manifest(variant_name="x", style="deceptive", rehab_scope=[{}], source_media_ids=["m1"])


def test_legal_templates_reject_format_injection_and_require_review():
    bad = validate_contract_template(
        "seller_purchase",
        "Seller {seller.__class__}",
        ["seller"],
    )
    assert bad["valid"] is False
    assert any(issue.startswith("template_field_invalid") for issue in bad["issues"])

    template = BUILTIN_CONTRACT_TEMPLATES["seller-purchase-standard"]
    rendered = render_approved_contract_template(
        document_type=template["document_type"],
        body_template=template["body_template"],
        required_fields=template["required_fields"],
        transaction_data={
            "current_date": "July 13, 2026",
            "seller_name": "Seller One",
            "buyer_name": "Buyer One",
            "property_address": "1 Main St",
            "purchase_price": 100000,
            "earnest_money_deposit": 1000,
            "closing_date": "August 1, 2026",
            "approved_addenda": "None",
        },
    )
    assert rendered["status"] == "SUCCESS"
    assert rendered["professional_review_required"] is True
    assert len(rendered["content_sha256"]) == 64

    redline = defensive_redline(
        "Purchase price is $100,000.\nClosing is August 1.",
        "Purchase price is $110,000.\nClosing is August 15.",
    )
    assert redline["status"] == "SUCCESS"
    assert redline["professional_review_required"] is True
    assert {"financial", "timing"}.issubset(set(redline["changes"][0]["literal_risk_flags"]))


def test_cache_hash_is_canonical_and_secret_independent():
    left = canonical_request_hash(
        "ArcGIS",
        {"where": "STATE='NY'", "params": {"limit": 100, "api_key": "secret-a"}, "token": "one"},
    )
    right = canonical_request_hash(
        " arcgis ",
        {"params": {"api_key": "secret-b", "limit": 100}, "where": "STATE='NY'", "token": "two"},
    )
    assert left == right
    assert len(left) == 64


def test_buyer_matching_uses_explicit_buy_box_and_verified_history_only():
    ranked = rank_buyer_request(
        {"state": "NY", "county": "Kings", "property_type": "SFR", "asking_price": 300000, "beds": 3},
        {
            "states": ["NY"], "counties": ["Kings"], "property_types": ["SFR"],
            "min_price": 200000, "max_price": 400000,
            "acquisition_history_verified": True, "verification_status": "funds_verified",
        },
        {"min_beds": 3},
    )
    assert ranked["match_score"] == 1.0
    assert ranked["basis"] == "explicit_buy_box_and_verified_public_acquisition_history"
    assert all("race" not in repr(item).lower() for item in ranked["criteria_trace"])


def test_feature_flags_are_independent_and_disabled_features_do_not_advertise(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_AUTOMATION", "false")
    monkeypatch.setenv("ORACLE_FEATURE_MARKETPLACE", "true")
    assert feature_enabled(Feature.AUTOMATION) is False
    assert feature_enabled(Feature.MARKETPLACE) is True
    with pytest.raises(HTTPException) as exc:
        require_feature(Feature.AUTOMATION)
    assert exc.value.status_code == 404

    monkeypatch.setenv("ORACLE_FEATURE_MUNICIPAL_HARVESTS", "false")
    scheduler = build_default_scheduler()
    assert scheduler._tasks["distress_scrape"].enabled is False
    assert scheduler._tasks["parcel_harvest"].enabled is False
    assert scheduler._tasks["platform_retention_cleanup"].enabled is True
    assert scheduler._tasks["platform_source_health"].enabled is True


def test_twilio_signature_is_constant_time_compatible_and_rejects_tampering():
    url = "https://neoh.example/api/commands/webhooks/twilio"
    form = {"CallSid": "CA123", "CallStatus": "completed", "SequenceNumber": "1"}
    material = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    signature = base64.b64encode(
        hmac.new(b"test-token", material.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    assert verify_twilio_signature(
        url=url, form=form, signature=signature, auth_token="test-token"
    )
    assert not verify_twilio_signature(
        url=url,
        form={**form, "CallStatus": "in-progress"},
        signature=signature,
        auth_token="test-token",
    )
    assert not verify_twilio_signature(
        url="file:///tmp/webhook", form=form, signature=signature, auth_token="test-token"
    )


def test_provider_adapters_fail_closed_before_any_network_call(monkeypatch):
    monkeypatch.delenv("ORACLE_SES_FROM_EMAIL", raising=False)
    with pytest.raises(ProviderConfigurationError):
        asyncio.run(
            send_ses_email(
                {"target": {"email": "seller@example.test"}, "subject": "Terms", "body": "Body"}
            )
        )

    with pytest.raises(ProviderRequestError):
        asyncio.run(
            place_twilio_call(
                {"target": {"phone": "555-0100"}},
                credentials={
                    "account_sid": "AC123",
                    "auth_token": "token",
                    "from_number": "+15555550101",
                    "twiml_url": "https://example.test/twiml",
                },
            )
        )

    with pytest.raises(ProviderRequestError):
        asyncio.run(
            create_google_calendar_event(
                {"event": {"summary": "Inspection"}}, access_token="oauth-token"
            )
        )
