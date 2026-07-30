from commands_api import ParsedIntent, _mao_payload, _parse_intent, _state_hint


def test_command_intent_classifier_is_closed_and_deterministic():
    assert _parse_intent("Email John about the offer") is ParsedIntent.EMAIL
    assert _parse_intent("Schedule a call with Sarah") is ParsedIntent.CALENDAR
    assert _parse_intent("Draft an assignment contract for Alex") is ParsedIntent.CONTRACT
    assert _parse_intent("Calculate the MAO for this property") is ParsedIntent.MAO_CALC


def test_contract_state_hint_is_limited_to_supported_seed_states():
    assert _state_hint("Draft this for Delaware") == "DE"
    assert _state_hint("Use the PA assignment") == "PA"
    assert _state_hint("No state supplied") == ""


def test_mao_payload_uses_exact_seventy_percent_formula():
    result, missing = _mao_payload(
        {"payload": {"after_repair_value": "250000", "rehab_estimate": "35000"}}
    )
    assert missing == []
    assert result["formula"] == "ARV * 0.70 - Rehab"
    assert result["mao"] == "140000.00"


def test_mao_payload_fails_closed_when_property_data_is_missing():
    result, missing = _mao_payload({"payload": {"arv": "250000"}})
    assert result["mao"] is None
    assert missing == ["rehab"]
