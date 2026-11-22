"""Twilio calls on a brokerage's own account (external audit 23.2, 23.3).

A brokerage can store its own Twilio credential (PUT /providers/twilio) and
the outbound rail dials with it. But every outbound webhook (/webhooks/twilio,
/status, /speech) and the media socket's handshake were checked against the
platform's current token only: a brokerage-account call's answer was refused
(the callee heard an application error), its outcome was never recorded, and
rotating the platform token (TWILIO_AUTH_TOKEN_PREVIOUS) broke every call in
flight. Hand-offs and refusals moved the call with the platform's credential,
whatever account carried it.

Now the tokens are chosen from Neoh's own state — the pre-submission
correlation or the CallSid's state, naming tenant + account — before anything
is acted on: the platform's current and previous token for the platform's
account, else only that brokerage's stored token, whose account must match.
Unresolvable is refused (503), never checked against another account. The
media socket carries an opaque per-call reference in its path (Twilio forbids
a query string in <Stream url>). Live calls are moved only with the whole
credential of the account their state names.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

import ai_chat_agent
import command_providers
import commands_api as ca
import qwen_omni_realtime
import twilio_call_handler as tch
import voice_provider

BACKEND = Path(__file__).resolve().parents[1]
BASE = "https://api.example.test"
PLATFORM = "AC" + "0" * 32
ACCOUNT_A = "AC" + "a" * 32
ACCOUNT_B = "AC" + "b" * 32
TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
TENANT_P = str(uuid.uuid4())
SID_A = "CA" + "a" * 32
SID_B = "CA" + "b" * 32
SID_P = "CA" + "c" * 32
ANSWER = "/api/commands/webhooks/twilio"
STATUS = "/api/commands/webhooks/twilio/status"
SPEECH = "/api/commands/webhooks/twilio/speech"
# Built at run time: nothing secret-shaped is committed (gitleaks).
TOKEN = {name: f"{name}-{secrets.token_hex(12)}" for name in ("platform", "previous", "a", "b")}


class _Redis:
    def __init__(self):
        self.values: dict = {}

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
def world(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(tch, "_get_redis", lambda: _value(redis))
    monkeypatch.setattr(tch, "_STATE_WAIT_SECONDS", 0.001)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ORACLE_ENV", "production")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", PLATFORM)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN["platform"])
    monkeypatch.setenv("TWILIO_AUTH_TOKEN_PREVIOUS", TOKEN["previous"])
    monkeypatch.setenv("TWILIO_API_KEY", "SK" + "9" * 32)
    monkeypatch.setenv("TWILIO_API_SECRET", f"platform-secret-{secrets.token_hex(6)}")
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", f"master-{secrets.token_hex(8)}")
    monkeypatch.delenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", raising=False)
    monkeypatch.delenv("ORACLE_TWILIO_ACCOUNT_TIER", raising=False)
    stored = {
        TENANT_A: {"account_sid": ACCOUNT_A, "auth_token": TOKEN["a"]},
        TENANT_B: {"account_sid": ACCOUNT_B, "auth_token": TOKEN["b"]},
    }
    loads: list = []

    async def load_credential(ctx, provider, **_kwargs):
        loads.append((ctx.tenant_id, provider))
        row = stored.get(ctx.tenant_id)
        if isinstance(row, Exception):
            raise row
        return None if row is None else json.dumps(row)

    async def spoken(text, **_kwargs):
        return f"<Say>{text}</Say>"

    accepted: list = []

    async def accept(provider, call_sid, status, duration=None, *, tenant_id, aliases=()):
        accepted.append((call_sid, status, tenant_id))
        return 1

    # The durable rows (live_call_sessions by provider_call_id), read as the
    # platform: CallSid -> brokerage, or an exception for an unreadable read.
    sessions: dict = {}
    session_reads: list = []

    class _Conn:
        async def fetchval(self, sql, *args):
            session_reads.append((sql, args))
            if isinstance(sessions.get("error"), Exception):
                raise sessions["error"]
            return sessions.get(args[0]) if "live_call_sessions" in sql else None

        async def execute(self, *_a):
            return "UPDATE 0"

    class _Tx:
        def __init__(self, _ctx):
            pass

        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ca, "tenant_tx", _Tx)
    monkeypatch.setattr(ca, "_load_provider_credential", load_credential)
    monkeypatch.setattr(ca, "_spoken", spoken)
    monkeypatch.setattr(ca, "accept_call_outcome", accept)
    return SimpleNamespace(redis=redis, stored=stored, loads=loads, accepted=accepted, sessions=sessions,
                           session_reads=session_reads)


def _prebound(tenant_id, account_sid):
    """The state the outbound rail writes BEFORE Twilio is asked for the call."""
    correlation = str(uuid.uuid4())
    asyncio.run(tch.initialize_outbound_twilio_correlation(
        correlation, "+15555550101", tenant_id=tenant_id, account_sid=account_sid))
    return correlation


def _bound(call_sid, tenant_id, account_sid):
    """The worker's CallSid state after the create returned (same correlation)."""
    correlation = _prebound(tenant_id, account_sid)
    asyncio.run(tch.initialize_twilio_call_state(
        call_sid, "+15555550101", tenant_id=tenant_id, account_sid=account_sid, correlation=correlation))
    return correlation


class _Request:
    def __init__(self, form, query="", signature=None):
        self._form = FormData(form)
        self.headers = {"host": "internal.invalid"}
        if signature is not None:
            self.headers["X-Twilio-Signature"] = signature
        self.query_params = dict(p.split("=", 1) for p in query.split("&") if p)
        self.url = SimpleNamespace(netloc="internal.invalid", scheme="http", query=query)

    async def form(self):
        return self._form


def _signed(path, form, token, *, query="", signed_query=None):
    q = query if signed_query is None else signed_query
    url = f"{BASE}{path}{'?' + q if q else ''}"
    return _Request(form, query, RequestValidator(token).compute_signature(url, form))


def _refused(coro) -> int:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value.status_code


# ── 23.2: outbound HTTP webhooks ─────────────────────────────────────────


def test_each_brokerage_signs_its_own_calls_answer_and_no_one_elses(world):
    ref = f"neoh_ref={_prebound(TENANT_A, ACCOUNT_A)}"
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "To": "+15555550101"}
    own = asyncio.run(ca.twilio_webhook(_signed(ANSWER, form, TOKEN["a"], query=ref)))
    assert own.status_code == 200 and b"<Gather" in own.body
    assert b"cannot be connected safely" not in own.body
    for token in (TOKEN["b"], TOKEN["platform"], TOKEN["previous"]):
        assert _refused(ca.twilio_webhook(_signed(ANSWER, form, token, query=ref))) == 400
    assert world.loads and all(load == (TENANT_A, "twilio") for load in world.loads)


def test_a_brokerage_call_outcome_is_recorded_only_under_its_own_token(world):
    ref = _prebound(TENANT_B, ACCOUNT_B)
    form = {"CallSid": SID_B, "AccountSid": ACCOUNT_B, "CallStatus": "completed", "CallDuration": "7"}
    for token in (TOKEN["a"], TOKEN["platform"], TOKEN["previous"]):
        assert _refused(ca.twilio_status_webhook(
            _signed(STATUS, form, token, query=f"neoh_ref={ref}"))) == 400
    assert world.accepted == []
    response = asyncio.run(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["b"], query=f"neoh_ref={ref}")))
    assert response.status_code == 204
    assert world.accepted == [(SID_B, "completed", TENANT_B)]


@pytest.mark.parametrize("token", ["platform", "previous"])
def test_a_platform_account_call_accepts_the_current_and_the_rotated_out_token(world, token):
    ref = f"neoh_ref={_prebound(TENANT_P, PLATFORM)}"
    form = {"CallSid": SID_P, "AccountSid": PLATFORM, "CallStatus": "in-progress"}
    assert asyncio.run(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN[token], query=ref))).status_code == 204
    assert world.accepted == [(SID_P, "in-progress", TENANT_P)]
    assert world.loads == []          # the platform's account: no brokerage credential is read
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["a"], query=ref))) == 400


def test_a_missing_signature_is_refused_before_any_state_or_credential_is_read(world, monkeypatch):
    ref = _prebound(TENANT_A, ACCOUNT_A)

    async def no_lookup(*_a, **_k):
        raise AssertionError("a request with no signature reached the state")

    monkeypatch.setattr(tch, "outbound_twilio_signing_state", no_lookup)
    monkeypatch.setattr(ca, "_durable_twilio_call_state", no_lookup)
    for route, path in ((ca.twilio_webhook, ANSWER), (ca.twilio_status_webhook, STATUS),
                        (ca.twilio_speech, SPEECH)):
        request = _Request({"CallSid": SID_A, "CallStatus": "completed"}, f"neoh_ref={ref}")
        assert _refused(route(request)) == 400, path
    assert world.loads == [] and world.accepted == []


def test_a_call_no_state_names_is_refused_not_checked_against_the_platform(world):
    form = {"CallSid": SID_A, "CallStatus": "completed"}
    unknown = f"neoh_ref={uuid.uuid4()}"
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["platform"], query=unknown))) == 503
    assert _refused(ca.twilio_webhook(_signed(ANSWER, form, TOKEN["platform"], query=unknown))) == 503
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["platform"], query="neoh_ref=junk"))) == 503
    assert world.accepted == [] and world.loads == []


@pytest.mark.parametrize("credential", ["other_account", "missing", "unreadable", "no_token"])
def test_a_brokerage_call_without_a_matching_credential_is_refused(world, credential):
    world.stored[TENANT_A] = {
        "other_account": {"account_sid": ACCOUNT_B, "auth_token": TOKEN["a"]},
        "missing": None,
        "unreadable": RuntimeError("decrypt failed"),
        "no_token": {"account_sid": ACCOUNT_A, "auth_token": "", "api_key": "SK" + "1" * 32,
                     "api_secret": "s"},
    }[credential]
    ref = f"neoh_ref={_prebound(TENANT_A, ACCOUNT_A)}"
    form = {"CallSid": SID_A, "CallStatus": "completed"}
    for token in (TOKEN["a"], TOKEN["platform"], TOKEN["previous"]):
        assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, token, query=ref))) == 503
    assert world.accepted == []


def test_a_crossed_reference_resolves_to_neither_call(world):
    """B signs, with its own token and its own reference, a webhook naming
    A's CallSid: A's call is bound to another reference, so it is refused —
    never acted on as A's call under B's signature."""
    _bound(SID_A, TENANT_A, ACCOUNT_A)
    ref_b = _prebound(TENANT_B, ACCOUNT_B)
    before = dict(world.redis.values)
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_B, "CallStatus": "completed"}
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["b"], query=f"neoh_ref={ref_b}"))) == 503
    assert _refused(ca.twilio_webhook(_signed(ANSWER, form, TOKEN["b"], query=f"neoh_ref={ref_b}"))) == 503
    assert world.accepted == [] and world.redis.values == before


def test_a_claimed_account_that_is_not_the_calls_is_refused(world):
    ref = f"neoh_ref={_prebound(TENANT_A, ACCOUNT_A)}"
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_B, "CallStatus": "completed"}
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["a"], query=ref))) == 400
    assert world.accepted == []


def test_the_reference_is_covered_by_the_signature(world):
    ref_a = _prebound(TENANT_A, ACCOUNT_A)
    ref_b = _prebound(TENANT_B, ACCOUNT_B)
    form = {"CallSid": SID_A, "CallStatus": "completed"}
    # Signed by A for A's reference, replayed with B's reference swapped in.
    forged = _signed(STATUS, form, TOKEN["a"], query=f"neoh_ref={ref_b}", signed_query=f"neoh_ref={ref_a}")
    assert _refused(ca.twilio_status_webhook(forged)) == 400


def _reply(monkeypatch):
    asked = []

    async def generate(_sid, _speech, *, tenant_id):
        asked.append(tenant_id)
        return "Happy to help."

    monkeypatch.setattr(ai_chat_agent, "_generate_voice_reply", generate)
    return asked


def test_speech_carries_the_calls_reference_and_is_checked_under_its_account(world, monkeypatch):
    asked = _reply(monkeypatch)
    correlation = _bound(SID_A, TENANT_A, ACCOUNT_A)
    ref = f"neoh_ref={correlation}"
    answer = asyncio.run(ca.twilio_webhook(_signed(ANSWER, {"CallSid": SID_A, "To": "+1"}, TOKEN["a"], query=ref)))
    assert f'action="{BASE}{SPEECH}?{ref}"'.encode() in answer.body

    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "SpeechResult": "when is the showing", "To": "+1"}
    reply = asyncio.run(ca.twilio_speech(_signed(SPEECH, form, TOKEN["a"], query=ref)))
    assert reply.status_code == 200 and asked == [TENANT_A]
    assert f'action="{BASE}{SPEECH}?{ref}"'.encode() in reply.body      # the next turn too
    for token in (TOKEN["b"], TOKEN["platform"]):
        assert _refused(ca.twilio_speech(_signed(SPEECH, form, token, query=ref))) == 400
    # Signed without the reference Twilio was given: not the URL it signed.
    assert _refused(ca.twilio_speech(_signed(SPEECH, form, TOKEN["a"], query=ref, signed_query=""))) == 400
    assert asked == [TENANT_A]


def test_speech_from_a_call_answered_before_the_reference_was_added_still_resolves(world, monkeypatch):
    """A call already in flight at deploy has a Gather URL without neoh_ref:
    its CallSid state names its account."""
    asked = _reply(monkeypatch)
    _bound(SID_A, TENANT_A, ACCOUNT_A)
    form = {"CallSid": SID_A, "SpeechResult": "hello", "To": "+1"}
    assert asyncio.run(ca.twilio_speech(_signed(SPEECH, form, TOKEN["a"]))).status_code == 200
    assert asked == [TENANT_A]
    assert _refused(ca.twilio_speech(_signed(SPEECH, form, TOKEN["platform"]))) == 400


# ── 23.2: the media socket ───────────────────────────────────────────────


def _mint(call_sid, tenant_id, account_sid, monkeypatch, *, qwen=True):
    if qwen:
        monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")
    _bound(call_sid, tenant_id, account_sid)
    url = asyncio.run(tch.create_twilio_media_stream_url(call_sid))
    return url, url.rsplit("/", 1)[1]


def _ws_sig(url, token):
    return RequestValidator(token).compute_signature(url, {})


def test_the_stream_url_carries_an_opaque_reference_in_its_path_never_a_query(world, monkeypatch):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    assert url == f"wss://api.example.test/api/commands/media/twilio/{ref}"
    assert "?" not in url and SID_A not in url and TENANT_A not in url
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", ref)
    assert world.redis.values[f"twilio:media_ref:{ref}"] == SID_A
    assert json.loads(world.redis.values[f"twilio:call_state:{SID_A}"])["media_ref"] == ref


def test_the_handshake_is_checked_against_the_brokerage_that_carries_the_call(world, monkeypatch):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    handshake = tch.authenticate_twilio_media_handshake
    assert asyncio.run(handshake(ref, _ws_sig(url, TOKEN["a"]))) == SID_A
    for token in (TOKEN["b"], TOKEN["platform"], TOKEN["previous"]):
        assert asyncio.run(handshake(ref, _ws_sig(url, token))) is None


@pytest.mark.parametrize("token", ["platform", "previous"])
def test_a_platform_account_handshake_accepts_the_current_and_previous_token(world, monkeypatch, token):
    url, ref = _mint(SID_P, TENANT_P, PLATFORM, monkeypatch)
    handshake = tch.authenticate_twilio_media_handshake
    assert asyncio.run(handshake(ref, _ws_sig(url, TOKEN[token]))) == SID_P
    assert asyncio.run(handshake(ref, _ws_sig(url, TOKEN["a"]))) is None


def test_unknown_expired_replaced_or_unsigned_references_are_refused(world, monkeypatch):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    signed = _ws_sig(url, TOKEN["a"])
    handshake = tch.authenticate_twilio_media_handshake
    assert asyncio.run(handshake(ref, "")) is None                          # missing signature
    assert asyncio.run(handshake("x" * 43, signed)) is None                  # unknown
    assert asyncio.run(handshake("../" + ref, signed)) is None               # malformed
    new_url = asyncio.run(tch.create_twilio_media_stream_url(SID_A))         # re-minted
    new_ref = new_url.rsplit("/", 1)[1]
    assert asyncio.run(handshake(ref, signed)) is None                       # only the latest is live
    assert asyncio.run(handshake(new_ref, _ws_sig(new_url, TOKEN["a"]))) == SID_A
    world.redis.values.pop(f"twilio:media_ref:{new_ref}")                    # expired
    assert asyncio.run(handshake(new_ref, _ws_sig(new_url, TOKEN["a"]))) is None


def test_a_media_handshake_for_a_call_with_no_matching_credential_is_refused(world, monkeypatch):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    world.stored[TENANT_A] = {"account_sid": ACCOUNT_B, "auth_token": TOKEN["a"]}
    assert asyncio.run(tch.authenticate_twilio_media_handshake(ref, _ws_sig(url, TOKEN["a"]))) is None


class _Socket:
    def __init__(self, signature, frames=()):
        self.headers = {"x-twilio-signature": signature}
        self.frames = list(frames)
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, *, code):
        self.closed_code = code

    async def receive_text(self):
        return self.frames.pop(0)


def _start(call_sid, account_sid, bridge_token):
    return json.dumps({"event": "start", "streamSid": "MZ" + "d" * 32, "start": {
        "callSid": call_sid, "accountSid": account_sid, "streamSid": "MZ" + "d" * 32,
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        "customParameters": {"bridge_token": bridge_token}}})


@pytest.fixture()
def bridge(monkeypatch):
    opened = []

    class _Bridge:
        def __init__(self, _ws, call_sid, _start_event):
            opened.append(call_sid)

        async def run(self):
            return None

    async def admitted(_tenant):
        return None

    monkeypatch.setattr(qwen_omni_realtime, "TwilioQwenRealtimeBridge", _Bridge)
    monkeypatch.setattr(ca, "_realtime_refusal", admitted)
    return opened


def test_an_unknown_reference_is_closed_4403_before_accept(world, bridge):
    socket = _Socket("anything")
    asyncio.run(ca.twilio_qwen_media(socket, "x" * 43))
    assert socket.closed_code == 4403 and not socket.accepted and bridge == []


def test_a_brokerage_call_opens_its_stream_under_its_own_signature(world, monkeypatch, bridge):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    token = tch.create_twilio_bridge_token(SID_A)
    socket = _Socket(_ws_sig(url, TOKEN["a"]), [_start(SID_A, ACCOUNT_A, token)])
    asyncio.run(ca.twilio_qwen_media(socket, ref))
    assert socket.accepted and socket.closed_code is None and bridge == [SID_A]

    platform_signed = _Socket(_ws_sig(url, TOKEN["platform"]), [_start(SID_A, ACCOUNT_A, token)])
    asyncio.run(ca.twilio_qwen_media(platform_signed, ref))
    assert platform_signed.closed_code == 4403 and not platform_signed.accepted


def test_another_calls_start_frame_on_this_reference_is_refused(world, monkeypatch, bridge):
    """The reference is A's, signed by A; the start frame is B's call with
    B's own valid bridge token and account: crossed, so 4403."""
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    _bound(SID_B, TENANT_B, ACCOUNT_B)
    socket = _Socket(_ws_sig(url, TOKEN["a"]),
                     [_start(SID_B, ACCOUNT_B, tch.create_twilio_bridge_token(SID_B))])
    asyncio.run(ca.twilio_qwen_media(socket, ref))
    assert socket.closed_code == 4403 and bridge == []


def test_an_expired_bridge_token_is_refused_after_a_valid_handshake(world, monkeypatch, bridge):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    expired = tch.create_twilio_bridge_token(SID_A, now=int(time.time()) - 400)
    socket = _Socket(_ws_sig(url, TOKEN["a"]), [_start(SID_A, ACCOUNT_A, expired)])
    asyncio.run(ca.twilio_qwen_media(socket, ref))
    assert socket.accepted and socket.closed_code == 4403 and bridge == []


# ── 23.3: moving a live call ─────────────────────────────────────────────


@pytest.fixture()
def rest(monkeypatch):
    """Every Twilio REST client built, with the credential it was built from."""
    built = []

    class _Call:
        def __init__(self, sid):
            self.sid = sid

        def update(self, **kwargs):
            built[-1]["updates"].append((self.sid, kwargs))

    class _Client:
        def calls(self, sid):
            return _Call(sid)

    def client(account_sid, auth_token, api_key, api_secret):
        built.append({"credential": (account_sid, auth_token, api_key, api_secret), "updates": []})
        return _Client()

    monkeypatch.setattr(command_providers, "_twilio_client", client)
    return built


def test_a_transfer_uses_the_credential_it_is_given_whole(world, rest):
    """The 23.3 probe: the passed brokerage credential was dropped for the
    platform's. Never completed from the environment either (the platform's
    API key pair is set here), which would pair A's SID with it."""
    asyncio.run(voice_provider.TwilioVoiceProvider().transfer_call(
        SID_A, redirect_url=f"{BASE}/x",
        credentials={"account_sid": ACCOUNT_A, "auth_token": TOKEN["a"]}))
    assert [b["credential"] for b in rest] == [(ACCOUNT_A, TOKEN["a"], "", "")]
    assert rest[0]["updates"] == [(SID_A, {"url": f"{BASE}/x", "method": "POST"})]


@pytest.mark.parametrize("call", ["brokerage", "platform"])
def test_without_a_credential_the_calls_own_account_is_resolved(world, rest, call):
    if call == "brokerage":
        _bound(SID_A, TENANT_A, ACCOUNT_A)
        expected = (ACCOUNT_A, TOKEN["a"], "", "")
    else:
        _bound(SID_A, TENANT_P, PLATFORM)
        expected = (PLATFORM, TOKEN["platform"], os.environ["TWILIO_API_KEY"], os.environ["TWILIO_API_SECRET"])
    asyncio.run(voice_provider.TwilioVoiceProvider().transfer_call(SID_A, redirect_url=f"{BASE}/x"))
    assert [b["credential"] for b in rest] == [expected]


@pytest.mark.parametrize("problem", ["other_account", "missing", "no_state"])
def test_a_brokerage_call_is_never_moved_with_the_platforms_credential(world, rest, caplog, problem):
    if problem != "no_state":
        _bound(SID_A, TENANT_A, ACCOUNT_A)
        world.stored[TENANT_A] = None if problem == "missing" else {"account_sid": ACCOUNT_B,
                                                                     "auth_token": TOKEN["b"]}
    caplog.set_level(logging.WARNING, logger="oracle.twilio_realtime")
    with pytest.raises(tch.TwilioCallStateUnavailable):
        asyncio.run(tch.credentials_for_call(SID_A))
    with pytest.raises(tch.TwilioCallStateUnavailable):
        asyncio.run(voice_provider.TwilioVoiceProvider().transfer_call(SID_A, redirect_url=f"{BASE}/x"))
    assert rest == []                                  # no REST call at all
    assert "transfer_credentials_unavailable" in caplog.text
    assert TOKEN["a"] not in caplog.text and TOKEN["b"] not in caplog.text


def _inbound_bridge(call_sid, tenant_id, account_sid, monkeypatch):
    import telephony_api

    asyncio.run(tch.initialize_inbound_twilio_call_state(
        call_sid, "+15555550101", tenant_id=tenant_id, agent_id="agent@example.test",
        account_sid=account_sid, route_id=str(uuid.uuid4()), voice_call_id=str(uuid.uuid4()),
        intake_mode="buyer", forward_available=True))

    async def transfer_url(_sid, *, reason):
        return f"{BASE}/api/telephony/webhooks/twilio/inbound/key/transfer?reason={reason}"

    monkeypatch.setattr(telephony_api, "transfer_webhook_url", transfer_url)
    return qwen_omni_realtime.TwilioQwenRealtimeBridge(
        SimpleNamespace(), call_sid, json.loads(_start(call_sid, account_sid, "t")),
        settings=qwen_omni_realtime.QwenRealtimeSettings(api_key="k", workspace_id="w"))


def test_the_agent_hand_off_moves_a_brokerage_call_with_its_own_credential(world, rest, monkeypatch):
    bridge = _inbound_bridge(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    asyncio.run(bridge._redirect_to_agent())
    assert [b["credential"] for b in rest] == [(ACCOUNT_A, TOKEN["a"], "", "")]
    assert rest[0]["updates"][0][0] == SID_A


def test_a_hand_off_with_no_matching_credential_makes_no_rest_call(world, rest, monkeypatch, caplog):
    bridge = _inbound_bridge(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)
    world.stored[TENANT_A] = {"account_sid": ACCOUNT_B, "auth_token": TOKEN["b"]}
    caplog.set_level(logging.WARNING, logger="oracle.twilio_realtime")
    with pytest.raises(tch.TwilioCallStateUnavailable):
        asyncio.run(bridge._redirect_to_agent())
    assert rest == [] and "transfer_credentials_unavailable" in caplog.text


def test_the_refusal_line_moves_a_brokerage_call_with_its_own_credential(world, rest):
    _bound(SID_A, TENANT_A, ACCOUNT_A)
    asyncio.run(ca._say_refused_and_end("twilio", SID_A))
    assert [b["credential"] for b in rest] == [(ACCOUNT_A, TOKEN["a"], "", "")]
    assert rest[0]["updates"] == [(SID_A, {"url": f"{BASE}/api/commands/webhooks/twilio/refused",
                                           "method": "POST"})]


def test_a_refusal_with_no_matching_credential_makes_no_rest_call(world, rest, caplog):
    _bound(SID_A, TENANT_A, ACCOUNT_A)
    world.stored[TENANT_A] = {"account_sid": ACCOUNT_B, "auth_token": TOKEN["b"]}
    caplog.set_level(logging.WARNING)
    asyncio.run(ca._say_refused_and_end("twilio", SID_A))        # never raises: the stream's goodbye ends it
    assert rest == [] and "transfer_credentials_unavailable" in caplog.text


def test_the_plivo_refusal_is_unchanged(world, monkeypatch):
    moved = []

    async def transfer(self, call_id, *, redirect_url, credentials=None):
        moved.append((call_id, credentials))

    monkeypatch.setattr(voice_provider.PlivoVoiceProvider, "transfer_call", transfer)
    asyncio.run(ca._say_refused_and_end("plivo", "uuid-1"))
    assert moved == [("uuid-1", None)]



# ── PR #182 review: when Redis no longer names the call ──────────────────


def _redis_down(monkeypatch):
    async def down():
        raise tch.TwilioCallStateUnavailable("redis down")

    monkeypatch.setattr(tch, "_get_redis", down)


@pytest.mark.parametrize("redis_state", ["expired", "down"])
@pytest.mark.parametrize("token", ["platform", "previous"])
def test_a_platform_calls_final_outcome_survives_its_redis_state(world, monkeypatch, redis_state, token):
    """The call's Redis state is gone (an hour-long Gather call, an eviction)
    or Redis is down: its brokerage comes from the session row the worker
    wrote, its account by the rail's own rule (no credential: the platform's)
    — and the final outcome is recorded instead of being refused into
    oblivion (#rc=3 retries only)."""
    world.sessions[SID_P] = TENANT_P
    if redis_state == "down":
        _redis_down(monkeypatch)
    form = {"CallSid": SID_P, "AccountSid": PLATFORM, "CallStatus": "completed", "CallDuration": "3700"}
    stale = f"neoh_ref={uuid.uuid4()}"         # its reference, long expired
    response = asyncio.run(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN[token], query=stale)))
    assert response.status_code == 204
    assert world.accepted == [(SID_P, "completed", TENANT_P)]
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["a"], query=stale))) == 400


@pytest.mark.parametrize("redis_state", ["expired", "down"])
def test_a_byo_call_without_redis_state_is_checked_only_under_its_own_account(world, monkeypatch, redis_state):
    world.sessions[SID_A] = TENANT_A
    if redis_state == "down":
        _redis_down(monkeypatch)
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    for token in (TOKEN["platform"], TOKEN["previous"], TOKEN["b"]):
        assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, token))) == 400
    assert world.accepted == []
    assert asyncio.run(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["a"]))).status_code == 204
    assert world.accepted == [(SID_A, "completed", TENANT_A)]


@pytest.mark.parametrize("claim", [PLATFORM, ACCOUNT_B, None])
def test_without_redis_the_signed_account_must_be_the_brokerages_own(world, claim):
    """TENANT_A dials from its own account A: a request naming the platform's
    account (signed by the platform), another brokerage's (signed by it), or
    no account is refused."""
    world.sessions[SID_A] = TENANT_A
    form = {"CallSid": SID_A, "CallStatus": "completed"}
    if claim:
        form["AccountSid"] = claim
    token = {PLATFORM: TOKEN["platform"], ACCOUNT_B: TOKEN["b"], None: TOKEN["a"]}[claim]
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, token))) == 400
    assert world.accepted == []


def test_without_redis_a_credential_now_for_another_account_is_refused(world):
    """The brokerage's stored credential no longer names the account in the
    request: the request is not that brokerage's, whoever signed it."""
    world.sessions[SID_A] = TENANT_A
    world.stored[TENANT_A] = {"account_sid": ACCOUNT_B, "auth_token": TOKEN["b"]}
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    for token in (TOKEN["a"], TOKEN["b"], TOKEN["platform"]):
        assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, token))) == 400
    assert world.accepted == []


@pytest.mark.parametrize("problem", ["no_session", "session_unreadable", "credential_unreadable"])
def test_without_redis_an_unknown_or_unreadable_call_is_refused(world, problem):
    if problem != "no_session":
        world.sessions[SID_A] = TENANT_A
    if problem == "session_unreadable":
        world.sessions["error"] = ConnectionError("database down")
    if problem == "credential_unreadable":
        world.stored[TENANT_A] = RuntimeError("decrypt failed")
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    for token in (TOKEN["a"], TOKEN["platform"]):
        assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, token))) == 503
    assert world.accepted == []


def test_a_crossed_reference_never_falls_through_to_the_session_rows(world):
    """Redis names the CallSid under another reference: refused outright,
    even though a session row would name a brokerage."""
    _bound(SID_A, TENANT_A, ACCOUNT_A)
    world.sessions[SID_A] = TENANT_A
    form = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    other = f"neoh_ref={_prebound(TENANT_A, ACCOUNT_A)}"
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["a"], query=other))) == 503
    assert world.accepted == [] and world.session_reads == []


# ── PR #182 review: one reference serves one call ────────────────────────


SID_X = "CA" + "e" * 32          # someone else's call, with no state here


@pytest.mark.parametrize("bound_by", ["worker", "answer"])
def test_a_reference_already_bound_cannot_be_paired_with_a_stateless_foreign_call(world, bound_by):
    """Brokerage A signs, with its own token, its own reference paired with
    another call's CallSid that has no state here. The reference already
    serves A's call: refused on every route — the foreign CallSid is never
    bound to A's state, and no outcome is recorded for it under A."""
    if bound_by == "worker":
        correlation = _bound(SID_A, TENANT_A, ACCOUNT_A)
    else:
        correlation = _prebound(TENANT_A, ACCOUNT_A)
        asyncio.run(ca.twilio_webhook(_signed(ANSWER, {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "To": "+1"},
                                              TOKEN["a"], query=f"neoh_ref={correlation}")))
    ref = f"neoh_ref={correlation}"
    foreign = {"CallSid": SID_X, "AccountSid": ACCOUNT_A, "CallStatus": "completed", "To": "+1"}
    assert _refused(ca.twilio_webhook(_signed(ANSWER, foreign, TOKEN["a"], query=ref))) == 503
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, foreign, TOKEN["a"], query=ref))) == 503
    assert f"twilio:call_state:{SID_X}" not in world.redis.values
    assert world.accepted == []
    assert asyncio.run(tch.twilio_correlation_call_sid(correlation)) == SID_A
    # A's own call still works under the reference.
    own = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    assert asyncio.run(ca.twilio_status_webhook(_signed(STATUS, own, TOKEN["a"], query=ref))).status_code == 204



def test_a_pairing_made_before_the_worker_binds_is_replaced_by_the_worker(world):
    """PR #182 review r2: before A's worker writes its state, brokerage B signs,
    with its own token, B's fresh reference paired with A's CallSid. That writes
    a state for A's call under B's reference. The worker, which holds Twilio's
    own CallSid -> reference pairing, replaces it, so A's webhooks for its own
    call are accepted and not refused as crossed."""
    ref_a = _prebound(TENANT_A, ACCOUNT_A)
    ref_b = _prebound(TENANT_B, ACCOUNT_B)
    planted = {"CallSid": SID_A, "AccountSid": ACCOUNT_B, "To": "+1"}
    asyncio.run(ca.twilio_webhook(_signed(ANSWER, planted, TOKEN["b"], query=f"neoh_ref={ref_b}")))
    assert json.loads(world.redis.values[f"twilio:call_state:{SID_A}"])["tenant_id"] == TENANT_B   # planted
    kept = asyncio.run(tch.initialize_twilio_call_state(
        SID_A, "+1", tenant_id=TENANT_A, account_sid=ACCOUNT_A, correlation=ref_a, keep_existing=True))
    assert kept["tenant_id"] == TENANT_A and kept["correlation"] == ref_a
    stored = json.loads(world.redis.values[f"twilio:call_state:{SID_A}"])
    assert stored["tenant_id"] == TENANT_A and stored["account_sid"] == ACCOUNT_A
    own = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    assert asyncio.run(ca.twilio_status_webhook(_signed(STATUS, own, TOKEN["a"], query=f"neoh_ref={ref_a}"))).status_code == 204


def test_the_worker_keeps_a_state_its_own_reference_wrote(world):
    """The 24.2 early answer bound from the call's own reference is kept
    (it may already be streaming), never reset."""
    ref_a = _prebound(TENANT_A, ACCOUNT_A)
    early = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "To": "+1"}
    asyncio.run(ca.twilio_webhook(_signed(ANSWER, early, TOKEN["a"], query=f"neoh_ref={ref_a}")))
    key = f"twilio:call_state:{SID_A}"
    streaming = json.loads(world.redis.values[key])
    streaming["stage"] = "qwen_streaming"
    world.redis.values[key] = json.dumps(streaming)
    kept = asyncio.run(tch.initialize_twilio_call_state(
        SID_A, "+1", tenant_id=TENANT_A, account_sid=ACCOUNT_A, correlation=ref_a, keep_existing=True))
    assert kept["stage"] == "qwen_streaming"
    assert json.loads(world.redis.values[key])["stage"] == "qwen_streaming"


def test_a_reference_bound_elsewhere_is_refused_before_any_credential_is_read(world):
    """The pairing is refused from Redis alone: no brokerage credential is
    decrypted and no signature is checked for it."""
    correlation = _bound(SID_A, TENANT_A, ACCOUNT_A)
    foreign = {"CallSid": SID_X, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    request = _Request(foreign, f"neoh_ref={correlation}", signature="junk")
    assert _refused(ca.twilio_status_webhook(request)) == 503        # crossed, not "bad signature"
    assert world.loads == [] and world.session_reads == []


def test_resolving_never_binds_a_reference_that_serves_another_call(world):
    """resolve_outbound_twilio_call_state's own contract, whoever calls it:
    a reference already bound to one CallSid is never bound to another."""
    correlation = _bound(SID_A, TENANT_A, ACCOUNT_A)
    assert asyncio.run(tch.resolve_outbound_twilio_call_state(SID_X, correlation)) is None
    assert f"twilio:call_state:{SID_X}" not in world.redis.values
    assert asyncio.run(tch.resolve_outbound_twilio_call_state(SID_A, correlation))["tenant_id"] == TENANT_A


def test_the_session_read_names_exactly_one_brokerage(world):
    """Two brokerages' sessions naming one CallSid would make the account
    ambiguous: the read answers only when exactly one brokerage is named (the
    in-memory store cannot evaluate SQL, so the predicate is pinned here)."""
    world.sessions[SID_P] = TENANT_P
    form = {"CallSid": SID_P, "AccountSid": PLATFORM, "CallStatus": "completed"}
    assert asyncio.run(ca.twilio_status_webhook(_signed(STATUS, form, TOKEN["platform"]))).status_code == 204
    sql, args = world.session_reads[0]
    assert "count(DISTINCT tenant_id) = 1" in sql and "live_call_sessions" in sql and args == (SID_P,)

def test_the_first_verified_webhook_binds_an_unbound_reference(world):
    """An early status (before the worker and the answer) binds the
    reference, so a later pairing with another CallSid is refused; the
    worker, which has Twilio's own CallSid, corrects a wrong early binding."""
    correlation = _prebound(TENANT_A, ACCOUNT_A)
    ref = f"neoh_ref={correlation}"
    early = {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "CallStatus": "ringing"}
    assert asyncio.run(ca.twilio_status_webhook(_signed(STATUS, early, TOKEN["a"], query=ref))).status_code == 204
    assert asyncio.run(tch.twilio_correlation_call_sid(correlation)) == SID_A
    late = {"CallSid": SID_X, "AccountSid": ACCOUNT_A, "CallStatus": "completed"}
    assert _refused(ca.twilio_status_webhook(_signed(STATUS, late, TOKEN["a"], query=ref))) == 503

    other = _prebound(TENANT_A, ACCOUNT_A)
    assert asyncio.run(tch.bind_twilio_correlation(other, SID_X)) is True       # a wrong early binding
    asyncio.run(tch.initialize_twilio_call_state(
        SID_B, "+1", tenant_id=TENANT_A, account_sid=ACCOUNT_A, correlation=other, keep_existing=True))
    assert asyncio.run(tch.twilio_correlation_call_sid(other)) == SID_B


# ── PR #182 review: nothing is waited for before the signature ───────────


def _counting(world, monkeypatch):
    reads = []
    real_get = world.redis.get

    async def get(key):
        reads.append(key)
        return await real_get(key)

    world.redis.get = get
    monkeypatch.setattr(tch, "_STATE_WAIT_SECONDS", 5.0)    # any wait would show
    return reads


def test_a_bad_signature_on_a_known_call_is_400_without_waiting(world, monkeypatch):
    correlation = _prebound(TENANT_A, ACCOUNT_A)
    reads = _counting(world, monkeypatch)
    request = _Request({"CallSid": SID_A, "AccountSid": ACCOUNT_A}, f"neoh_ref={correlation}", signature="junk")
    started = time.monotonic()
    assert _refused(ca.twilio_webhook(request)) == 400
    assert time.monotonic() - started < 1.0 and len(reads) <= 3


def test_a_junk_request_for_an_unknown_call_is_refused_without_waiting(world, monkeypatch):
    reads = _counting(world, monkeypatch)
    started = time.monotonic()
    assert _refused(ca.twilio_webhook(_Request({"CallSid": SID_X}, "", signature="junk"))) == 503
    assert time.monotonic() - started < 1.0 and len(reads) <= 2


# ── PR #182 review: a brokerage's credential is used whole everywhere ────


@pytest.fixture()
def platform_key(monkeypatch):
    """The platform authenticates with an API key pair (as production may)."""
    key = "SK" + "7" * 32
    monkeypatch.setenv("TWILIO_API_KEY", key)
    return key


@pytest.fixture()
def built(monkeypatch):
    """Every Twilio REST client built: the credential it was built from."""
    seen = []

    class _Stop(Exception):
        pass

    def client(account_sid, auth_token, api_key, api_secret):
        seen.append((account_sid, auth_token, api_key, api_secret))
        raise _Stop()

    monkeypatch.setattr(command_providers, "_twilio_client", client)
    return SimpleNamespace(seen=seen, Stop=_Stop)


BYO = {"account_sid": ACCOUNT_A}


def _byo():
    return {**BYO, "auth_token": TOKEN["a"]}


def test_a_byo_call_is_placed_with_the_brokerages_own_credential_only(world, built, platform_key, monkeypatch):
    monkeypatch.setenv("ORACLE_TWILIO_ACCOUNT_TIER", "full")
    draft = {"target": {"phone": "+15555550101"}}
    with pytest.raises(built.Stop):
        asyncio.run(command_providers.place_twilio_call(draft, credentials={**_byo(), "from_number": "+15555550100"}))
    assert built.seen == [(ACCOUNT_A, TOKEN["a"], "", "")]
    built.seen.clear()
    with pytest.raises(built.Stop):
        asyncio.run(command_providers.place_twilio_call(draft, credentials={"from_number": "+15555550100"}))
    assert built.seen == [(PLATFORM, TOKEN["platform"], platform_key, os.environ["TWILIO_API_SECRET"])]


def test_the_workers_abort_uses_the_brokerages_own_credential_only(world, built, platform_key):
    asyncio.run(command_providers.abort_twilio_call(SID_A, credentials={**_byo(), "from_number": "+1"}))
    assert built.seen == [(ACCOUNT_A, TOKEN["a"], "", "")]



@pytest.mark.parametrize("stored", ["byo", "none", "no_account"])
def test_the_worker_writes_the_account_it_then_dials_from(monkeypatch, platform_key, stored):
    """The approved-call worker records, before placing the call, the account
    place_twilio_call then dials with (whole_twilio_credential): the
    brokerage's own for a BYO credential, else the platform's — so the
    webhooks' account is the call's."""
    import twilio_call_handler
    from tests.test_provider_refused_before_send import Network, _twilio_env, _worker

    _twilio_env(monkeypatch)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", BASE)
    _conn, run = _worker(monkeypatch, "call_twilio")
    written = []

    async def correlation(_ref, _callee, *, tenant_id, account_sid, command_id=""):
        written.append(account_sid)

    async def credential(_ctx, provider, **_k):
        if provider != "twilio" or stored == "none":
            return None
        return json.dumps(_byo() if stored == "byo" else {"auth_token": TOKEN["a"]})

    class _Stop(Exception):
        pass

    dialled = []

    def client(account_sid, auth_token, api_key, api_secret):
        dialled.append((account_sid, auth_token, api_key))
        raise _Stop()

    monkeypatch.setattr(twilio_call_handler, "initialize_outbound_twilio_correlation", correlation)
    monkeypatch.setattr(ca, "_load_provider_credential", credential)
    Network(monkeypatch, twilio=client)
    with contextlib.suppress(Exception):
        asyncio.run(run())
    if stored == "byo":
        assert written == [ACCOUNT_A] and dialled == [(ACCOUNT_A, TOKEN["a"], "")]
    elif stored == "no_account":
        # A stored credential without its account is not the platform's:
        # nothing is written for, or dialled from, the platform account.
        assert written == [""] and dialled == []
    else:
        platform = os.environ["TWILIO_ACCOUNT_SID"]
        assert written == [platform] and dialled == [(platform, os.environ["TWILIO_AUTH_TOKEN"], platform_key)]

@pytest.mark.parametrize("call", [
    lambda c: command_providers.send_twilio_sms(
        {"target": {"phone": "+15555550101"}, "body": "hi"},
        credentials={**c, "sms_sender": "+15555550100", "sms_sender_type": "twilio_registered"}),
    lambda c: command_providers.start_twilio_caller_id_verification("+15555550101", credentials=c),
    lambda c: command_providers.check_twilio_caller_id_verified("+15555550101", credentials=c),
    lambda c: command_providers.provision_twilio_forwarding_number(credentials=c),
    lambda c: command_providers.release_twilio_number("PN" + "1" * 32, credentials=c),
    lambda c: command_providers.configure_twilio_number_webhook("PN" + "1" * 32, voice_url=f"{BASE}/v",
                                                                credentials=c),
], ids=["sms", "verify_start", "verify_check", "provision", "release", "configure"])
def test_every_twilio_request_uses_a_byo_credential_whole(world, built, platform_key, call):
    with pytest.raises(built.Stop):
        asyncio.run(call(_byo()))
    assert built.seen == [(ACCOUNT_A, TOKEN["a"], "", "")]


def test_the_tenant_credential_resolver_never_fills_a_byo_credential_from_env(world, platform_key, monkeypatch):
    import telephony_api
    from tenancy import Role, TenantContext

    monkeypatch.setenv("TWILIO_TWIML_APP_SID", "AP" + "3" * 32)

    async def load(ctx, provider, **_k):
        return world.stored.get(ctx.tenant_id) and json.dumps(world.stored[ctx.tenant_id])

    monkeypatch.setattr(telephony_api, "_load_provider_credential", load)
    byo = asyncio.run(telephony_api._twilio_credentials(
        TenantContext(agent_id="a@x.test", tenant_id=TENANT_A, role=Role.AGENT)))
    assert (byo["account_sid"], byo["auth_token"], byo["api_key"], byo["api_secret"], byo["twiml_app_sid"]) == \
        (ACCOUNT_A, TOKEN["a"], "", "", "")
    platform = asyncio.run(telephony_api._twilio_credentials(
        TenantContext(agent_id="a@x.test", tenant_id=TENANT_P, role=Role.AGENT)))
    assert (platform["account_sid"], platform["api_key"], platform["twiml_app_sid"]) == \
        (PLATFORM, platform_key, "AP" + "3" * 32)


@pytest.mark.parametrize("credential", ["matching", "other_account"])
def test_the_turn_limit_hang_up_uses_the_calls_own_credential(world, monkeypatch, bridge, rest, caplog, credential):
    url, ref = _mint(SID_A, TENANT_A, ACCOUNT_A, monkeypatch)

    class _LimitedBridge:
        def __init__(self, *_a):
            pass

        async def run(self):
            raise qwen_omni_realtime.QwenCallLimitReached("turn limit")

    monkeypatch.setattr(qwen_omni_realtime, "TwilioQwenRealtimeBridge", _LimitedBridge)
    token = tch.create_twilio_bridge_token(SID_A)
    socket = _Socket(_ws_sig(url, TOKEN["a"]), [_start(SID_A, ACCOUNT_A, token)])
    if credential == "other_account":
        # The handshake has passed; the credential changes before the hang-up.
        real = tch.authenticate_twilio_media_handshake

        async def handshake(media_ref, signature):
            sid = await real(media_ref, signature)
            world.stored[TENANT_A] = {"account_sid": ACCOUNT_B, "auth_token": TOKEN["b"]}
            return sid

        monkeypatch.setattr(tch, "authenticate_twilio_media_handshake", handshake)
    caplog.set_level(logging.WARNING)
    asyncio.run(ca.twilio_qwen_media(socket, ref))
    if credential == "matching":
        assert [b["credential"] for b in rest] == [(ACCOUNT_A, TOKEN["a"], "", "")]
        assert rest[0]["updates"] == [(SID_A, {"status": "completed"})]
    else:
        assert rest == [] and "transfer_credentials_unavailable" in caplog.text


def test_an_outbound_stream_that_cannot_be_bound_ends_politely(world, monkeypatch):
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")
    correlation = _bound(SID_A, TENANT_A, ACCOUNT_A)

    async def unbindable(_sid):
        raise tch.TwilioCallStateUnavailable("Twilio media binding could not be stored.")

    monkeypatch.setattr(tch, "create_twilio_media_stream_url", unbindable)
    response = asyncio.run(ca.twilio_webhook(_signed(
        ANSWER, {"CallSid": SID_A, "AccountSid": ACCOUNT_A, "To": "+1"}, TOKEN["a"], query=f"neoh_ref={correlation}")))
    body = response.body.decode()
    assert response.status_code == 200 and "<Hangup/>" in body and "<Stream" not in body
    assert "unavailable" in body


def test_no_voice_provider_advertises_the_removed_bare_media_route():
    assert not any(hasattr(cls, "media_websocket_path") for cls in (
        voice_provider.VoiceProvider, voice_provider.TwilioVoiceProvider, voice_provider.PlivoVoiceProvider))

def test_no_migration_allows_a_plivo_provider_credential():
    """Plivo REST actions read a brokerage credential (commands_api, the
    telephony routes) but provider_credentials cannot hold one: the latest
    CHECK (0104) allows no 'plivo', so those reads are dead and the Plivo half
    of 23.3 is not a defect. Whoever adds bring-your-own Plivo must first
    thread that credential through _say_refused_and_end, the Plivo media
    abort in commands_api, elevenlabs_realtime and qwen_omni_realtime's
    _redirect_to_agent, as 23.3 did for Twilio."""
    definitions = []
    for path in sorted((BACKEND / "db" / "migrations").glob("*.sql")):
        for body in re.findall(
                r"ADD\s+CONSTRAINT\s+provider_credentials_provider_check\s+CHECK\s*\((.*?)\)\s*;",
                path.read_text(), flags=re.S | re.I):
            definitions.append((path.name, body))
    assert definitions, "provider_credentials_provider_check not found"
    latest_name, latest = definitions[-1]
    assert latest_name.startswith("0104_"), latest_name
    assert "'twilio'" in latest and "plivo" not in latest.lower()
