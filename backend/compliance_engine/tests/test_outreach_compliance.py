"""
Unit tests for the outreach compliance gate (backend/outreach_compliance.py).

Covers the pure decision logic only — no DB. The persisted facts (suppression,
consent, frequency) are passed in directly, exactly as guard_outreach would after
querying the 0015 tables.

Run with:  python -m pytest backend/compliance_engine/tests/test_outreach_compliance.py -v
       or:  python backend/compliance_engine/tests/test_outreach_compliance.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from outreach_compliance import (  # noqa: E402
    AI_VOICE_DISCLOSURE,
    Channel,
    ConsentRecord,
    evaluate,
    is_stop_keyword,
    normalize_contact,
    within_calling_window,
)


def test_consent_record_rejects_malformed_optional_uuids():
    with pytest.raises(ValidationError):
        ConsentRecord(
            contact="+13055550142",
            channel=Channel.VOICE,
            client_id="null",
        )

# 2026-06-17 17:00 UTC → 13:00 EDT (NY) / 10:00 PDT (LA): inside the 8am-8pm window.
NOON_ISH_UTC = datetime(2026, 6, 17, 17, 0, tzinfo=timezone.utc)
# 2026-06-17 03:00 UTC → 23:00 EDT the prior evening in NY: outside the window.
LATE_NIGHT_UTC = datetime(2026, 6, 17, 3, 0, tzinfo=timezone.utc)


# ── normalisation ────────────────────────────────────────────────────────────
def test_normalize_phone_to_e164():
    assert normalize_contact("(305) 555-0142", Channel.VOICE) == "+13055550142"
    assert normalize_contact("305-555-0142", Channel.SMS) == "+13055550142"
    assert normalize_contact("13055550142", Channel.VOICE) == "+13055550142"


def test_normalize_email_lowercased():
    assert normalize_contact("Owner@Example.COM", Channel.EMAIL) == "owner@example.com"


def test_stop_keyword_detection():
    for kw in ("STOP", "stop", " Unsubscribe ", "CANCEL", "opt-out"):
        assert is_stop_keyword(kw), kw
    assert not is_stop_keyword("yes please call me")


# ── calling window ───────────────────────────────────────────────────────────
def test_window_inside():
    ok, _ = within_calling_window("FL", NOON_ISH_UTC)
    assert ok


def test_window_outside():
    ok, _ = within_calling_window("FL", LATE_NIGHT_UTC)
    assert not ok


def test_window_unknown_state_fails_closed():
    ok, clock = within_calling_window(None, NOON_ISH_UTC)
    assert not ok and clock is None


# ── evaluate: voice ──────────────────────────────────────────────────────────
def _voice(**over):
    base = dict(
        channel=Channel.VOICE, contact="+13055550142", state_code="FL",
        now_utc=NOON_ISH_UTC, suppressed=False, has_consent=True,
        has_written_consent=True,
        has_voiceprint_consent=True, recent_voice_attempts=0,
    )
    base.update(over)
    return evaluate(**base)


def test_voice_allowed_with_consent_in_window():
    d = _voice()
    assert d.allowed
    assert AI_VOICE_DISCLOSURE in d.required_disclosures


def test_voice_blocked_without_written_consent():
    # FCC 24-17: oral / prior-business consent (has_consent=True) does NOT qualify
    # for an AI artificial-voice call — only express WRITTEN consent does.
    d = _voice(has_consent=True, has_written_consent=False)
    assert not d.allowed
    assert any("written" in b.lower() for b in d.blockers)


def test_voice_blocked_when_suppressed_even_with_consent():
    d = _voice(suppressed=True)
    assert not d.allowed
    assert any("do-not-contact" in b or "opt-out" in b for b in d.blockers)


def test_voice_blocked_outside_window():
    d = _voice(now_utc=LATE_NIGHT_UTC)
    assert not d.allowed
    assert any("calling window" in b for b in d.blockers)


def test_voice_frequency_cap():
    d = _voice(recent_voice_attempts=3)
    assert not d.allowed
    assert any("frequency cap" in b for b in d.blockers)


def test_il_voiceprint_block_and_disclosure():
    d = _voice(state_code="IL", has_voiceprint_consent=False)
    assert not d.allowed
    assert any("voiceprint" in b for b in d.blockers)
    # With voiceprint consent, IL is allowed and carries the biometric disclosure.
    d2 = _voice(state_code="IL", has_voiceprint_consent=True)
    assert d2.allowed
    assert len(d2.required_disclosures) == 2  # AI disclosure + biometric ask


def test_mini_tcpa_warning_present():
    d = _voice(state_code="OK")
    assert any("mini-TCPA" in w for w in d.warnings)


# ── evaluate: email ──────────────────────────────────────────────────────────
def test_email_allowed_without_consent():
    d = evaluate(
        channel=Channel.EMAIL, contact="owner@example.com", state_code=None,
        now_utc=LATE_NIGHT_UTC, suppressed=False, has_consent=False,
    )
    assert d.allowed  # email needs no TCPA consent / window
    assert d.required_disclosures == ()


def test_email_blocked_when_suppressed():
    d = evaluate(
        channel=Channel.EMAIL, contact="owner@example.com", state_code=None,
        now_utc=NOON_ISH_UTC, suppressed=True, has_consent=True,
    )
    assert not d.allowed


# ── no-pytest fallback runner ────────────────────────────────────────────────
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
