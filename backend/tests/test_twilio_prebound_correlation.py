"""An approved Twilio call is managed from before Twilio is asked to place it
(external audit 24.2).

The worker stored the call's Redis state keyed on the CallSid only after
calls.create returned. Twilio's answer webhook waited at most 20 x 250 ms for
that state, then hung up on the approved call as "unmanaged" ("This call
cannot be connected safely"): a slow create response or a paused worker turned
an approved call into a customer failure. Now a correlation state is written
first, its id rides in the answer and status URLs (signed by Twilio, query
included), and an early webhook finds the call by it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from starlette.datastructures import FormData

import command_providers
import commands_api as ca
import twilio_call_handler as tch

SID = "CA" + "f" * 32
ACCOUNT = "AC" + "1" * 32
TENANT = str(uuid.uuid4())


class _Redis:
    def __init__(self):
        self.values = {}

    async def ping(self):
        return True

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)


async def _value(v):
    return v


@pytest.fixture()
def redis(monkeypatch):
    r = _Redis()
    monkeypatch.setattr(tch, "_get_redis", lambda: _value(r))
    monkeypatch.setattr(tch, "_STATE_WAIT_SECONDS", 0.001)
    return r


def _prebound(correlation):
    return asyncio.run(tch.initialize_outbound_twilio_correlation(
        correlation, "+15555550101", tenant_id=TENANT, account_sid=ACCOUNT))


# ── the state exists before the call ─────────────────────────────────────


def test_an_answer_that_beats_the_create_response_finds_the_call(redis, monkeypatch):
    correlation = str(uuid.uuid4())
    _prebound(correlation)
    waits = []
    real_load = tch.load_twilio_call_state

    async def load(call_sid, *, wait_for_initialization=False):
        waits.append(wait_for_initialization)
        return await real_load(call_sid, wait_for_initialization=wait_for_initialization)

    monkeypatch.setattr(tch, "load_twilio_call_state", load)
    state = asyncio.run(tch.resolve_outbound_twilio_call_state(SID, correlation, wait_for_initialization=True))
    assert state and state["tenant_id"] == TENANT and state["account_sid"] == ACCOUNT
    assert waits[0] is False                      # no 5 s wait: the correlation is already there
    bound = json.loads(redis.values[tch._state_key(SID)])
    assert bound["correlation"] == correlation and bound["direction"] == "outbound"


def test_the_workers_own_state_is_never_overwritten(redis):
    correlation = str(uuid.uuid4())
    _prebound(correlation)
    asyncio.run(tch.initialize_twilio_call_state(SID, "+15555550101", tenant_id=TENANT,
                                                 account_sid=ACCOUNT, correlation=correlation))
    before = redis.values[tch._state_key(SID)]
    asyncio.run(tch.resolve_outbound_twilio_call_state(SID, correlation))
    assert redis.values[tch._state_key(SID)] == before


@pytest.mark.parametrize("correlation", ["", "not-a-uuid", str(uuid.uuid4())])
def test_an_unknown_or_malformed_reference_is_still_unmanaged(redis, correlation):
    assert asyncio.run(tch.resolve_outbound_twilio_call_state(
        SID, correlation, wait_for_initialization=True)) is None
    assert tch._state_key(SID) not in redis.values


def test_an_inbound_shaped_reference_is_not_an_outbound_call(redis):
    correlation = str(uuid.uuid4())
    redis.values[tch._correlation_key(correlation)] = json.dumps({"tenant_id": TENANT, "direction": "inbound"})
    assert asyncio.run(tch.resolve_outbound_twilio_call_state(SID, correlation)) is None


def test_the_correlation_state_validates_what_it_stores(redis):
    with pytest.raises(ValueError):
        asyncio.run(tch.initialize_outbound_twilio_correlation(
            "nope", "+1", tenant_id=TENANT, account_sid=ACCOUNT))
    with pytest.raises(ValueError):
        asyncio.run(tch.initialize_outbound_twilio_correlation(
            str(uuid.uuid4()), "+1", tenant_id="not-a-uuid", account_sid=ACCOUNT))


def test_the_worker_writes_the_correlation_before_placing_the_call():
    source = inspect.getsource(ca._execute_command_job)
    prebound = source.index("await initialize_outbound_twilio_correlation(")
    placed = source.index("provider_result = await place_twilio_call(")
    assert prebound < placed
    assert "correlation=twilio_correlation" in source[placed:placed + 300]
    # Still before submission_started: a Redis failure here is a clean failure,
    # never an uncertain call.
    assert prebound < source.index("submission_started = True", source.index("initialize_outbound_twilio_correlation"))


# ── the URLs carry it ────────────────────────────────────────────────────


def _placed(monkeypatch, **env):
    captured = {}

    class _Calls:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(sid=SID)

    class _Client:
        def __init__(self, *_a, **_k):
            self.calls = _Calls()

    import twilio.rest

    monkeypatch.setattr(twilio.rest, "Client", _Client)
    for k, v in {"TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_AUTH_TOKEN": "auth-token",
                 "TWILIO_FROM_NUMBER": "+15555550100", "ORACLE_PUBLIC_BASE_URL": "https://api.example.test",
                 "ORACLE_TWILIO_ACCOUNT_TIER": "full", **env}.items():
        monkeypatch.setenv(k, v)
    correlation = str(uuid.uuid4())
    asyncio.run(command_providers.place_twilio_call({"target": {"phone": "+15555550101"}},
                                                    correlation=correlation))
    return captured, correlation


def test_both_webhook_urls_carry_the_reference(monkeypatch):
    captured, correlation = _placed(monkeypatch)
    assert captured["url"] == f"https://api.example.test/api/commands/webhooks/twilio?neoh_ref={correlation}"
    assert captured["status_callback"] == (
        f"https://api.example.test/api/commands/webhooks/twilio/status?neoh_ref={correlation}"
        f"{command_providers.TWILIO_STATUS_RETRY}")      # the retry override stays in the fragment


def test_a_configured_twiml_url_with_a_query_keeps_it(monkeypatch):
    captured, correlation = _placed(monkeypatch, ORACLE_TWILIO_TWIML_URL="https://twiml.example.test/x?a=1")
    assert captured["url"] == f"https://twiml.example.test/x?a=1&neoh_ref={correlation}"


# ── the webhooks verify it and use it ────────────────────────────────────


class _Request:
    def __init__(self, form, query=""):
        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": "sig", "host": "internal.invalid"}
        self.query_params = dict(p.split("=", 1) for p in query.split("&") if p)
        self.url = SimpleNamespace(netloc="internal.invalid", scheme="http", query=query)

    async def form(self):
        return self._form


def _signed(monkeypatch, path, query, params, *, signed_query=None):
    from twilio.request_validator import RequestValidator

    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")
    request = _Request(params, query)
    q = query if signed_query is None else signed_query
    request.headers["X-Twilio-Signature"] = RequestValidator("test-token").compute_signature(
        f"https://api.example.test{path}{'?' + q if q else ''}", params)
    return request


@pytest.mark.parametrize("path", ["/api/commands/webhooks/twilio", "/api/commands/webhooks/twilio/status"])
def test_the_signature_covers_the_reference(monkeypatch, path):
    from fastapi import HTTPException

    ref = f"neoh_ref={uuid.uuid4()}"
    params = {"CallSid": SID, "CallStatus": "in-progress"}
    request = _signed(monkeypatch, path, ref, params)
    ca._validate_twilio_webhook_signature(request, request._form, ca._twilio_signed_suffix(request, path))
    forged = _signed(monkeypatch, path, f"neoh_ref={uuid.uuid4()}", params, signed_query=ref)
    with pytest.raises(HTTPException):          # someone else's reference swapped in
        ca._validate_twilio_webhook_signature(forged, forged._form, ca._twilio_signed_suffix(forged, path))


def test_the_answer_webhook_connects_an_early_call(redis, monkeypatch):
    correlation = str(uuid.uuid4())
    _prebound(correlation)
    monkeypatch.setattr(ca, "_validate_twilio_webhook_signature", lambda *a, **k: None)

    async def spoken(text, **_k):
        return f"<Say>{text}</Say>"

    monkeypatch.setattr(ca, "_spoken", spoken)
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "false")
    early = asyncio.run(ca.twilio_webhook(_Request({"CallSid": SID, "To": "+15555550101"},
                                                   f"neoh_ref={correlation}")))
    assert b"cannot be connected safely" not in early.body
    unmanaged = asyncio.run(ca.twilio_webhook(_Request({"CallSid": "CA" + "0" * 32, "To": "+1"},
                                                       f"neoh_ref={uuid.uuid4()}")))
    assert b"cannot be connected safely" in unmanaged.body


def test_an_early_status_knows_its_brokerage_by_the_reference(redis, monkeypatch):
    correlation = str(uuid.uuid4())
    _prebound(correlation)

    class _Tx:
        def __init__(self, _ctx):
            pass

        async def __aenter__(self):
            raise AssertionError("the reference answered first; no database read")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ca, "tenant_tx", _Tx)
    assert asyncio.run(ca._twilio_call_brokerage(SID, correlation)) == TENANT


def test_the_status_route_stores_with_the_reference_and_cleans_it_up(redis, monkeypatch):
    correlation = str(uuid.uuid4())
    _prebound(correlation)
    monkeypatch.setattr(ca, "_validate_twilio_webhook_signature", lambda *a, **k: None)
    stored = []

    async def accept(provider, call_sid, status, duration=None, *, tenant_id, aliases=()):
        stored.append((call_sid, status, tenant_id, list(aliases)))
        return 0

    monkeypatch.setattr(ca, "accept_call_outcome", accept)
    response = asyncio.run(ca.twilio_status_webhook(_Request(
        {"CallSid": SID, "CallStatus": "completed", "CallDuration": "12"}, f"neoh_ref={correlation}")))
    assert response.status_code == 204
    assert stored == [(SID, "completed", TENANT, [correlation])]
    assert tch._correlation_key(correlation) not in redis.values
