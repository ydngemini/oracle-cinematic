"""
Unit tests for the compliance_engine package.

Run with:  python -m pytest backend/compliance_engine/tests/ -v
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from compliance_engine import ComplianceEngine
from compliance_engine.evaluator import ConditionEvaluator
from compliance_engine.rules import Rule, RuleSet, SEVERITY_REQUIRED
from compliance_engine.closing import ClosingEngine, ATTORNEY_CLOSE_STATES
from compliance_engine.reciprocity import ReciprocityEngine


def _engine() -> ComplianceEngine:
    return ComplianceEngine.from_seed_directory()


FULL_CA_CONTEXT = {
    "state": "CA",
    "property_type": "single_family",
    "year_built": 1965,
    "in_flood_zone": True,
    "has_hoa": True,
    "transaction_type": "purchase",
    "representation": "dual_agency",
    "price": 850_000,
}


class TestConditionEvaluator:

    @pytest.fixture
    def ev(self) -> ConditionEvaluator:
        return ConditionEvaluator()

    def test_empty_condition_always_true(self, ev):
        assert ev.evaluate({}, {"state": "CA"}) is True

    def test_eq_string_case_insensitive(self, ev):
        assert ev.evaluate({"field": "state", "op": "eq", "value": "ca"}, {"state": "CA"})
        assert not ev.evaluate({"field": "state", "op": "eq", "value": "TX"}, {"state": "CA"})

    def test_lt_numeric(self, ev):
        assert ev.evaluate({"field": "year_built", "op": "lt", "value": 1978}, {"year_built": 1965})
        assert not ev.evaluate({"field": "year_built", "op": "lt", "value": 1978}, {"year_built": 1990})

    def test_in_list(self, ev):
        ctx = {"property_type": "single_family"}
        assert ev.evaluate(
            {"field": "property_type", "op": "in", "value": ["single_family", "condo"]},
            ctx,
        )
        assert not ev.evaluate(
            {"field": "property_type", "op": "in", "value": ["condo", "townhouse"]},
            ctx,
        )

    def test_all_combinator(self, ev):
        cond = {"all": [
            {"field": "year_built", "op": "lt", "value": 1978},
            {"field": "transaction_type", "op": "eq", "value": "purchase"},
        ]}
        assert ev.evaluate(cond, {"year_built": 1960, "transaction_type": "purchase"})
        assert not ev.evaluate(cond, {"year_built": 1985, "transaction_type": "purchase"})

    def test_any_combinator(self, ev):
        cond = {"any": [
            {"field": "in_flood_zone", "op": "eq", "value": True},
            {"field": "has_flooded_previously", "op": "eq", "value": True},
        ]}
        assert ev.evaluate(cond, {"in_flood_zone": True, "has_flooded_previously": False})
        assert ev.evaluate(cond, {"in_flood_zone": False, "has_flooded_previously": True})
        assert not ev.evaluate(cond, {"in_flood_zone": False, "has_flooded_previously": False})

    def test_not_combinator(self, ev):
        cond = {"not": {"field": "is_foreclosure", "op": "eq", "value": True}}
        assert ev.evaluate(cond, {"is_foreclosure": False})
        assert not ev.evaluate(cond, {"is_foreclosure": True})

    def test_exists(self, ev):
        assert ev.evaluate({"field": "hoa_fee", "op": "exists"}, {"hoa_fee": 350.0})
        assert not ev.evaluate({"field": "hoa_fee", "op": "exists"}, {"hoa_fee": None})
        assert not ev.evaluate({"field": "hoa_fee", "op": "exists"}, {})

    def test_dot_notation(self, ev):
        ctx = {"seller": {"is_individual": True}}
        assert ev.evaluate({"field": "seller.is_individual", "op": "eq", "value": True}, ctx)

    def test_invalid_op_raises(self, ev):
        with pytest.raises(ValueError, match="Unknown operator"):
            ev.evaluate({"field": "price", "op": "supergt", "value": 100}, {"price": 200})

    def test_numeric_type_coercion(self, ev):
        # string "1965" should coerce to float for numeric compare
        assert ev.evaluate({"field": "year_built", "op": "lt", "value": 1978}, {"year_built": "1965"})


class TestRuleSet:

    def test_load_from_seed_directory(self):
        seed_dir = Path(__file__).parent.parent / "seed_data"
        rs = RuleSet.from_seed_directory(seed_dir)
        assert len(rs) > 0

    def test_for_state_includes_federal(self):
        seed_dir = Path(__file__).parent.parent / "seed_data"
        rs = RuleSet.from_seed_directory(seed_dir)
        ca_rules = rs.for_state("CA")
        rule_ids = {r.rule_id for r in ca_rules}
        assert "FEDERAL-LEAD-PAINT-001" in rule_ids
        assert "CA-SELLER-DISCLOSURE-001" in rule_ids

    def test_active_date_filtering(self):
        rule = Rule(
            rule_id="TEST-FUTURE-001",
            state_code="CA",
            category="disclosure",
            title="Future Rule",
            description="Not yet in effect",
            severity=SEVERITY_REQUIRED,
            trigger_conditions={},
            required_actions=[],
            effective_date=date(2030, 1, 1),
        )
        rs = RuleSet([rule])
        assert len(rs.for_state("CA", as_of=date(2025, 1, 1))) == 0
        assert len(rs.for_state("CA", as_of=date(2031, 1, 1))) == 1

    def test_expiry_date_filtering(self):
        rule = Rule(
            rule_id="TEST-EXPIRED-001",
            state_code="TX",
            category="disclosure",
            title="Expired Rule",
            description="No longer in effect",
            severity=SEVERITY_REQUIRED,
            trigger_conditions={},
            required_actions=[],
            effective_date=date(2000, 1, 1),
            expiry_date=date(2010, 1, 1),
        )
        rs = RuleSet([rule])
        assert len(rs.for_state("TX", as_of=date(2005, 1, 1))) == 1
        assert len(rs.for_state("TX", as_of=date(2011, 1, 1))) == 0

    def test_snapshot(self):
        seed_dir = Path(__file__).parent.parent / "seed_data"
        rs = RuleSet.from_seed_directory(seed_dir)
        historic = rs.snapshot(date(1990, 1, 1))
        assert len(historic) < len(rs)

    def test_all_states_excludes_federal(self):
        seed_dir = Path(__file__).parent.parent / "seed_data"
        rs = RuleSet.from_seed_directory(seed_dir)
        states = rs.all_states()
        assert "FEDERAL" not in states
        assert "CA" in states


class TestDisclosureEngine:

    def test_ca_lead_paint_fires_pre1978(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "FEDERAL-LEAD-PAINT-001" in rule_ids

    def test_ca_lead_paint_does_not_fire_post1978(self):
        engine = _engine()
        ctx = {**FULL_CA_CONTEXT, "year_built": 1990}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "FEDERAL-LEAD-PAINT-001" not in rule_ids

    def test_ca_hoa_fires_with_hoa(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "CA-HOA-DISCLOSURE-001" in rule_ids

    def test_ca_hoa_does_not_fire_without_hoa(self):
        engine = _engine()
        ctx = {**FULL_CA_CONTEXT, "has_hoa": False}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "CA-HOA-DISCLOSURE-001" not in rule_ids

    def test_ca_tds_required(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        rule_ids = {d.rule_id for d in result.required_disclosures}
        assert "CA-SELLER-DISCLOSURE-001" in rule_ids

    def test_ca_dual_agency_disclosure_fires(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "CA-DUAL-AGENCY-001" in rule_ids

    def test_ca_dual_agency_does_not_fire_single_agency(self):
        engine = _engine()
        ctx = {**FULL_CA_CONTEXT, "representation": "seller_only"}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "CA-DUAL-AGENCY-001" not in rule_ids

    def test_tx_flood_fires_in_flood_zone(self):
        engine = _engine()
        ctx = {"state": "TX", "property_type": "single_family",
               "transaction_type": "purchase", "in_flood_zone": True}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "TX-FLOOD-ZONE-001" in rule_ids

    def test_tx_flood_does_not_fire_without_flood(self):
        engine = _engine()
        ctx = {"state": "TX", "property_type": "single_family",
               "transaction_type": "purchase", "in_flood_zone": False,
               "has_flooded_previously": False}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "TX-FLOOD-ZONE-001" not in rule_ids

    def test_fl_radon_always_fires_on_purchase(self):
        engine = _engine()
        ctx = {"state": "FL", "property_type": "single_family",
               "transaction_type": "purchase"}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "FL-RADON-DISCLOSURE-001" in rule_ids

    def test_il_radon_fires_on_purchase(self):
        engine = _engine()
        ctx = {"state": "IL", "property_type": "single_family",
               "transaction_type": "purchase"}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "IL-RADON-DISCLOSURE-001" in rule_ids

    def test_pa_disclosure_required(self):
        engine = _engine()
        ctx = {"state": "PA", "property_type": "single_family",
               "transaction_type": "purchase"}
        result = engine.check(ctx)
        rule_ids = {d.rule_id for d in result.required_disclosures}
        assert "PA-SELLER-DISCLOSURE-001" in rule_ids

    def test_required_form_ids_deduplicated(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        form_ids = result.required_form_ids
        assert len(form_ids) == len(set(form_ids))

    def test_to_dict_json_serialisable(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        serialised = result.to_dict()
        json_str = json.dumps(serialised)
        assert len(json_str) > 100

    def test_summary_returns_string(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        s = result.summary()
        assert "CA" in s
        assert "Closing" in s


class TestClosingEngine:

    @pytest.fixture
    def closing(self) -> ClosingEngine:
        return ClosingEngine()

    def test_ca_is_escrow_dry_funding(self, closing):
        profile = closing.get_closing_profile({"state": "CA", "price": 500_000})
        assert profile.closing_method == "escrow"
        assert profile.is_dry_funding is True
        assert profile.is_community_property_state is True
        assert profile.requires_attorney is False

    def test_ny_is_attorney_close(self, closing):
        profile = closing.get_closing_profile({"state": "NY", "price": 500_000})
        assert profile.closing_method == "attorney_close"
        assert profile.requires_attorney is True

    def test_ny_mansion_tax_warning(self, closing):
        profile = closing.get_closing_profile({"state": "NY", "price": 1_500_000})
        assert any("Mansion Tax" in w for w in profile.warnings)

    def test_ny_no_mansion_tax_below_threshold(self, closing):
        profile = closing.get_closing_profile({"state": "NY", "price": 900_000})
        assert not any("Mansion Tax" in w for w in profile.warnings)

    def test_tx_title_company_no_transfer_tax(self, closing):
        profile = closing.get_closing_profile({"state": "TX"})
        assert profile.closing_method == "title_company"
        assert profile.transfer_tax_payer == "negotiable"

    def test_wa_is_escrow_dry(self, closing):
        profile = closing.get_closing_profile({"state": "WA", "price": 600_000})
        assert profile.closing_method == "escrow"
        assert profile.is_dry_funding is True
        assert any("REET" in w for w in profile.warnings)

    def test_ga_attorney_close(self, closing):
        profile = closing.get_closing_profile({"state": "GA"})
        assert profile.requires_attorney is True

    def test_il_hybrid_method(self, closing):
        profile = closing.get_closing_profile({"state": "IL"})
        assert profile.closing_method == "hybrid"

    def test_il_cook_county_warning(self, closing):
        profile = closing.get_closing_profile({"state": "IL", "county": "Cook"})
        assert any("Cook County" in w for w in profile.warnings)

    def test_az_community_property(self, closing):
        profile = closing.get_closing_profile({"state": "AZ"})
        assert profile.is_community_property_state is True

    def test_unknown_state_raises(self, closing):
        with pytest.raises(ValueError):
            closing.get_closing_profile({"state": "XX"})

    def test_attorney_close_states_list(self):
        states = ClosingEngine.attorney_close_states()
        assert "NY" in states
        assert "GA" in states
        assert "CA" not in states

    def test_escrow_states_list(self):
        states = ClosingEngine.escrow_states()
        assert "CA" in states
        assert "WA" in states
        assert "OR" in states


class TestReciprocityEngine:

    @pytest.fixture
    def rec(self) -> ReciprocityEngine:
        return ReciprocityEngine()

    def test_same_state_always_full(self, rec):
        result = rec.check("CA", "CA")
        assert result.level == "full"

    def test_co_ga_full_reciprocity(self, rec):
        result = rec.check("CO", "GA")
        assert result.level == "full"

    def test_ga_co_full_reciprocity(self, rec):
        result = rec.check("GA", "CO")
        assert result.level == "full"

    def test_ca_tx_no_reciprocity(self, rec):
        result = rec.check("CA", "TX")
        assert result.level == "none"

    def test_ca_nv_partial(self, rec):
        result = rec.check("CA", "NV")
        assert result.level == "partial"

    def test_no_reciprocity_state(self, rec):
        # DE has no reciprocity with anyone
        result = rec.check("CA", "DE")
        assert result.level == "none"

    def test_reciprocal_states_for_tx(self, rec):
        results = rec.reciprocal_states_for("TX")
        host_states = {r.host_state for r in results}
        assert "AR" in host_states
        assert "OK" in host_states

    def test_extra_records_override(self):
        from compliance_engine.reciprocity import ReciprocityRecord
        extra = [ReciprocityRecord(home_state="ZZ", host_state="YY", level="full")]
        rec = ReciprocityEngine(extra_records=extra)
        result = rec.check("ZZ", "YY")
        assert result.level == "full"

    def test_to_dict(self, rec):
        result = rec.check("FL", "GA")
        d = result.to_dict()
        assert "level" in d
        assert "home_state" in d


class TestComplianceEngineFacade:

    def test_check_returns_compliance_result(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        assert result.context["state"] == "CA"
        assert result.rules_evaluated > 0

    def test_dual_agency_warning_appended(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        assert any("dual agency" in w.lower() for w in result.warnings)

    def test_community_property_warning_for_ca(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        assert any("community property" in w.lower() for w in result.warnings)

    def test_high_value_gto_warning(self):
        engine = _engine()
        ctx = {**FULL_CA_CONTEXT, "price": 5_000_000, "is_cash": True}
        result = engine.check(ctx)
        assert any("FinCEN" in w for w in result.warnings)

    def test_list_states_with_rules(self):
        engine = _engine()
        states = engine.list_states_with_rules()
        assert "CA" in states
        assert "NY" in states
        assert "TX" in states

    def test_get_rules_for_state_category_filter(self):
        engine = _engine()
        rules = engine.get_rules_for_state("CA", category="disclosure")
        assert all(r.category == "disclosure" for r in rules)

    def test_check_reciprocity(self):
        engine = _engine()
        result = engine.check_reciprocity("CO", "GA")
        assert result.level == "full"

    def test_historical_check(self):
        engine = _engine()
        # Check as of 1996-09-05 — lead paint rule effective 1996-09-06 should NOT fire
        result = engine.check(FULL_CA_CONTEXT, as_of=date(1996, 9, 5))
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "FEDERAL-LEAD-PAINT-001" not in rule_ids

    def test_historical_check_after_effective(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT, as_of=date(1996, 9, 7))
        rule_ids = {d.rule_id for d in result.disclosures}
        assert "FEDERAL-LEAD-PAINT-001" in rule_ids

    def test_to_json_round_trip(self):
        engine = _engine()
        result = engine.check(FULL_CA_CONTEXT)
        json_str = result.to_json()
        parsed = json.loads(json_str)
        assert parsed["context"]["state"] == "CA"
        assert "disclosures" in parsed
        assert "closing_profile" in parsed
