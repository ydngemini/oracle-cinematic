from decimal import Decimal

from intelligence_engine import negotiation_guidance
from voice_intel import (
    _objective_objection_response,
    extract_counter_offer,
    voice_session_group,
)


def test_extract_counter_offer_from_explicit_seller_floor():
    assert extract_counter_offer("I won't take less than $165,000.") == Decimal("165000.00")
    assert extract_counter_offer("My number is 185k and that is firm.") == Decimal("185000.00")


def test_repair_amount_is_not_misclassified_as_counter_offer():
    assert extract_counter_offer("The roof replacement estimate is $12k.") is None


def test_mao_objection_uses_only_supplied_repair_fact():
    guidance = negotiation_guidance(counter_offer=175_000, arv=250_000, rehab=20_000)
    draft = _objective_objection_response(
        guidance,
        {"repair_items": {"roof replacement": {"cost": 12_000}}},
        {},
    )
    assert "$12,000" in draft
    assert "roof replacement" in draft
    assert "verify" in draft.lower()


def test_voice_group_is_tenant_and_session_specific():
    assert voice_session_group("tenant-a", "session-b") == "voice:tenant-a:session-b"
