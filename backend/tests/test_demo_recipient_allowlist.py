"""The ONE exception to recovery mode: a named demo recipient on staging.

Staging runs with ORACLE_RECOVERY_MODE=1 so nothing it does can reach a
customer. The sales demo still has to show a real text/call arriving on the
operator's own phone, so `ORACLE_DEMO_RECIPIENT_ALLOWLIST` lets exactly those
numbers through — and nothing else. These tests pin every edge of that hole:

* allowlisted + staging + send/call            -> passes the guard
* any other number, a prefix, a near miss      -> blocked
* any other action (abort, buy, email, ...)    -> blocked, even to the number
* ORACLE_ENV unset / prod / unrecognised       -> blocked
* production boot with the variable set        -> refuses to start
* the outreach compliance gate (consent, quiet hours, AI disclosure) still
  runs before any provider submission, allowlisted or not.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import recovery_mode
from recovery_mode import RecoveryModeBlocked

DEMO = "+13024078981"
OTHER = "+13025550123"
BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture
def staging_demo(monkeypatch):
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    monkeypatch.setenv("ORACLE_ENV", "staging")
    monkeypatch.setenv(recovery_mode.DEMO_ALLOWLIST_ENV_VAR, DEMO)
    for name in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID",
                 "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "TWILIO_API_SECRET",
                 "TWILIO_SMS_FROM_NUMBER", "TWILIO_FROM_NUMBER", "TELNYX_API_KEY",
                 "ORACLE_PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


# ── the guard itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["send_message", "place_call"])
def test_allowlisted_destination_passes_on_staging(staging_demo, kind):
    recovery_mode.guard("demo", kind=kind, destination=DEMO)  # must not raise


def test_surrounding_whitespace_is_not_a_different_number(staging_demo):
    recovery_mode.guard("demo", kind="place_call", destination=f"  {DEMO} ")


@pytest.mark.parametrize("destination", [
    OTHER,
    DEMO + "0",          # a longer number that starts with the demo number
    DEMO[:-1],           # a prefix of it
    "13024078981",       # not E.164
    "",
    None,
])
def test_anything_but_the_exact_number_is_blocked(staging_demo, destination):
    with pytest.raises(RecoveryModeBlocked):
        recovery_mode.guard("demo", kind="send_message", destination=destination)


@pytest.mark.parametrize("kind", [
    None, "abort_call", "transfer_call", "provision_forwarding_number",
    "configure_number_webhook", "send_email", "create_brand",
])
def test_only_send_and_call_can_use_the_exception(staging_demo, kind):
    with pytest.raises(RecoveryModeBlocked):
        recovery_mode.guard("demo", kind=kind, destination=DEMO)


@pytest.mark.parametrize("env", [None, "", "prod", "production", "PROD", "ci", "stagin"])
def test_production_or_unknown_environments_never_honour_it(monkeypatch, env):
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    monkeypatch.setenv(recovery_mode.DEMO_ALLOWLIST_ENV_VAR, DEMO)
    if env is None:
        monkeypatch.delenv("ORACLE_ENV", raising=False)
    else:
        monkeypatch.setenv("ORACLE_ENV", env)
    with pytest.raises(RecoveryModeBlocked):
        recovery_mode.guard("demo", kind="place_call", destination=DEMO)


def test_unset_allowlist_blocks_everything(monkeypatch):
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    monkeypatch.setenv("ORACLE_ENV", "staging")
    monkeypatch.delenv(recovery_mode.DEMO_ALLOWLIST_ENV_VAR, raising=False)
    with pytest.raises(RecoveryModeBlocked):
        recovery_mode.guard("demo", kind="place_call", destination=DEMO)


def test_malformed_entries_are_dropped_not_repaired(monkeypatch):
    monkeypatch.setenv(
        recovery_mode.DEMO_ALLOWLIST_ENV_VAR,
        f"3024078981, +1 302 555 0123,{DEMO},+0123456789,+1302abc",
    )
    assert recovery_mode.demo_recipient_allowlist() == frozenset({DEMO})


def test_describe_reports_a_count_never_the_numbers(staging_demo):
    state = recovery_mode.describe()
    assert state["demo_recipient_allowlist_count"] == 1
    assert DEMO not in repr(state)


# ── it is wired into the real provider egress methods ───────────────────────

def test_plivo_call_to_the_demo_number_gets_past_the_guard(staging_demo):
    """Past the guard, the next thing that stops it is the (absent) credential
    — proof the recovery-mode check let it through, with no network call."""
    from command_providers import ProviderConfigurationError
    from voice_provider import PlivoVoiceProvider

    with pytest.raises(ProviderConfigurationError):
        asyncio.run(PlivoVoiceProvider().place_call(
            to_number=DEMO, from_number="+13475550100",
            answer_url="https://example.invalid/answer",
        ))


def test_plivo_call_to_anyone_else_is_still_refused(staging_demo):
    from voice_provider import PlivoVoiceProvider

    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(PlivoVoiceProvider().place_call(
            to_number=OTHER, from_number="+13475550100",
            answer_url="https://example.invalid/answer",
        ))


def test_plivo_abort_is_still_refused_even_for_a_demo_call(staging_demo):
    from voice_provider import PlivoVoiceProvider

    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(PlivoVoiceProvider().abort_call("call-uuid"))


def test_twilio_sms_to_the_demo_number_gets_past_the_guard(staging_demo):
    from command_providers import ProviderConfigurationError, send_twilio_sms

    with pytest.raises(ProviderConfigurationError):
        asyncio.run(send_twilio_sms({"target": {"phone": DEMO}, "body": "hi"}))
    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(send_twilio_sms({"target": {"phone": OTHER}, "body": "hi"}))


def test_telnyx_sms_respects_the_allowlist(staging_demo):
    from command_providers import ProviderConfigurationError
    from messaging_provider import TelnyxMessagingProvider

    with pytest.raises(ProviderConfigurationError):
        asyncio.run(TelnyxMessagingProvider().send_message(
            to=DEMO, from_="+13475550100", text="hi"))
    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(TelnyxMessagingProvider().send_message(
            to=OTHER, from_="+13475550100", text="hi"))


def test_email_has_no_exception(staging_demo):
    from command_providers import send_smtp_email

    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(send_smtp_email({"target": {"email": "x@example.test"},
                                     "subject": "s", "body": "b"}))


def test_custom_http_calls_have_no_exception(staging_demo):
    from command_providers import place_custom_http_call

    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(place_custom_http_call({"target": {"phone": DEMO}}))


# ── production refuses to boot with it set ──────────────────────────────────

def _validate(env: str, allowlist: str | None) -> subprocess.CompletedProcess:
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("ORACLE_", "STRIPE_", "TWILIO_"))}
    environment.update({
        "PYTHONPATH": str(BACKEND),
        "ORACLE_SKIP_DOTENV": "1",
        "ORACLE_ENV": env,
        "ORACLE_BASE_URL": "https://neoh.example.test",
        "ORACLE_SECRET_KEY": "s3cur3-signing-key-with-plenty-of-entropy-01",
        "ORACLE_ENCRYPTION_MASTER_KEY": "m4st3r-encryption-key-with-entropy-02xyz",
        "ORACLE_JWT_ISSUER": "neoh-test",
        "ORACLE_JWT_AUDIENCE": "neoh-test",
    })
    if allowlist is not None:
        environment[recovery_mode.DEMO_ALLOWLIST_ENV_VAR] = allowlist
    return subprocess.run(
        [sys.executable, "-c", "import config; config.validate_or_die()"],
        cwd=BACKEND, env=environment, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("env", ["prod", "production"])
def test_production_refuses_to_start_with_the_allowlist_set(env):
    result = _validate(env, DEMO)
    assert result.returncode != 0
    assert "ORACLE_DEMO_RECIPIENT_ALLOWLIST" in result.stderr
    assert DEMO not in result.stderr  # the refusal names the setting, not the phone


def test_staging_may_start_with_it():
    result = _validate("staging", DEMO)
    assert "ORACLE_DEMO_RECIPIENT_ALLOWLIST" not in result.stderr


# ── outreach compliance still applies to an allowlisted recipient ───────────

def test_compliance_gate_runs_before_provider_submission_for_sms_and_calls():
    """Recovery mode is checked inside the provider; the TCPA/consent/quiet-
    hours gate is checked in the executor BEFORE any provider is reached. The
    allowlist only affects the former, so a demo number without consent, or
    outside 8am-8pm, is refused before a provider is ever called."""
    import commands_api as ca

    source = inspect.getsource(ca._execute_command_job)
    for branch, channel, submit in (
        ("elif command_type is CommandType.SMS:", "Channel.SMS", "submission_started = True"),
        ("elif command_type is CommandType.CALL:", "Channel.VOICE", "submission_started = True"),
    ):
        start = source.index(branch)
        body = source[start:]
        gate = body.index("guard_outreach(")
        assert channel in body[gate:gate + 200]
        refuse = body.index("if not decision.allowed:")
        assert gate < refuse < body.index(submit)


def test_allowlisted_number_without_consent_or_outside_hours_is_still_blocked():
    from outreach_compliance import Channel, evaluate

    noon_et = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    night_et = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)

    no_consent = evaluate(channel=Channel.SMS, contact=DEMO, state_code="DE",
                          now_utc=noon_et, suppressed=False, has_consent=False)
    assert not no_consent.allowed

    oral_only = evaluate(channel=Channel.VOICE, contact=DEMO, state_code="DE",
                         now_utc=noon_et, suppressed=False, has_consent=True,
                         has_written_consent=False)
    assert not oral_only.allowed  # AI voice needs WRITTEN consent

    at_night = evaluate(channel=Channel.VOICE, contact=DEMO, state_code="DE",
                        now_utc=night_et, suppressed=False, has_consent=True,
                        has_written_consent=True)
    assert not at_night.allowed

    opted_out = evaluate(channel=Channel.SMS, contact=DEMO, state_code="DE",
                         now_utc=noon_et, suppressed=True, has_consent=True)
    assert not opted_out.allowed

    ok = evaluate(channel=Channel.VOICE, contact=DEMO, state_code="DE",
                  now_utc=noon_et, suppressed=False, has_consent=True,
                  has_written_consent=True)
    assert ok.allowed
    assert any("AI" in d or "artificial" in d.lower() for d in ok.required_disclosures)
