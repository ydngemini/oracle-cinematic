"""A Twilio outbound call's status goes through the same durable outcome path
as Plivo's (external audit 24.3).

twilio_status_webhook used to update live_call_sessions and clean up Redis
only: a Twilio call's answer never reached record_call_outcome, so the first
response was never marked delivered, a speed-to-lead call never metered
lead_engaged, and the client's timeline never got the carrier's line. It now
stores the outcome (provider_call_outcomes, 0174) before answering and applies
it through record_call_outcome; a failure is a 503 that Twilio is told to
retry, never a 204 over a lost outcome. The real-PostgreSQL proof is
test_twilio_call_outcome_live.py.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from starlette.datastructures import FormData

import commands_api as ca

SID = "CA" + "e" * 32


class _Request:
    def __init__(self, form: dict[str, str]):
        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": "sig", "host": "internal.invalid"}
        self.query_params: dict[str, str] = {}
        self.url = SimpleNamespace(netloc="internal.invalid", scheme="http", query=urlencode({}))

    async def form(self):
        return self._form


@pytest.fixture()
def hook(monkeypatch):
    monkeypatch.setattr(ca, "_validate_twilio_webhook_signature", lambda *a, **k: None)
    seen = SimpleNamespace(accepted=[], cleaned=[], brokerage="brokerage-1", accept_error=None,
                           cleanup_error=None)

    async def accept(provider, call_sid, status, duration=None, *, tenant_id, aliases=()):
        if seen.accept_error:
            raise seen.accept_error
        seen.accepted.append((provider, call_sid, status, duration, tenant_id))
        return 1

    async def brokerage(_sid, _ref=""):
        return seen.brokerage

    async def cleanup(call_sid):
        if seen.cleanup_error:
            raise seen.cleanup_error
        seen.cleaned.append(call_sid)

    monkeypatch.setattr(ca, "accept_call_outcome", accept)
    monkeypatch.setattr(ca, "_twilio_call_brokerage", brokerage)
    import twilio_call_handler

    monkeypatch.setattr(twilio_call_handler, "cleanup_twilio_call", cleanup)
    return seen


def _post(form):
    return asyncio.run(ca.twilio_status_webhook(_Request(form)))


def test_an_answered_twilio_call_goes_through_the_outcome_path(hook):
    assert _post({"CallSid": SID, "CallStatus": "in-progress"}).status_code == 204
    assert _post({"CallSid": SID, "CallStatus": "completed", "CallDuration": "42"}).status_code == 204
    assert hook.accepted == [("twilio", SID, "in-progress", None, "brokerage-1"),
                             ("twilio", SID, "completed", 42, "brokerage-1")]
    assert hook.cleaned == [SID]          # only after the terminal outcome is stored


@pytest.mark.parametrize("status", ["busy", "no-answer", "failed", "canceled"])
def test_an_unanswered_twilio_call_is_stored_as_its_own_outcome(hook, status):
    assert _post({"CallSid": SID, "CallStatus": status, "CallDuration": "0"}).status_code == 204
    assert hook.accepted == [("twilio", SID, status, 0, "brokerage-1")]
    assert hook.cleaned == [SID]


@pytest.mark.parametrize("status", ["initiated", "ringing", "queued", ""])
def test_statuses_without_an_outcome_store_nothing(hook, status):
    assert _post({"CallSid": SID, "CallStatus": status}).status_code == 204
    assert hook.accepted == [] and hook.cleaned == []


@pytest.mark.parametrize("raw", ["soon", "-5", str(2**31)])
def test_an_unusable_duration_is_unknown_not_zero(hook, raw):
    _post({"CallSid": SID, "CallStatus": "completed", "CallDuration": raw})
    assert hook.accepted[0][3] is None    # unknown is not "not answered"; never a CHECK failure


def test_an_outcome_that_cannot_be_stored_asks_twilio_to_retry(hook):
    hook.accept_error = RuntimeError("database unavailable")
    assert _post({"CallSid": SID, "CallStatus": "completed", "CallDuration": "9"}).status_code == 503
    assert hook.cleaned == []             # the call state stays for the retry


def test_a_call_nobody_can_attribute_yet_asks_twilio_to_retry(hook):
    hook.brokerage = ""                   # the callback beat the worker's call state (24.2)
    assert _post({"CallSid": SID, "CallStatus": "completed", "CallDuration": "9"}).status_code == 503
    assert hook.accepted == [] and hook.cleaned == []


def test_a_failed_cleanup_does_not_unrecord_a_stored_outcome(hook):
    hook.cleanup_error = RuntimeError("redis down")
    assert _post({"CallSid": SID, "CallStatus": "completed", "CallDuration": "9"}).status_code == 204
    assert len(hook.accepted) == 1


def test_the_signature_is_checked_before_anything_is_stored():
    src = inspect.getsource(ca.twilio_status_webhook)
    assert src.index("_validate_twilio_webhook_signature(") < src.index("_twilio_call_brokerage(")
    assert src.index("_twilio_call_brokerage(") < src.index("accept_call_outcome(")


def test_twilio_is_told_to_retry_a_failed_status_callback(monkeypatch):
    """By default Twilio retries only a failed connection; the 503 above is
    useless without a retry policy that covers it. The override rides in the
    URL fragment, which Twilio leaves out of the signature."""
    import command_providers

    assert command_providers.TWILIO_STATUS_RETRY.startswith("#")
    assert "rp=5xx" in command_providers.TWILIO_STATUS_RETRY
    assert "{TWILIO_STATUS_RETRY}" in inspect.getsource(command_providers.place_twilio_call)


def test_a_status_signed_for_the_url_without_its_fragment_is_accepted(monkeypatch):
    """Twilio is given .../status#rc=3&rp=5xx,ct and signs the URL without the
    fragment; the validator must accept exactly that, and refuse a signature
    over the fragment form."""
    import command_providers
    from fastapi import HTTPException
    from twilio.request_validator import RequestValidator

    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")
    suffix = "/api/commands/webhooks/twilio/status"
    given = f"https://api.example.test{suffix}{command_providers.TWILIO_STATUS_RETRY}"
    params = {"CallSid": SID, "CallStatus": "completed"}
    signed = RequestValidator("test-token").compute_signature(given.split("#", 1)[0], params)

    request = _Request(params)
    request.headers["X-Twilio-Signature"] = signed
    ca._validate_twilio_webhook_signature(request, request._form, suffix)   # accepted

    request.headers["X-Twilio-Signature"] = RequestValidator("test-token").compute_signature(given, params)
    with pytest.raises(HTTPException):
        ca._validate_twilio_webhook_signature(request, request._form, suffix)


# ── whose call is it ──────────────────────────────────────────────────────


class _Conn:
    def __init__(self, tenant):
        self.tenant, self.queries = tenant, []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self.tenant


def _tx(conn):
    class _Tx:
        def __init__(self, _ctx):
            pass

        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    return _Tx


def test_the_brokerage_comes_from_the_outbound_call_state(monkeypatch):
    import twilio_call_handler

    async def state(_sid):
        return {"tenant_id": "b-state", "direction": "outbound"}

    conn = _Conn("b-session")
    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", state)
    monkeypatch.setattr(ca, "tenant_tx", _tx(conn))
    assert asyncio.run(ca._twilio_call_brokerage(SID)) == "b-state"
    assert conn.queries == []


@pytest.mark.parametrize("state", [None, {"tenant_id": "b-in", "direction": "inbound"}, "raise"])
def test_otherwise_from_the_call_session(monkeypatch, state):
    import twilio_call_handler

    async def load(_sid):
        if state == "raise":
            raise RuntimeError("redis down")
        return state

    conn = _Conn("b-session")
    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", load)
    monkeypatch.setattr(ca, "tenant_tx", _tx(conn))
    assert asyncio.run(ca._twilio_call_brokerage(SID)) == "b-session"
    assert "live_call_sessions" in conn.queries[0][0] and conn.queries[0][1] == (SID,)


def test_unknown_everywhere_is_empty(monkeypatch):
    import twilio_call_handler

    async def load(_sid):
        return None

    monkeypatch.setattr(twilio_call_handler, "load_twilio_call_state", load)
    monkeypatch.setattr(ca, "tenant_tx", _tx(_Conn(None)))
    assert asyncio.run(ca._twilio_call_brokerage(SID)) == ""
