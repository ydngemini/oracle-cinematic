import pytest

from commands_api import (
    CommandType,
    ParsedIntent,
    _mao_payload,
    _parse_intent,
    _state_hint,
    _validate_command_payload,
)


CONTACT_ID = "22222222-2222-2222-2222-222222222222"


def test_command_intent_classifier_is_closed_and_deterministic():
    assert _parse_intent("Email John about the offer") is ParsedIntent.EMAIL
    assert _parse_intent("Text John about the offer") is ParsedIntent.SMS
    assert _parse_intent("Schedule a call with Sarah") is ParsedIntent.CALENDAR
    assert _parse_intent("Draft an assignment contract for Alex") is ParsedIntent.CONTRACT
    assert _parse_intent("Calculate the MAO for this property") is ParsedIntent.MAO_CALC


def test_text_request_mentioning_a_call_stays_an_sms():
    # "call" and "text" collide constantly in this domain; misrouting a text
    # into the LIVE_CALL risk class is the expensive direction of the error.
    assert _parse_intent("Send Dana a text about our call tomorrow") is ParsedIntent.SMS
    assert _parse_intent("Call Dana about the offer") is ParsedIntent.CALL


def test_text_as_a_noun_does_not_hijack_email_and_call_commands():
    # SMS is tested before EMAIL and CALL, so a bare "text" used as a noun would
    # otherwise turn these into billable outbound messages under the wrong
    # compliance gate — and send them to the contact's phone, not their inbox.
    assert _parse_intent("Email Sarah the text of the counteroffer") is ParsedIntent.EMAIL
    assert _parse_intent("Email her the full original text") is ParsedIntent.EMAIL
    assert _parse_intent("Call him about their last text") is ParsedIntent.CALL
    # Genuine SMS phrasings must survive the noun stripping.
    assert _parse_intent("Send the client a text") is ParsedIntent.SMS
    assert _parse_intent("Send her the text message about closing") is ParsedIntent.SMS


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


def test_sms_command_requires_canonical_contact_state_and_bounded_body():
    target = {
        "contact_id": CONTACT_ID,
        "phone": "+13055550142",
        "state_code": "FL",
    }
    _validate_command_payload(CommandType.SMS, target, {"body": "Your showing is confirmed."})

    with pytest.raises(ValueError, match="reference"):
        _validate_command_payload(
            CommandType.SMS,
            {"phone": "+13055550142", "state_code": "FL"},
            {"body": "Hello"},
        )
    with pytest.raises(ValueError, match="state_code"):
        _validate_command_payload(
            CommandType.SMS,
            {**target, "state_code": "1!"},
            {"body": "Hello"},
        )
    with pytest.raises(ValueError, match="1-1600"):
        _validate_command_payload(CommandType.SMS, target, {"body": "x" * 1601})
