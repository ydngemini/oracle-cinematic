"""The payment card redactor (pan_redaction.py, PCI DSS SAQ A, ledger L17-07).

Unit level: what it removes, what it must leave alone, that it never records
the number it removed, the log filter, and the persona rule every AI voice and
chat prompt carries. The write paths that apply it are
tests/test_pan_redaction_paths.py; real PostgreSQL is
tests/test_pan_redaction_live.py.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import random
import uuid
from pathlib import Path

import pytest

import pan_redaction
from pan_redaction import CVV_PLACEHOLDER, PAN_PLACEHOLDER, PAYMENT_CARD_RULE, redact

BACKEND = Path(__file__).resolve().parents[1]
WORDS = "zero one two three four five six seven eight nine".split()


def _spoken(digits: str) -> str:
    return " ".join(WORDS[int(d)] for d in digits)


# ── true positives ─────────────────────────────────────────────────────────

CARDS = {
    "visa": "4111111111111111",
    "visa13": "4222222222222",
    "visa19": "4111111111111111110",
    "mastercard": "5555555555554444",
    "mastercard2": "2223003122003222",
    "amex": "378282246310005",
    "discover": "6011111111111117",
    "diners": "30569309025904",
    "jcb": "3530111333300000",
    "unionpay": "6200000000000005",
}


@pytest.mark.parametrize("name", sorted(CARDS))
def test_a_solid_card_number_is_removed(name):
    pan = CARDS[name]
    assert pan_redaction.is_card_number(pan), "fixture must be a valid card number"
    result = redact(f"my card is {pan}, thanks")
    assert result.text == f"my card is {PAN_PLACEHOLDER}, thanks"
    assert result.cards == 1


@pytest.mark.parametrize("text", [
    "card 4111 1111 1111 1111 please",
    "card 4111-1111-1111-1111 please",
    "card 4111.1111.1111.1111 please",
    "card 4111, 1111, 1111, 1111 please",
    "card 3782 822463 10005 please",          # Amex 4-6-5
    "card 3056 930902 5904 please",           # Diners 4-6-4
    "card 4222 2222 2222 2 please",           # Visa 13
])
def test_a_grouped_card_number_is_removed(text):
    result = redact(text)
    assert result.cards == 1 and PAN_PLACEHOLDER in result.text
    assert sum(ch.isdigit() for ch in result.text) == 0


@pytest.mark.parametrize("pan", [CARDS["visa"], CARDS["mastercard"], CARDS["amex"],
                                 CARDS["discover"], CARDS["mastercard2"]])
def test_a_dictated_card_number_is_removed(pan):
    for text in (_spoken(pan), _spoken(pan).replace("zero", "oh"),
                 " ".join(pan),                                # "4 1 1 1 ..."
                 ", ".join(_spoken(pan[i:i + 4]) for i in range(0, len(pan), 4))):
        result = redact(f"sure, it's {text}.")
        assert result.cards == 1, text
        assert not any(w in result.text.split() for w in WORDS[1:]), result.text


def test_a_dictated_card_followed_by_its_expiry_and_code_is_removed():
    text = _spoken(CARDS["visa"] + "1227" + "123")
    result = redact(f"it is {text}")
    assert result.cards == 1
    assert result.text.startswith(f"it is {PAN_PLACEHOLDER}")


def test_a_card_followed_by_an_expiry_keeps_the_expiry_text():
    result = redact("4111 1111 1111 1111 1227")
    assert result.text == f"{PAN_PLACEHOLDER} 1227"


def test_two_cards_in_one_message():
    result = redact("old 5555 5555 5555 4444, new 4111111111111111")
    assert result.cards == 2 and result.text == f"old {PAN_PLACEHOLDER}, new {PAN_PLACEHOLDER}"


@pytest.mark.parametrize("text, expected", [
    ("cvv 123", f"cvv {CVV_PLACEHOLDER}"),
    ("CVC: 4321", f"CVC: {CVV_PLACEHOLDER}"),
    ("the cvv2 is 987", f"the cvv2 is {CVV_PLACEHOLDER}"),
    ("card verification value 123", f"card verification value {CVV_PLACEHOLDER}"),
    ("cvv is one two three", f"cvv is {CVV_PLACEHOLDER}"),
])
def test_an_unambiguous_security_code_is_removed_on_its_own(text, expected):
    assert redact(text).text == expected


def test_a_loose_security_code_phrase_counts_only_next_to_a_card():
    with_card = redact("4111111111111111 and the security code is 123")
    assert with_card.text == f"{PAN_PLACEHOLDER} and the security code is {CVV_PLACEHOLDER}"
    assert with_card.codes == 1
    spoken = redact(_spoken(CARDS["visa"]) + ", the code on the back is one two three")
    assert spoken.text.endswith(f"the code on the back is {CVV_PLACEHOLDER}")
    # The same words in a login text are not a card code.
    assert redact("Your security code is 482913").changed is False
    assert redact("security code 123, no card here").changed is False


# ── false positives ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "call me at 302-555-0100 or 302-555-0199",
    "302-555-0100, 302-555-0199",
    "3025550100 3025550199",
    "+1 (302) 555-0100",
    "+1 302 555 0100 ext 1234",
    "3025550100 19901",
    "APN: 1-16-06.00-028.00-001",
    "parcel 17-16-404-036-0000",
    "parcel 4111111111111111",                # a parcel id that happens to be Luhn-valid
    "MLS# 1234567, MLS id ACT123456",
    "folio 4111111111111111",
    "list price $1,250,000; offer 1250000.00",
    "ARV 450000 rehab 65000 MAO 250000",
    "zip 19901-1234",
    "id 123e4567-e89b-12d3-a456-426614174000",
    "client 22222222-2222-2222-2222-222222222222",
    "ts 1760400000000",
    "2026-10-14 12:30:00",
    "ref 12345678901234567890123",
    "1234567812345670",                       # Luhn-valid, but no card network starts with 1
    "9111111111111111",
    "someone said phone money tone none",
    "one two three four",
    "four bedrooms, two baths, one garage",
    "Your verification code is 123456",
    "192.168.100.200",
])
def test_ordinary_numbers_are_left_alone(text):
    result = redact(text)
    assert result.text == text and not result.changed


def _phone(rng):
    return rng.choice(["302", "215", "347", "360", "385", "646", "212", "610"]) + \
        "".join(rng.choice("0123456789") for _ in range(7))


def test_seeded_sample_of_phone_numbers_stays_bounded():
    """Written phone numbers, pairs and phone+ZIP never match; the one known
    false positive (two numbers dictated digit by digit with no word between)
    stays rare. Seeded so the bound is reproducible."""
    rng = random.Random(20261014)
    for _ in range(5_000):
        a, b = _phone(rng), _phone(rng)
        for text in (f"{a[:3]}-{a[3:6]}-{a[6:]}", f"({a[:3]}) {a[3:6]}-{a[6:]}",
                     f"{a} {b}", f"{a[:3]}-{a[3:6]}-{a[6:]} {b[:3]}-{b[3:6]}-{b[6:]}",
                     f"{a} 19901", _spoken(a)):
            assert not redact(text).changed, text
    dictated_pairs = sum(redact(_spoken(_phone(rng) + _phone(rng))).changed for _ in range(5_000))
    assert dictated_pairs / 5_000 < 0.03


def test_luhn_and_network_rules():
    assert pan_redaction.luhn_valid("4111111111111111")
    assert not pan_redaction.luhn_valid("4111111111111112")
    assert not pan_redaction.is_card_number("5555555555554444"[:15])   # Mastercard is 16
    assert not pan_redaction.is_card_number("378282246310005" + "0")   # Amex is 15


def test_non_text_values_pass_through():
    assert redact(None).text is None
    assert redact("").text == ""
    assert redact(12345).text == 12345


def test_redact_obj_walks_nested_values_and_keeps_keys():
    value = {"body": "card 4111111111111111", "items": ["cvv 123", 5, None],
             "4111111111111111": "key untouched", "nested": {"t": ("x", "5555555555554444")}}
    cleaned, cards, codes = pan_redaction.redact_obj(value)
    assert (cards, codes) == (2, 1)
    assert cleaned["body"] == f"card {PAN_PLACEHOLDER}"
    assert cleaned["items"] == [f"cvv {CVV_PLACEHOLDER}", 5, None]
    assert cleaned["nested"]["t"] == ("x", PAN_PLACEHOLDER)
    assert "4111111111111111" in cleaned


# ── what is recorded: never the number ─────────────────────────────────────

def test_report_records_surface_and_counts_only(monkeypatch, caplog):
    audited = []

    async def fake_audit(surface, cards, codes, tenant_id, target_id):
        audited.append((surface, cards, codes, tenant_id, target_id))

    monkeypatch.setattr(pan_redaction, "_audit", fake_audit)
    monkeypatch.setattr(pan_redaction, "REDACTION_COUNTS", {})
    tenant, target = str(uuid.uuid4()), str(uuid.uuid4())

    async def run():
        out = pan_redaction.scrub("pay with 4111 1111 1111 1111 cvv 123", "unit_surface",
                                  tenant_id=tenant, target_id=target)
        await asyncio.gather(*list(pan_redaction._pending))
        return out

    with caplog.at_level(logging.WARNING, logger="oracle.pci"):
        out = asyncio.run(run())
    assert out == f"pay with {PAN_PLACEHOLDER} cvv {CVV_PLACEHOLDER}"
    assert audited == [("unit_surface", 1, 1, tenant, target)]
    assert pan_redaction.REDACTION_COUNTS == {"unit_surface": 2}
    assert "surface=unit_surface cards=1 codes=1" in caplog.text
    assert "4111" not in caplog.text and "123" not in caplog.text.replace(tenant, "")


def test_report_ignores_non_uuid_ids_and_runs_without_a_loop(monkeypatch):
    audited = []

    async def fake_audit(*args):
        audited.append(args)

    monkeypatch.setattr(pan_redaction, "_audit", fake_audit)
    # No running loop: the log line and counter still happen; no audit task.
    assert pan_redaction.scrub("4111111111111111", "sync_surface", tenant_id="t") == PAN_PLACEHOLDER
    assert audited == []
    assert pan_redaction._uuid_or_none("agent@x.test") is None


def test_the_audit_event_carries_counts_not_text(monkeypatch):
    import audit_ledger

    recorded = {}

    async def fake_record(**kwargs):
        recorded.update(kwargs)
        return {}

    monkeypatch.setattr(audit_ledger.ledger, "record", fake_record)
    asyncio.run(pan_redaction._audit("sms_inbound", 1, 0, None, None))
    assert recorded["category"] is audit_ledger.AuditCategory.PCI_REDACTION
    assert recorded["metadata"] == {"surface": "sms_inbound", "cards": 1, "codes": 0}


def test_scrub_returns_the_original_object_when_nothing_matches():
    text = "Prefers afternoon showings"
    assert pan_redaction.scrub(text, "x") is text


# ── logs ───────────────────────────────────────────────────────────────────

def test_the_log_filter_removes_card_numbers_from_messages_and_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("tests.pan_log_filter")
    log.propagate = False
    log.addHandler(handler)
    pan_redaction.install_log_redaction(log)
    pan_redaction.install_log_redaction(log)
    assert sum(isinstance(f, pan_redaction.PanLogRedactionFilter) for f in handler.filters) == 1
    try:
        log.warning("caller said %s", "4111 1111 1111 1111")
        try:
            raise ValueError("bad card 5555555555554444")
        except ValueError:
            log.exception("handler failed")
        log.warning("call %s lasted %d s", "3025550100", 42)
    finally:
        log.removeHandler(handler)
    output = stream.getvalue()
    assert "4111" not in output and "5555555555554444" not in output
    assert f"caller said {PAN_PLACEHOLDER}" in output
    assert f"bad card {PAN_PLACEHOLDER}" in output
    assert "call 3025550100 lasted 42 s" in output


def test_the_server_installs_the_log_filter():
    import server

    src = inspect.getsource(server._configure_logging)
    assert "install_log_redaction()" in src


# ── the persona rule ───────────────────────────────────────────────────────

def test_the_rule_says_never_ask_never_read_back_and_points_to_the_secure_link():
    rule = PAYMENT_CARD_RULE.lower()
    for phrase in ("never ask for", "read back", "security code", "never write one into a note",
                   "secure payment link", "never by phone"):
        assert phrase in rule, phrase


def test_every_ai_persona_carries_the_payment_card_rule():
    import ai_chat_agent
    import elevenlabs_realtime
    import inbound_voice
    import neoh_persona
    import qwen_omni_realtime
    import qwen_voice_agent

    prompts = {
        "chat (full)": neoh_persona.build_system_prompt(),
        "chat (compact)": neoh_persona.build_system_prompt(compact=True),
        "chat (brokerage)": neoh_persona.build_system_prompt(brokerage={"name": "Acme"}),
        "realtime voice": qwen_omni_realtime._system_instructions(),
        "elevenlabs outbound": elevenlabs_realtime.outbound_instructions(
            qwen_omni_realtime._system_instructions(), {"briefing": "123 Main St"})[0],
        "phone receptionist": ai_chat_agent._VOICE_PROMPT,
        "qwen voice agent": qwen_voice_agent.QwenVoiceAgent._build_system_prompt(None),
    }
    for mode in ("buyer", "seller", "auto"):
        prompts[f"inbound {mode}"] = inbound_voice.build_inbound_intake_instructions(
            {"intake_mode": mode, "forward_available": True})
    for name, prompt in prompts.items():
        assert PAYMENT_CARD_RULE in prompt, name
    assert "{text}" in ai_chat_agent._VOICE_PROMPT and ai_chat_agent._VOICE_PROMPT.format(text="x")


def test_the_provisioned_elevenlabs_agent_uses_the_same_instructions():
    src = (BACKEND.parent / "scripts" / "elevenlabs-agent.py").read_text(encoding="utf-8")
    assert '"prompt": {"prompt": _system_instructions()' in src


# ── coverage guard: the write paths keep calling the redactor ──────────────

WRITE_PATHS = {
    ("client_note_crypto", "seal_body"): "every client note (agent, AI chat tool, AI call)",
    ("commands_api", "deliver_call_notes"): "AI call notes + CALL_NOTES frame",
    ("commands_api", "call_telemetry"): "negotiation_events.transcript_excerpt",
    ("commands_api", "stage_command"): "outreach drafts (email/SMS/call) before approval",
    ("commands_api", "edit_command"): "edited outreach drafts",
    ("voice_intel", "ingest_voice_telemetry"): "negotiation_events.transcript_excerpt + frame",
    ("voice_intel", "_process"): "voice-note transcript → interaction_logs",
    ("messaging_data", "record_inbound_message"): "sms_messages.body (inbound)",
    ("messaging_data", "record_outbound_message"): "sms_messages.body (outbound)",
    ("crm", "send_message"): "interaction_logs payload + email_outbox.body_text",
    ("intelligence_os_api", "capture_events"): "interaction_logs payload (perception)",
    ("client_portal", "_clean_portal_payload"): "interaction_logs payload (portal)",
    ("ai_chat_store", "create_chat_request"): "ai_chat_messages (user)",
    ("ai_chat_store", "update_assistant"): "ai_chat_messages (assistant)",
    ("ai_chat_store", "_execute_safe_tool"): "AI chat add_client_note",
    ("memory_core.session_manager", "SessionManager.record_interaction"): "user_interactions",
    ("inbound_voice", "_clean_transcript"): "inbound_voice_calls transcript + answers",
    ("contacts_api", "create_intake"): "contact_intake_sessions",
    ("elevenlabs_realtime", "PlivoElevenLabsBridge._on_tool_call"): "save_note on AI calls",
    ("ai_chat_agent", "_generate_voice_reply"): "caller speech sent to the model provider",
    ("outreach_compliance", "ConsentLedger.suppress"): "outreach_suppression.source_text",
}


@pytest.mark.parametrize("module_name, qualname", sorted(WRITE_PATHS))
def test_each_write_path_still_applies_the_redactor(module_name, qualname):
    import importlib

    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    src = inspect.getsource(obj)
    assert any(name in src for name in ("pan_redaction.", "_scrub_pan(", "_redact_pan(", "redact(")), \
        f"{module_name}.{qualname} ({WRITE_PATHS[(module_name, qualname)]}) stopped removing card numbers"
