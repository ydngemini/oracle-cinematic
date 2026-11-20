"""A refusal made before any network I/O is a definite "not sent" (PR #147 review, L09-16).

The command worker sets `submission_started` immediately before each provider
call (PR #147), and `_provider_submission_is_uncertain` files any exception
after it as reconciliation_required ("may have been sent") unless the
exception says nothing was sent. The send adapters refused several things
before any request (recovery mode, a target that is not E.164, an email with no
subject) with types that said "uncertain", so:

- an invalid approved target became reconciliation_required instead of failed;
- a recovery-mode refusal came back as a success-shaped result, the job
  "succeeded", and the worker's RECOVERY_MODE_BLOCKED dead-letter clause never
  ran.

These tests pin:
(a) every pre-network refusal in the send adapters (SMTP, Twilio SMS and call,
    custom call, Google Calendar, Telnyx, Plivo) is certain and terminal for
    the job, and never reaches the network;
(b) a failure after the request may have reached the provider stays uncertain,
    and so does anything that fails after the provider call returned;
(c) recovery mode never produces a success-shaped outcome for a send: the
    command is recorded 'failed' and the job system dead-letters it.

No database and no provider: tenant_tx is an in-memory fake and every network
boundary is a tripwire or a fake.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import command_providers
import commands_api as ca
import recovery_mode
from command_providers import (
    ProviderConfigurationError,
    ProviderRejectedError,
    ProviderRequestError,
    ProviderResult,
)

T = "11111111-1111-1111-1111-111111111111"
COMMAND = "22222222-2222-2222-2222-222222222222"
AGENT = "agent@brokerage.test"
TO = "+13025550142"
CALLER_ID = "+13025550100"
EVENT = {"summary": "Showing", "start": {"dateTime": "2026-12-01T10:00:00Z"},
         "end": {"dateTime": "2026-12-01T10:30:00Z"}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        recovery_mode.ENV_VAR, recovery_mode.DEMO_ALLOWLIST_ENV_VAR,
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "TWILIO_API_SECRET",
        "TWILIO_FROM_NUMBER", "TWILIO_SMS_FROM_NUMBER", "TWILIO_SMS_SENDER_TYPE",
        "ORACLE_TWILIO_ACCOUNT_TIER", "ORACLE_TWILIO_TWIML_URL", "ORACLE_CUSTOM_CALL_API_URL",
        "ORACLE_EMAIL_PROVIDER", "GOOGLE_CALENDAR_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://neoh.test")
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_WEBHOOK_SECRET", "custom-call-secret-for-tests")

    import provider_health

    async def no_health(*_a, **_k):
        return None

    # Provider health writes go to the database; nothing here has one.
    monkeypatch.setattr(provider_health, "record", no_health)


# ── network boundaries ───────────────────────────────────────────────────────


class Network:
    """Every provider boundary. `reached` lists each one touched; a fake
    client stands in only where a test passes one."""

    def __init__(self, monkeypatch, *, twilio=None, plivo=None, telnyx=None, smtp=None, urlopen=None):
        self.reached: list[str] = []
        import smtp_mailer
        import messaging_provider
        import voice_provider

        def tripwire(name, fake=None):
            def boundary(*args, **kwargs):
                self.reached.append(name)
                if fake is None:
                    raise AssertionError(f"{name} reached for a refusal that needs no network")
                return fake(*args, **kwargs) if callable(fake) else fake
            return boundary

        monkeypatch.setattr(smtp_mailer, "send", tripwire("smtp", smtp))
        monkeypatch.setattr(command_providers, "_twilio_client", tripwire("twilio", twilio))
        monkeypatch.setattr(urllib.request, "urlopen", tripwire("https", urlopen))
        monkeypatch.setattr(voice_provider.PlivoVoiceProvider, "_client",
                            classmethod(lambda cls, creds: tripwire("plivo", plivo)()))
        monkeypatch.setattr(messaging_provider.TelnyxMessagingProvider, "_client",
                            classmethod(lambda cls, creds, **_k: tripwire("telnyx", telnyx)()))


def _twilio_env(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", CALLER_ID)
    monkeypatch.setenv("TWILIO_SMS_FROM_NUMBER", CALLER_ID)
    monkeypatch.setenv("TWILIO_SMS_SENDER_TYPE", "twilio_registered")


def _raise(exc):
    def boom(*_a, **_k):
        raise exc
    return boom


def _outcome(coro):
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001 - the exception IS the outcome under test
        return exc
    raise AssertionError("the adapter returned instead of refusing")


# ── adapter calls, by rail ───────────────────────────────────────────────────


def _smtp(email="a@b.test", subject="Hello", body="Body"):
    return command_providers.send_smtp_email({"target": {"email": email}, "subject": subject, "body": body})


def _twilio_sms(target=TO, body="hi"):
    return command_providers.send_twilio_sms({"target": {"phone": target}, "body": body})


def _twilio_call(target=TO):
    return command_providers.place_twilio_call({"target": {"phone": target}})


def _custom_call(target=TO):
    return command_providers.place_custom_http_call({"target": {"phone": target}})


def _calendar(event=EVENT):
    return command_providers.create_google_calendar_event({"event": dict(event)}, access_token="token")


def _plivo(target=TO):
    import voice_provider

    return voice_provider.PlivoVoiceProvider().place_call(
        to_number=target, from_number=CALLER_ID, answer_url="https://neoh.test/answer",
        credentials={"auth_id": "MA" + "a" * 18, "auth_token": "b" * 40})


def _telnyx(target=TO):
    import messaging_provider

    return messaging_provider.TelnyxMessagingProvider().send_message(
        to=target, from_=CALLER_ID, text="hi", credentials={"api_key": "KEY_test"})


def _twilio_voice_provider(target=TO):
    import voice_provider

    return voice_provider.TwilioVoiceProvider().place_call(
        to_number=target, from_number=CALLER_ID, answer_url="https://neoh.test/answer")


def _twilio_messaging_provider(target=TO):
    import messaging_provider

    return messaging_provider.TwilioMessagingProvider().send_message(to=target, from_=CALLER_ID, text="hi")


SENDS = {
    "smtp": _smtp, "twilio_sms": _twilio_sms, "twilio_call": _twilio_call, "custom_call": _custom_call,
    "calendar": _calendar, "plivo": _plivo, "telnyx": _telnyx,
    "twilio_voice_provider": _twilio_voice_provider, "twilio_messaging_provider": _twilio_messaging_provider,
}


# ── (a) every pre-network refusal is certain ─────────────────────────────────


@pytest.mark.parametrize("rail", sorted(SENDS))
def test_a_recovery_mode_refusal_is_a_definite_not_sent(monkeypatch, rail):
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    _twilio_env(monkeypatch)
    net = Network(monkeypatch)

    exc = _outcome(SENDS[rail]())

    # Still a RecoveryModeBlocked, so the job worker dead-letters it (OUT-1)...
    assert isinstance(exc, recovery_mode.RecoveryModeBlocked), repr(exc)
    # ...and certain, so the command is 'failed', never reconciliation_required.
    assert not ca._provider_submission_is_uncertain(True, exc)
    # Never caught by a handler that reconciles provider errors.
    assert not isinstance(exc, ProviderRequestError)
    assert net.reached == []


INVALID = {
    "smtp, address without @": lambda: _smtp(email="not-an-address"),
    "smtp, no subject": lambda: _smtp(subject=""),
    "smtp, no body": lambda: _smtp(body="  "),
    "twilio_sms, not E.164": lambda: _twilio_sms(target="3025550142"),
    "twilio_sms, empty body": lambda: _twilio_sms(body=""),
    "twilio_sms, body too long": lambda: _twilio_sms(body="x" * 1601),
    "twilio_call, not E.164": lambda: _twilio_call(target="3025550142"),
    "custom_call, not E.164": lambda: _custom_call(target="3025550142"),
    "calendar, no summary": lambda: _calendar(event={**EVENT, "summary": ""}),
    "plivo, not E.164": lambda: _plivo(target="3025550142"),
    "telnyx, not E.164": lambda: _telnyx(target="3025550142"),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_approved_send_is_refused_before_the_network_and_is_certain(monkeypatch, case):
    _twilio_env(monkeypatch)
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "https://calls.example.test/place")
    net = Network(monkeypatch)

    exc = _outcome(INVALID[case]())

    assert not ca._provider_submission_is_uncertain(True, exc), repr(exc)
    # Terminal: the same approved payload is refused the same way every time.
    assert getattr(exc, "job_terminal", False) is True
    assert str(exc).startswith(("Not sent:", "Not placed:", "Not created:")), str(exc)
    # Still a ProviderRequestError for the callers that already catch one.
    assert isinstance(exc, ProviderRequestError)
    assert net.reached == []


def test_a_non_https_provider_url_is_a_configuration_error_not_an_uncertain_send(monkeypatch):
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "http://calls.example.test/place")
    net = Network(monkeypatch)

    exc = _outcome(_custom_call())

    assert isinstance(exc, ProviderConfigurationError), repr(exc)
    assert not ca._provider_submission_is_uncertain(True, exc)
    assert net.reached == []


# ── (b) after the request may have reached the provider: still uncertain ─────


def _twilio_failing(status):
    from twilio.base.exceptions import TwilioRestException

    failure = TwilioRestException(status, "https://api.twilio.com/x", "unavailable")
    return SimpleNamespace(messages=SimpleNamespace(create=_raise(failure)),
                           calls=SimpleNamespace(create=_raise(failure)))


def _plivo_failing():
    from plivo.exceptions import PlivoServerError

    return SimpleNamespace(calls=SimpleNamespace(create=_raise(PlivoServerError("HTTP 503"))))


def _uncertain_smtp(monkeypatch):
    import smtp_mailer

    Network(monkeypatch, smtp=_raise(smtp_mailer.SmtpUncertainError("connection lost after DATA")))
    return _smtp()


def _uncertain_twilio_sms(monkeypatch):
    _twilio_env(monkeypatch)
    Network(monkeypatch, twilio=_twilio_failing(503))
    return _twilio_sms()


def _uncertain_twilio_call(monkeypatch):
    _twilio_env(monkeypatch)
    Network(monkeypatch, twilio=_twilio_failing(500))
    return _twilio_call()


def _uncertain_custom_call(monkeypatch):
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "https://calls.example.test/place")
    Network(monkeypatch, urlopen=_raise(urllib.error.URLError("timed out")))
    return _custom_call()


def _uncertain_calendar(monkeypatch):
    Network(monkeypatch, urlopen=_raise(TimeoutError("read timed out")))
    return _calendar()


def _uncertain_plivo(monkeypatch):
    Network(monkeypatch, plivo=_plivo_failing())
    return _plivo()


def _uncertain_telnyx(monkeypatch):
    Network(monkeypatch, telnyx=SimpleNamespace(messages=SimpleNamespace(
        send_long_code=_raise(TimeoutError("read timed out")))))
    return _telnyx()


UNCERTAIN = {
    "smtp, connection lost after DATA": _uncertain_smtp,
    "twilio_sms, HTTP 503": _uncertain_twilio_sms,
    "twilio_call, HTTP 500": _uncertain_twilio_call,
    "custom_call, timed out": _uncertain_custom_call,
    "calendar, read timed out": _uncertain_calendar,
    "plivo, HTTP 503": _uncertain_plivo,
    "telnyx, read timed out": _uncertain_telnyx,
}


@pytest.mark.parametrize("case", sorted(UNCERTAIN))
def test_a_failure_after_the_request_may_have_reached_the_provider_stays_uncertain(monkeypatch, case):
    exc = _outcome(UNCERTAIN[case](monkeypatch))

    assert ca._provider_submission_is_uncertain(True, exc), repr(exc)
    assert not getattr(exc, "job_terminal", False)


def test_once_the_provider_call_returned_nothing_makes_it_not_sent():
    """A state write that fails after the provider accepted the send, then a
    refused or misconfigured hang-up, must not turn a placed call into 'failed'."""
    for after_the_send in (
        command_providers.ProviderRefusedBeforeSend("Not placed: x"),
        command_providers.RecoveryModeRefusedBeforeSend("abort a call"),
        ProviderConfigurationError("x"),
        ProviderRejectedError("x"),
        RuntimeError("state write failed"),
    ):
        assert ca._provider_submission_is_uncertain(True, after_the_send, provider_returned=True)
    # Nothing started: certain whatever the error.
    assert not ca._provider_submission_is_uncertain(False, RuntimeError("x"), provider_returned=False)


# ── the command worker, end to end (in memory) ───────────────────────────────


class Conn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        return {"state": "queued", "approval_id": None, "created_by": AGENT,
                "provider": None, "provider_reference": None}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def failure_state(self):
        """The state the worker's failure branch recorded, if it ran."""
        states = [args[2] for sql, args in self.executed if "SET state=$3" in sql]
        return states[-1] if states else None


class Reporter:
    job = {"tenant_id": T, "created_by": AGENT}

    async def progress(self, *_a, **_k):
        return None


PAYLOADS = {
    "email": {"command_type": "EMAIL", "target": {"email": "a@b.test"},
              "draft": {"subject": "Hello", "body": "Body"}},
    "sms_twilio": {"command_type": "SMS", "target": {"phone": TO}, "draft": {"body": "hi"}},
    "sms_telnyx": {"command_type": "SMS", "target": {"phone": TO}, "draft": {"body": "hi"}},
    "call_plivo": {"command_type": "CALL", "target": {"phone": TO}, "draft": {"reason": "follow up"}},
    "call_twilio": {"command_type": "CALL", "target": {"phone": TO}, "draft": {"reason": "follow up"}},
    "calendar": {"command_type": "CALENDAR", "target": {}, "draft": {"event": dict(EVENT)}},
}


def _worker(monkeypatch, rail):
    """The real _execute_command_job with everything around the provider call
    faked: the in-memory row, an entitled brokerage, an allowed contact, and
    the rail's own setup (sender readiness, verified caller ID, call state)."""
    import billing
    import commercial_email
    import inbound_voice
    import messaging_data
    import plivo_call_handler
    import twilio_call_handler

    conn = Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    async def none(*_a, **_k):
        return None

    async def yes(*_a, **_k):
        return True

    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, blockers=[])

    async def prepare(_tenant, *, recipient, body):
        return SimpleNamespace(body=body, unsubscribe_url=None)

    async def identity(*_a, **_k):
        return {}

    async def ready(*_a, **_k):
        return {"ready": True, "sender": CALLER_ID, "blockers": []}

    async def caller_id(_ctx):
        return CALLER_ID

    async def route(_ctx):
        return {"provider": "plivo" if rail == "call_plivo" else "twilio"}

    async def token(*_a, **_k):
        return "token"

    monkeypatch.setattr(ca, "tenant_tx", tx)
    monkeypatch.setattr(billing, "entitlement_refusal", none)
    monkeypatch.setattr(ca, "_requester_may_contact", yes)
    monkeypatch.setattr(ca, "_destination_still_on_record", none)
    monkeypatch.setattr(ca, "guard_outreach", allowed)
    monkeypatch.setattr(ca, "_load_provider_credential", none)
    monkeypatch.setattr(ca, "load_agent_identity", identity)
    monkeypatch.setattr(ca, "_load_google_access_token", token)
    monkeypatch.setattr(commercial_email, "prepare", prepare)
    monkeypatch.setattr(messaging_data, "messaging_send_readiness", ready)
    monkeypatch.setattr(messaging_data, "record_outbound_message", none)
    monkeypatch.setattr(inbound_voice, "get_verified_caller_id", caller_id)
    monkeypatch.setattr(inbound_voice, "get_telephony_route", route)
    for name in ("ensure_plivo_call_state_available", "initialize_outbound_plivo_call_state",
                 "record_outbound_plivo_reference"):
        monkeypatch.setattr(plivo_call_handler, name, none)
    for name in ("ensure_twilio_call_state_available", "initialize_outbound_twilio_correlation",
                 "initialize_twilio_call_state"):
        monkeypatch.setattr(twilio_call_handler, name, none)
    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "telnyx" if rail == "sms_telnyx" else "twilio")

    def run(**changes):
        payload = {**PAYLOADS[rail], "command_id": COMMAND}
        for key, value in changes.items():
            payload[key] = {**payload[key], **value}
        return ca._execute_command_job(payload, Reporter())

    return conn, run


RAILS = sorted(PAYLOADS)


@pytest.mark.parametrize("rail", RAILS)
def test_recovery_mode_never_reports_a_send_as_submitted(monkeypatch, rail):
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    net = Network(monkeypatch)
    conn, run = _worker(monkeypatch, rail)

    with pytest.raises(recovery_mode.RecoveryModeBlocked):
        asyncio.run(run())     # raised to the job system, never a success-shaped dict

    assert conn.failure_state() == "failed"
    assert net.reached == []


INVALID_COMMANDS = {
    "email": {"target": {"email": "not-an-address"}},
    "sms_twilio": {"target": {"phone": "3025550142"}},
    "sms_telnyx": {"target": {"phone": "3025550142"}},
    "call_plivo": {"target": {"phone": "3025550142"}},
    "call_twilio": {"target": {"phone": "3025550142"}},
    "calendar": {"draft": {"event": {**EVENT, "summary": ""}}},
}


@pytest.mark.parametrize("rail", RAILS)
def test_an_invalid_approved_send_is_recorded_failed_and_not_retried(monkeypatch, rail):
    _twilio_env(monkeypatch)
    net = Network(monkeypatch)
    conn, run = _worker(monkeypatch, rail)

    with pytest.raises(ProviderRequestError) as refused:
        asyncio.run(run(**INVALID_COMMANDS[rail]))

    assert conn.failure_state() == "failed"
    assert refused.value.job_terminal is True
    assert net.reached == []


def _uncertain_rail(monkeypatch, rail):
    import smtp_mailer

    _twilio_env(monkeypatch)
    return {
        "email": lambda: Network(monkeypatch, smtp=_raise(smtp_mailer.SmtpUncertainError("lost after DATA"))),
        "sms_twilio": lambda: Network(monkeypatch, twilio=_twilio_failing(503)),
        "sms_telnyx": lambda: Network(monkeypatch, telnyx=SimpleNamespace(messages=SimpleNamespace(
            send_long_code=_raise(TimeoutError("read timed out"))))),
        "call_plivo": lambda: Network(monkeypatch, plivo=_plivo_failing()),
        "call_twilio": lambda: Network(monkeypatch, twilio=_twilio_failing(500)),
        "calendar": lambda: Network(monkeypatch, urlopen=_raise(TimeoutError("read timed out"))),
    }[rail]()


@pytest.mark.parametrize("rail", RAILS)
def test_an_uncertain_send_stays_reconciliation_required(monkeypatch, rail):
    net = _uncertain_rail(monkeypatch, rail)
    conn, run = _worker(monkeypatch, rail)

    result = asyncio.run(run())

    assert result["status"] == "reconciliation_required"
    assert conn.failure_state() == "reconciliation_required"
    assert net.reached   # the request was made


def test_a_demo_call_placed_in_recovery_mode_whose_hang_up_is_refused_stays_uncertain(monkeypatch):
    """Staging runs in recovery mode with a demo allowlist: the call to the
    operator's own phone is placed, then its state write fails and the hang-up
    is refused by recovery mode (abort is never allowlisted). The call exists,
    so this must stay reconciliation_required — which is why only the send
    adapters' own first guard is certain, not every RecoveryModeBlocked."""
    import plivo_call_handler

    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    monkeypatch.setenv("ORACLE_ENV", "staging")
    monkeypatch.setenv(recovery_mode.DEMO_ALLOWLIST_ENV_VAR, TO)
    net = Network(monkeypatch, plivo=SimpleNamespace(calls=SimpleNamespace(
        create=lambda **_k: SimpleNamespace(request_uuid="11111111-2222-3333-4444-555555555555"))))
    conn, run = _worker(monkeypatch, "call_plivo")
    monkeypatch.setattr(plivo_call_handler, "record_outbound_plivo_reference",
                        _async_raise(plivo_call_handler.PlivoCallStateUnavailable("state vanished")))

    result = asyncio.run(run())

    assert result["status"] == "reconciliation_required"
    assert conn.failure_state() == "reconciliation_required"
    assert net.reached == ["plivo"]


def test_a_twilio_call_that_returned_never_falls_back_to_a_second_rail(monkeypatch):
    """The custom call fallback is for a Twilio that could not be asked at all.
    Once Twilio returned a call, a configuration error from a later step must
    not place the same approved call again through the custom API."""
    import twilio_call_handler

    _twilio_env(monkeypatch)
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "https://calls.example.test/place")
    conn, run = _worker(monkeypatch, "call_twilio")
    placed = []

    async def twilio_placed(draft, *, credentials=None, correlation=""):
        placed.append("twilio")
        return ProviderResult("twilio", "CA" + "f" * 32, "queued", {})

    async def custom(draft):
        placed.append("custom")
        return ProviderResult("custom_call", "custom-1", "queued", {})

    monkeypatch.setattr(ca, "place_twilio_call", twilio_placed)
    monkeypatch.setattr(ca, "place_custom_http_call", custom)
    monkeypatch.setattr(ca, "abort_twilio_call", _async_raise(None))
    monkeypatch.setattr(twilio_call_handler, "initialize_twilio_call_state",
                        _async_raise(ProviderConfigurationError("stand-in for a later setup failure")))
    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", _async_raise(None))

    result = asyncio.run(run())

    assert placed == ["twilio"]
    assert result["status"] == "reconciliation_required"
    assert conn.failure_state() == "reconciliation_required"


def _async_raise(exc):
    async def fn(*_a, **_k):
        if exc is not None:
            raise exc
        return None
    return fn


# ── (c) the job system sees a failure, never a completed job ─────────────────


def test_the_job_worker_dead_letters_a_recovery_mode_send_instead_of_completing_it(monkeypatch):
    """Through DurableJobWorkers._loop itself: the command job raises, so
    fail_job(terminal=True, RECOVERY_MODE_BLOCKED) runs and complete_job does not."""
    import automation_jobs

    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    Network(monkeypatch)
    conn, _run = _worker(monkeypatch, "email")
    job = {"id": "job-1", "job_type": "command:execute", "tenant_id": T, "created_by": AGENT,
           "payload": {**PAYLOADS["email"], "command_id": COMMAND}}
    workers = automation_jobs.DurableJobWorkers(worker_count=0, interactive_count=0)
    workers._running = True
    claimed, failed, completed = [], [], []

    async def claim(worker_id, *, queue_name="default"):
        if claimed:
            workers._running = False
            return None
        claimed.append(job)
        return job

    async def fail(job, worker_id, exc, *, error_code=None, terminal=False, **_k):
        failed.append((type(exc), error_code, terminal))

    async def complete(job, worker_id, result):
        completed.append(result)

    async def lease(*_a, **_k):
        await asyncio.sleep(3600)

    class JobReporter:
        def __init__(self, job, worker_id):
            self.job = job

        async def progress(self, *_a, **_k):
            return None

    monkeypatch.setattr(automation_jobs, "claim_next_job", claim)
    monkeypatch.setattr(automation_jobs, "mark_running", _async_raise(None))
    monkeypatch.setattr(automation_jobs, "fail_job", fail)
    monkeypatch.setattr(automation_jobs, "complete_job", complete)
    monkeypatch.setattr(automation_jobs, "_keep_lease", lease)
    monkeypatch.setattr(automation_jobs, "JobReporter", JobReporter)
    monkeypatch.setitem(automation_jobs._HANDLERS, "command:execute", ca._execute_command_job)

    asyncio.run(workers._loop("w-test", poll_seconds=0))

    assert completed == []
    assert len(failed) == 1
    exc_type, error_code, terminal = failed[0]
    assert issubclass(exc_type, recovery_mode.RecoveryModeBlocked)
    assert error_code == "RECOVERY_MODE_BLOCKED" and terminal is True
    assert conn.failure_state() == "failed"


# ── review r1 follow-ups: refusals that still looked uncertain ────────────────

def test_an_email_header_with_a_line_break_is_refused_before_connecting(monkeypatch):
    import smtplib

    import command_providers
    import commands_api
    import smtp_mailer

    monkeypatch.setattr(smtp_mailer, "resolve_settings",
                        lambda credentials: {"sender": "relay@neoh.test", "sender_name": ""})

    def no_connection(*a, **k):
        raise AssertionError("connected to the mail server")

    monkeypatch.setattr(smtplib, "SMTP", no_connection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", no_connection)
    draft = {"target": {"email": "a@b.test"}, "subject": "Hello\nBcc: x@y.test", "body": "hi"}
    with pytest.raises(command_providers.ProviderRefusedBeforeSend) as caught:
        asyncio.run(command_providers.send_smtp_email(draft))
    assert commands_api._provider_submission_is_uncertain(True, caught.value) is False


@pytest.mark.parametrize("brk", ["\r\n", "\n", "\x0b", "\x0c", "\x85", "\u2028"])
def test_a_subject_with_a_line_break_is_refused_at_staging(brk):
    import commands_api

    with pytest.raises(ValueError, match="single line"):
        commands_api._validate_command_payload(
            commands_api.CommandType.EMAIL, {"email": "a@b.test"}, {"subject": f"Hello{brk}Bcc: x@y.test", "body": "hi"})


def test_a_custom_call_url_without_a_scheme_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_API_URL", "calls.example.test/place")
    net = Network(monkeypatch)

    exc = _outcome(_custom_call())

    assert isinstance(exc, ProviderConfigurationError), repr(exc)
    assert not ca._provider_submission_is_uncertain(True, exc)
    assert net.reached == []


def test_a_plivo_auth_id_the_sdk_rejects_is_a_configuration_error(monkeypatch):
    """plivo.RestClient checks the auth id in its constructor, before any
    request; the real _client runs here, with the HTTP layer tripwired."""
    import requests

    import voice_provider

    def no_request(*_a, **_k):
        raise AssertionError("an HTTP request was made")

    monkeypatch.setattr(requests.Session, "request", no_request)
    exc = _outcome(voice_provider.PlivoVoiceProvider().place_call(
        to_number=TO, from_number=CALLER_ID, answer_url="https://neoh.test/answer",
        credentials={"auth_id": "not-an-auth-id", "auth_token": "b" * 40}))

    assert isinstance(exc, ProviderConfigurationError), repr(exc)
    assert not ca._provider_submission_is_uncertain(True, exc)


@pytest.mark.parametrize("module, send", [("plivo.exceptions", _plivo), ("telnyx", _telnyx)],
                         ids=["plivo", "telnyx"])
def test_a_missing_sdk_is_a_configuration_error_not_an_uncertain_send(monkeypatch, module, send):
    import sys

    net = Network(monkeypatch)
    monkeypatch.setitem(sys.modules, module, None)      # the import raises ImportError

    exc = _outcome(send())

    assert isinstance(exc, ProviderConfigurationError), repr(exc)
    assert not ca._provider_submission_is_uncertain(True, exc)
    assert net.reached == []
