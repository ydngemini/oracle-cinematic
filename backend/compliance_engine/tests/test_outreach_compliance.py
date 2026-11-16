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
    BIOMETRIC_VOICE_DISCLOSURE,
    Channel,
    ConsentRecord,
    VoiceMode,
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


def test_human_agent_voice_accepts_general_consent_without_ai_disclosure():
    d = _voice(
        has_consent=True,
        has_written_consent=False,
        voice_mode=VoiceMode.AGENT,
        recording_enabled=False,
    )
    assert d.allowed
    assert d.required_disclosures == ()


def test_human_agent_voice_still_fails_closed_without_consent_basis():
    d = _voice(
        has_consent=False,
        has_written_consent=False,
        voice_mode=VoiceMode.AGENT,
        recording_enabled=False,
    )
    assert not d.allowed
    assert any("agent voice" in b for b in d.blockers)


def test_unrecorded_human_agent_call_does_not_require_voiceprint_consent():
    d = _voice(
        state_code="IL",
        has_consent=True,
        has_written_consent=False,
        has_voiceprint_consent=False,
        voice_mode=VoiceMode.AGENT,
        recording_enabled=False,
    )
    assert d.allowed
    assert not any("voiceprint" in blocker for blocker in d.blockers)


def test_recorded_human_agent_call_still_owes_the_biometric_disclosure():
    # BIPA attaches to the recording, not to who is speaking: a human agent
    # recording an IL recipient must still deliver the consent ask, even though
    # no AI-identification line is owed.
    d = _voice(
        state_code="IL",
        has_consent=True,
        has_written_consent=False,
        has_voiceprint_consent=True,
        voice_mode=VoiceMode.AGENT,
        recording_enabled=True,
    )
    assert d.allowed
    assert AI_VOICE_DISCLOSURE not in d.required_disclosures
    assert BIOMETRIC_VOICE_DISCLOSURE.format(state="IL") in d.required_disclosures


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


# Audit 25.3: real-world opt-out replies that the whole-body keyword match
# missed, so no suppression was recorded and later texts could be approved.
OPT_OUTS = [
    "REVOKE", "Revoke.", "STOP please", "stop pls", "Stop texting me", "stop sending me these",
    "Please take me off your list", "remove me from your list", "Unsubscribe me", "opt out",
    "Opt me out", "Don’t text me again", "do not contact me", "No more texts", "I said STOP!",
    "revoke my consent", "not interested. stop",
    # independent review of PR #78
    "STOP STOP", "stop it now", "lol stop", "cancel all", "leave me alone", "dont contact me",
    "never message me again", "Delete my number", "Wrong number", "unsub", "ALTO", "BAJA",
    "STOP\nwho is this", "UNSUBSCRIBE ME NOW",
    # re-review of PR #78
    "Remove my number", "please remove my number", "I want to opt-out", "opt-out my number",
    "Stop - not selling", "Lose this number", "stop txt", "not selling so stop",
]
NOT_OPT_OUTS = [
    "Please stop by the house on Tuesday", "Stop by at 5", "End of the day works",
    "I want to cancel my listing with my agent", "yes please call me", "Can you send the comps?",
    "We could end up selling in the spring", "Remove the old fridge before showing?",
    "Can you remove measurements from the listing?", "I quit my job last week",
    "cancel the showing tomorrow", "the end of the month is fine",
]


@pytest.mark.parametrize("text", OPT_OUTS)
def test_plain_language_and_revoke_replies_are_opt_outs(text):
    assert is_stop_keyword(text), text


@pytest.mark.parametrize("text", NOT_OPT_OUTS)
def test_ordinary_replies_using_keyword_words_are_not_opt_outs(text):
    assert not is_stop_keyword(text), text


def test_a_line_break_ends_a_clause():
    """'STOP' on its own line, then a question, is still STOP (mutant pin)."""
    assert is_stop_keyword("STOP\nwho is this")
    assert not is_stop_keyword("STOP who is this")


@pytest.mark.parametrize("speech, expected", [
    ("My lender said stop sending documents", False),
    ("There will be no more calls from the bank once we close", False),
    ("Don't text my wife, text me", False),
    ("I'll withdraw consent for the inspection if they push", False),
    ("Please stop calling me", True),
    ("I'm not interested, stop.", True),
])
def test_spoken_opt_out_rule_is_unchanged(speech, expected):
    """A voice opt-out is permanent and discards the caller's answers, so the
    SMS broadening must not leak into speech (review of PR #78)."""
    import inbound_voice

    assert inbound_voice._requested_opt_out([{"role": "caller", "text": speech}]) is expected


# ── 26.7: a logged voice attempt is one atomic claim ─────────────────────────
class _ClaimConn:
    recent_voice = 2

    def __init__(self, log):
        self.log = log
        self.args = {}

    async def execute(self, sql, *args):
        kind = ("lock" if "pg_advisory_xact_lock" in sql else
                "insert" if "INSERT INTO outreach_attempt_log" in sql else sql)
        self.log.append(kind)
        self.args[kind] = args

    async def fetchrow(self, sql, *args):
        self.log.append("count")
        self.args["count"] = args
        return {"suppressed": False, "has_consent": True, "has_written_consent": True,
                "vp_consent": False, "recent_voice": self.recent_voice}


def _claim_env(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    import outreach_compliance
    from tenancy import Role, TenantContext

    events: list = []

    @asynccontextmanager
    async def tx(_ctx):
        events.append("begin")
        conn = _ClaimConn(events)
        events.append(conn)
        yield conn
        events.append("commit")

    monkeypatch.setattr(outreach_compliance, "tenant_tx", tx)
    ctx = TenantContext(agent_id="a@x.test", tenant_id="00000000-0000-0000-0000-0000000000aa",
                        role=Role.AGENT)
    return asyncio, outreach_compliance, ctx, events


def _steps(events):
    return [e for e in events if isinstance(e, str)]


def _conn(events):
    return next(e for e in events if isinstance(e, _ClaimConn))


def test_a_logged_voice_attempt_counts_and_claims_under_one_lock(monkeypatch):
    """Count, decide and record in ONE transaction behind the per-contact lock,
    so two callers cannot both read 2/3 and both dial (external audit 26.7)."""
    asyncio, oc, ctx, events = _claim_env(monkeypatch)

    class CallerConn:  # a caller's open transaction must not host the claim
        async def fetchrow(self, *a):
            raise AssertionError("the claim must not run on the caller's connection")

    decision = asyncio.run(oc.guard_outreach(
        ctx, channel=Channel.VOICE, contact="+13025550142", state_code="DE",
        now_utc=NOON_ISH_UTC, voice_mode=VoiceMode.AGENT, recording_enabled=False,
        conn=CallerConn()))
    assert decision.allowed
    assert _steps(events) == ["begin", "lock", "count", "insert", "commit"]


def test_the_claim_locks_on_the_normalised_number_and_reads_this_brokerage(monkeypatch):
    """Two formats of one number must contend for ONE lock, and the gate read is
    filtered to the caller's brokerage (review of the 26.7/26.8 fix)."""
    asyncio, oc, ctx, events = _claim_env(monkeypatch)
    asyncio.run(oc.guard_outreach(
        ctx, channel=Channel.VOICE, contact="(302) 555-0142", state_code="DE",
        now_utc=NOON_ISH_UTC, voice_mode=VoiceMode.AGENT, recording_enabled=False))
    conn = _conn(events)
    assert conn.args["lock"] == (f"outreach-voice:{ctx.tenant_id}:+13025550142",)
    assert conn.args["count"][-1] == ctx.tenant_id


@pytest.mark.parametrize("pending_stop", [False, True])
def test_a_denied_voice_claim_is_recorded_with_its_reason(monkeypatch, pending_stop):
    """A full cap, or a spoken STOP still being written (23.5), denies on the
    claim path and the denial row is written in the same transaction."""
    asyncio, oc, ctx, events = _claim_env(monkeypatch)
    import voice_opt_out

    monkeypatch.setattr(_ClaimConn, "recent_voice", 2 if pending_stop else 3)
    monkeypatch.setattr(voice_opt_out, "pending_opt_out", lambda *_a: pending_stop)
    decision = asyncio.run(oc.guard_outreach(
        ctx, channel=Channel.VOICE, contact="+13025550142", state_code="DE",
        now_utc=NOON_ISH_UTC, voice_mode=VoiceMode.AGENT, recording_enabled=False))
    assert not decision.allowed
    assert _steps(events) == ["begin", "lock", "count", "insert", "commit"]
    *_, allowed, reason = _conn(events).args["insert"]
    assert allowed is False
    assert reason == decision.blockers[0]
    assert ("frequency cap" in reason) is not pending_stop


def test_an_unlogged_voice_check_takes_no_lock(monkeypatch):
    asyncio, oc, ctx, events = _claim_env(monkeypatch)
    asyncio.run(oc.guard_outreach(
        ctx, channel=Channel.VOICE, contact="+13025550142", state_code="DE",
        now_utc=NOON_ISH_UTC, voice_mode=VoiceMode.AGENT, recording_enabled=False, log=False))
    assert _steps(events) == ["begin", "count", "commit"]


# ── 26.8: a voiceprint release is a voice-channel record ─────────────────────
@pytest.mark.parametrize("channel, contact", [
    (Channel.SMS, "+13025550142"), (Channel.EMAIL, "a@b.test"), (Channel.VOICE, "a@b.test")])
def test_a_voiceprint_release_is_refused_off_the_voice_channel(monkeypatch, channel, contact):
    import asyncio

    from fastapi import HTTPException

    import outreach_compliance

    async def never(*a, **k):
        raise AssertionError("nothing may be stored")

    monkeypatch.setattr(outreach_compliance.ConsentLedger, "record_consent", never)
    body = ConsentRecord(contact=contact, channel=channel, consent_type="biometric_voiceprint")
    with pytest.raises(HTTPException) as err:
        asyncio.run(outreach_compliance.record_consent(body, ctx=None))
    assert err.value.status_code == 422
