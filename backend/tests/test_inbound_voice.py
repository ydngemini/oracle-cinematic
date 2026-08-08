import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

import inbound_voice
import telephony_api
import twilio_call_handler
from inbound_voice import InboundCallBinding
from qwen_omni_realtime import (
    QwenHandoffRequested,
    QwenRealtimeSettings,
    TwilioQwenRealtimeBridge,
)


CALL_SID = "CA" + ("a" * 32)
ACCOUNT_SID = "AC" + ("b" * 32)
STREAM_SID = "MZ" + ("c" * 32)
TENANT_ID = "11111111-1111-4111-8111-111111111111"
ROUTE_ID = "22222222-2222-4222-8222-222222222222"
CALL_ID = "33333333-3333-4333-8333-333333333333"
CONTACT_ID = "44444444-4444-4444-8444-444444444444"
CLIENT_ID = "55555555-5555-4555-8555-555555555555"
ENDPOINT_KEY = "66666666-6666-4666-8666-666666666666"


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)


class FakeRequest:
    def __init__(self, form, signature, query_params=None):
        self._form = FormData(form)
        self.headers = {
            "X-Twilio-Signature": signature,
            "host": "internal.invalid",
        }
        self.url = SimpleNamespace(netloc="internal.invalid", scheme="http")
        self.query_params = dict(query_params or {})

    async def form(self):
        return self._form


def start_event():
    return {
        "event": "start",
        "streamSid": STREAM_SID,
        "start": {
            "accountSid": ACCOUNT_SID,
            "callSid": CALL_SID,
            "streamSid": STREAM_SID,
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
            "customParameters": {"bridge_token": "bound"},
        },
    }


def test_intake_personas_are_exactly_three_questions_and_have_no_tools():
    buyer = inbound_voice.build_inbound_intake_instructions(
        {"direction": "inbound", "intake_mode": "buyer"}
    )
    seller = inbound_voice.build_inbound_intake_instructions(
        {"direction": "inbound", "intake_mode": "seller"}
    )

    assert len(inbound_voice.BUYER_INTAKE_QUESTIONS) == 3
    assert len(inbound_voice.SELLER_INTAKE_QUESTIONS) == 3
    assert all(question in buyer for question in inbound_voice.BUYER_INTAKE_QUESTIONS)
    assert all(question in seller for question in inbound_voice.SELLER_INTAKE_QUESTIONS)
    assert all(question not in buyer for question in inbound_voice.SELLER_INTAKE_QUESTIONS)
    assert all(question not in seller for question in inbound_voice.BUYER_INTAKE_QUESTIONS)
    assert "no MLS" in buyer
    assert "Do not search for properties" in buyer
    assert "Do not ask any other intake question" in buyer


def test_auto_intake_routes_once_then_uses_one_three_question_flow():
    instructions = inbound_voice.build_inbound_intake_instructions(
        {"direction": "inbound", "intake_mode": "auto"}
    )
    assert inbound_voice.INTAKE_ROUTING_QUESTION in instructions
    assert "Do not combine the flows" in instructions

    mode, answers = inbound_voice._resolve_mode_and_answers(
        "auto",
        [
            {"role": "caller", "text": "I am selling."},
            {"role": "caller", "text": "10 Main Street"},
            {"role": "caller", "text": "Within 60 days"},
            {"role": "caller", "text": "A clean, certain closing"},
        ],
    )
    assert mode == "seller"
    assert answers == {
        "property_address": "10 Main Street",
        "desired_timeline": "Within 60 days",
        "desired_outcome": "A clean, certain closing",
    }


def test_phone_lookup_hash_is_tenant_separated_and_never_plaintext(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-master-key")
    normalized = inbound_voice.normalize_e164("302-407-8981")
    first = inbound_voice.phone_lookup_hash(TENANT_ID, normalized)
    second = inbound_voice.phone_lookup_hash(
        "77777777-7777-4777-8777-777777777777", normalized
    )
    assert normalized == "+13024078981"
    assert first != second
    assert normalized not in first
    assert len(first) == 64


def test_inbound_redis_state_has_direction_and_no_caller_pii(monkeypatch):
    redis = MemoryRedis()

    async def get_redis():
        return redis

    monkeypatch.setattr(twilio_call_handler, "_get_redis", get_redis)
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")
    monkeypatch.setenv("ORACLE_TWILIO_ACCOUNT_TIER", "full")
    state = asyncio.run(
        twilio_call_handler.initialize_inbound_twilio_call_state(
            CALL_SID,
            "+18662805386",
            tenant_id=TENANT_ID,
            agent_id="agent@example.test",
            account_sid=ACCOUNT_SID,
            route_id=ROUTE_ID,
            voice_call_id=CALL_ID,
            intake_mode="seller",
            contact_id=CONTACT_ID,
            client_id=CLIENT_ID,
        )
    )

    assert state["direction"] == "inbound"
    assert state["stage"] == "disclosed"
    assert state["intake_mode"] == "seller"
    assert "caller" not in state
    assert "+13024078981" not in str(state)


def test_twilio_bridge_uses_inbound_persona_and_collects_both_sides():
    bridge = TwilioQwenRealtimeBridge(
        object(),
        CALL_SID,
        start_event(),
        settings=QwenRealtimeSettings(api_key="secret", workspace_id="workspace"),
    )
    bridge._call_state = {"direction": "inbound", "intake_mode": "buyer"}
    instructions = bridge._session_instructions()
    assert inbound_voice.BUYER_INTAKE_QUESTIONS[0] in instructions
    assert inbound_voice.SELLER_INTAKE_QUESTIONS[0] not in instructions

    asyncio.run(bridge._on_transcript_completed("assistant", "What is your target budget?"))
    asyncio.run(bridge._on_transcript_completed("caller", "About 400 thousand."))
    assert bridge._transcript == [
        {"role": "assistant", "text": "What is your target budget?"},
        {"role": "caller", "text": "About 400 thousand."},
    ]
    assert bridge._turns == 1


def test_route_model_forbids_verified_voice_id_as_implicit_sms_sender():
    base = {
        "inbound_did": "+18662805386",
        "twilio_account_sid": ACCOUNT_SID,
        "voice_caller_id_e164": "+13024078981",
        "voice_caller_id_verified": True,
    }
    route = telephony_api.TelephonyRouteUpsert(**base)
    assert route.sms_sender_e164 is None
    assert route.sms_sender_type is None

    with pytest.raises(ValidationError):
        telephony_api.TelephonyRouteUpsert(
            **base,
            sms_sender_e164="+13024078981",
        )


def test_signed_inbound_webhook_resolves_before_stream_and_discloses_first(monkeypatch):
    public_base = "https://api.example.test"
    suffix = f"/api/telephony/webhooks/twilio/inbound/{ENDPOINT_KEY}"
    webhook_url = public_base + suffix
    auth_token = "twilio-auth-token"
    form = {
        "CallSid": CALL_SID,
        "AccountSid": ACCOUNT_SID,
        "From": "+13024078981",
        "To": "+18662805386",
    }
    signature = RequestValidator(auth_token).compute_signature(webhook_url, form)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", public_base)
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")
    monkeypatch.setenv("ORACLE_TWILIO_ACCOUNT_TIER", "full")

    resolved = []

    async def resolve(endpoint, did, account):
        resolved.append((endpoint, did, account))
        return {
            "id": ROUTE_ID,
            "tenant_id": TENANT_ID,
            "agent_id": "agent@example.test",
            "twilio_account_sid": ACCOUNT_SID,
            "intake_mode": "buyer",
        }

    async def route_tokens(route):
        assert route["tenant_id"] == TENANT_ID
        assert route["twilio_account_sid"] == ACCOUNT_SID
        return [auth_token]

    async def prepare(route, *, call_sid, caller_phone):
        assert resolved
        return InboundCallBinding(
            call_id=CALL_ID,
            tenant_id=TENANT_ID,
            agent_id="agent@example.test",
            route_id=ROUTE_ID,
            contact_id=None,
            client_id=None,
            intake_mode="buyer",
        )

    async def initialize(*args, **kwargs):
        return {
            "direction": "inbound",
            "tenant_id": TENANT_ID,
            "agent_id": "agent@example.test",
            "intake_mode": "buyer",
            "qwen_realtime_enabled": True,
        }

    monkeypatch.setattr(telephony_api, "resolve_inbound_route", resolve)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "prepare_inbound_call", prepare)
    monkeypatch.setattr(
        telephony_api, "initialize_inbound_twilio_call_state", initialize
    )
    monkeypatch.setattr(
        telephony_api,
        "twilio_media_websocket_url",
        lambda: "wss://api.example.test/api/commands/media/twilio",
    )
    monkeypatch.setattr(
        telephony_api, "create_twilio_bridge_token", lambda _sid: "short-lived-token"
    )

    response = asyncio.run(
        telephony_api.twilio_inbound_webhook(
            ENDPOINT_KEY,
            FakeRequest(form, signature),
        )
    )
    body = response.body.decode("utf-8")
    assert resolved == [(ENDPOINT_KEY, "+18662805386", ACCOUNT_SID)]
    assert body.index("automated AI assistant") < body.index("<Connect>")
    assert "wss://api.example.test/api/commands/media/twilio" in body
    assert "short-lived-token" in body
    assert auth_token not in body


def test_bad_twilio_signature_fails_before_call_state_or_contact_work(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-auth-token")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    async def resolve(*args, **kwargs):
        return {
            "id": ROUTE_ID,
            "tenant_id": TENANT_ID,
            "agent_id": "agent@example.test",
            "twilio_account_sid": ACCOUNT_SID,
            "intake_mode": "buyer",
        }

    async def route_tokens(route):
        return ["twilio-auth-token"]

    async def should_not_prepare(*args, **kwargs):
        raise AssertionError("call state work ran before signature validation")

    monkeypatch.setattr(telephony_api, "resolve_inbound_route", resolve)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "prepare_inbound_call", should_not_prepare)
    form = {
        "CallSid": CALL_SID,
        "AccountSid": ACCOUNT_SID,
        "From": "+13024078981",
        "To": "+18662805386",
    }
    with pytest.raises(telephony_api.HTTPException) as exc_info:
        asyncio.run(
            telephony_api.twilio_inbound_webhook(
                ENDPOINT_KEY,
                FakeRequest(form, "invalid"),
            )
        )
    assert exc_info.value.status_code == 400


def test_opt_out_finalization_creates_compliance_task_and_suppresses_voice(monkeypatch):
    executed = []
    inserted_task = []

    class FakeConn:
        async def fetchrow(self, query, *args):
            if "SELECT id,client_id,contact_id" in query:
                return {
                    "id": CALL_ID,
                    "client_id": CLIENT_ID,
                    "contact_id": CONTACT_ID,
                    "intake_mode": "seller",
                    "callback_task_id": None,
                    "contact_intake_session_id": None,
                    "intake_handoff_task_id": None,
                }
            if "INSERT INTO client_tasks" in query:
                inserted_task.extend(args)
                return {"id": "88888888-8888-4888-8888-888888888888"}
            if "pgp_sym_encrypt" in query:
                return {"ct": b"encrypted"}
            raise AssertionError(query)

        async def execute(self, query, *args):
            executed.append((query, args))
            return "UPDATE 1"

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield FakeConn()

    async def fake_encrypt(_conn, _plaintext, _key):
        return b"encrypted"

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-master-key")
    monkeypatch.setattr(inbound_voice, "tenant_tx", fake_tx)
    monkeypatch.setattr(inbound_voice, "encrypt_pii", fake_encrypt)
    asyncio.run(
        inbound_voice.finalize_inbound_voice_call(
            CALL_SID,
            [{"role": "caller", "text": "Please do not call me again."}],
            {
                "direction": "inbound",
                "tenant_id": TENANT_ID,
                "agent_id": "agent@example.test",
            },
        )
    )

    assert "Process inbound do-not-contact request" in inserted_task
    assert any("UPDATE agent_contacts" in query for query, _ in executed)
    call_update = next(args for query, args in executed if "UPDATE inbound_voice_calls" in query)
    assert call_update[8] is True
    assert "do_not_contact" in call_update


# ── Live agent hand-off ──────────────────────────────────────────────────────
FORWARD_E164 = "+13025550147"


def test_handoff_detector_fires_on_human_requests_only():
    def caller(text):
        return [{"role": "caller", "text": text}]

    assert inbound_voice.requested_human_handoff(caller("Can I talk to a real person?"))
    assert inbound_voice.requested_human_handoff(caller("transfer me to an agent"))
    assert inbound_voice.requested_human_handoff(caller("I want to speak with a human"))
    assert inbound_voice.requested_human_handoff(caller("get me my realtor"))

    # Ordinary real-estate conversation must not trigger a live transfer.
    assert not inbound_voice.requested_human_handoff(
        caller("My agent said the listing agent would call back")
    )
    assert not inbound_voice.requested_human_handoff(caller("I am selling a house"))
    assert not inbound_voice.requested_human_handoff([])

    # The assistant offering a transfer is not the caller asking for one.
    assert not inbound_voice.requested_human_handoff(
        [{"role": "assistant", "text": "Would you like to speak to a person?"}]
    )


def test_opt_out_never_becomes_a_transfer():
    # "Do not call me" must suppress, not bridge the caller to the agent's cell.
    transcript = [{"role": "caller", "text": "Do not call me, take me off your list"}]
    assert inbound_voice._requested_opt_out(transcript)
    assert not inbound_voice.requested_human_handoff(transcript)


def test_route_model_requires_a_number_before_enabling_handoff():
    base = {"inbound_did": "+18662805386", "twilio_account_sid": ACCOUNT_SID}

    # Omitting the fields entirely (a client predating the feature) disables it.
    route = telephony_api.TelephonyRouteUpsert(**base)
    assert route.agent_forward_e164 is None
    assert route.forward_on_request is False
    assert route.forward_when_ai_unavailable is False

    # Explicitly asking for hand-off without a destination is an error.
    with pytest.raises(ValidationError):
        telephony_api.TelephonyRouteUpsert(**base, forward_on_request=True)

    # Forwarding to the DID itself would loop the call back into this webhook.
    with pytest.raises(ValidationError):
        telephony_api.TelephonyRouteUpsert(
            **base, agent_forward_e164="+18662805386"
        )

    configured = telephony_api.TelephonyRouteUpsert(
        **base, agent_forward_e164=FORWARD_E164, forward_timeout_seconds=30
    )
    assert configured.agent_forward_e164 == FORWARD_E164
    assert configured.forward_on_request is True
    assert configured.forward_timeout_seconds == 30

    with pytest.raises(ValidationError):
        telephony_api.TelephonyRouteUpsert(
            **base, agent_forward_e164=FORWARD_E164, forward_timeout_seconds=1
        )


def _inbound_webhook_response(monkeypatch, *, route_extra, qwen_enabled):
    public_base = "https://api.example.test"
    suffix = f"/api/telephony/webhooks/twilio/inbound/{ENDPOINT_KEY}"
    auth_token = "twilio-auth-token"
    form = {
        "CallSid": CALL_SID,
        "AccountSid": ACCOUNT_SID,
        "From": "+13024078981",
        "To": "+18662805386",
    }
    signature = RequestValidator(auth_token).compute_signature(public_base + suffix, form)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", public_base)

    async def resolve(_endpoint, _did, _account):
        return {
            "id": ROUTE_ID,
            "tenant_id": TENANT_ID,
            "agent_id": "agent@example.test",
            "twilio_account_sid": ACCOUNT_SID,
            "intake_mode": "buyer",
            **route_extra,
        }

    async def route_tokens(_route):
        return [auth_token]

    async def prepare(_route, *, call_sid, caller_phone):
        return InboundCallBinding(
            call_id=CALL_ID, tenant_id=TENANT_ID, agent_id="agent@example.test",
            route_id=ROUTE_ID, contact_id=None, client_id=None, intake_mode="buyer",
        )

    captured = {}

    async def initialize(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "direction": "inbound",
            "tenant_id": TENANT_ID,
            "agent_id": "agent@example.test",
            "intake_mode": "buyer",
            "qwen_realtime_enabled": qwen_enabled,
        }

    async def finalize(*_args, **_kwargs):
        return None

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(telephony_api, "resolve_inbound_route", resolve)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "prepare_inbound_call", prepare)
    monkeypatch.setattr(telephony_api, "initialize_inbound_twilio_call_state", initialize)
    monkeypatch.setattr(telephony_api, "finalize_inbound_voice_call", finalize)
    monkeypatch.setattr(telephony_api, "update_inbound_call_status", noop)
    monkeypatch.setattr(telephony_api, "record_forward_attempt", noop)
    monkeypatch.setattr(telephony_api, "twilio_qwen_enabled", lambda _state: qwen_enabled)

    response = asyncio.run(
        telephony_api.twilio_inbound_webhook(ENDPOINT_KEY, FakeRequest(form, signature))
    )
    return response.body.decode("utf-8"), captured


def test_unavailable_ai_forwards_the_caller_instead_of_dropping_them(monkeypatch):
    body, captured = _inbound_webhook_response(
        monkeypatch,
        route_extra={
            "agent_forward_e164": FORWARD_E164,
            "forward_on_request": True,
            "forward_when_ai_unavailable": True,
            "forward_timeout_seconds": 30,
            "voice_caller_id_e164": "+18662805386",
        },
        qwen_enabled=False,
    )
    assert "<Dial" in body
    assert FORWARD_E164 in body
    assert 'timeout="30"' in body
    assert "<Hangup/>" not in body.split("<Dial")[0]
    # The persona is only told about hand-off when hand-off is actually armed.
    assert captured["forward_available"] is True


def test_unavailable_ai_still_hangs_up_when_no_forward_number_is_set(monkeypatch):
    body, captured = _inbound_webhook_response(
        monkeypatch,
        route_extra={
            "agent_forward_e164": None,
            "forward_on_request": False,
            "forward_when_ai_unavailable": False,
        },
        qwen_enabled=False,
    )
    assert "<Dial" not in body
    assert "<Hangup/>" in body
    assert captured["forward_available"] is False


def test_transfer_webhook_dials_the_number_resolved_from_the_database(monkeypatch):
    public_base = "https://api.example.test"
    suffix = f"/api/telephony/webhooks/twilio/inbound/{ENDPOINT_KEY}/transfer"
    auth_token = "twilio-auth-token"
    form = {"CallSid": CALL_SID, "AccountSid": ACCOUNT_SID}
    signature = RequestValidator(auth_token).compute_signature(public_base + suffix, form)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", public_base)

    recorded = {}

    async def resolve_call_route(_endpoint, _sid, _account):
        return {"tenant_id": TENANT_ID, "twilio_account_sid": ACCOUNT_SID}

    async def route_tokens(_route):
        return [auth_token]

    async def resolve_target(call_sid, *, reason):
        recorded["reason"] = reason
        return {
            "forward_e164": FORWARD_E164,
            "timeout_seconds": 25,
            "caller_id": "+18662805386",
            "tenant_id": TENANT_ID,
            "endpoint_key": ENDPOINT_KEY,
        }

    async def record(call_sid, *, reason, outcome):
        recorded["outcome"] = outcome

    monkeypatch.setattr(telephony_api, "resolve_inbound_call_route", resolve_call_route)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "resolve_forward_target", resolve_target)
    monkeypatch.setattr(telephony_api, "record_forward_attempt", record)

    response = asyncio.run(
        telephony_api.twilio_inbound_transfer(
            ENDPOINT_KEY,
            FakeRequest(form, signature, {"reason": "caller_request"}),
        )
    )
    body = response.body.decode("utf-8")
    assert FORWARD_E164 in body
    assert 'callerId="+18662805386"' in body
    assert recorded == {"reason": "caller_request", "outcome": "requested"}


def test_transfer_webhook_never_leaks_a_disabled_route(monkeypatch):
    public_base = "https://api.example.test"
    suffix = f"/api/telephony/webhooks/twilio/inbound/{ENDPOINT_KEY}/transfer"
    auth_token = "twilio-auth-token"
    form = {"CallSid": CALL_SID, "AccountSid": ACCOUNT_SID}
    signature = RequestValidator(auth_token).compute_signature(public_base + suffix, form)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", public_base)

    async def resolve_call_route(_endpoint, _sid, _account):
        return {"tenant_id": TENANT_ID, "twilio_account_sid": ACCOUNT_SID}

    async def route_tokens(_route):
        return [auth_token]

    async def no_target(_call_sid, *, reason):
        return None

    monkeypatch.setattr(telephony_api, "resolve_inbound_call_route", resolve_call_route)
    monkeypatch.setattr(telephony_api, "_route_twilio_tokens", route_tokens)
    monkeypatch.setattr(telephony_api, "resolve_forward_target", no_target)

    response = asyncio.run(
        telephony_api.twilio_inbound_transfer(
            ENDPOINT_KEY, FakeRequest(form, signature, {"reason": "caller_request"})
        )
    )
    body = response.body.decode("utf-8")
    assert "<Dial" not in body
    assert "<Hangup/>" in body


def test_bridge_hands_off_once_and_stops_the_ai_session(monkeypatch):
    bridge = TwilioQwenRealtimeBridge(
        twilio_websocket=None,
        call_sid=CALL_SID,
        start_event=start_event(),
        settings=QwenRealtimeSettings(api_key="k", workspace_id="w"),
    )
    bridge._call_state = {"direction": "inbound", "forward_available": True}

    redirects = []

    async def fake_redirect():
        redirects.append(CALL_SID)

    monkeypatch.setattr(bridge, "_redirect_to_agent", fake_redirect)

    with pytest.raises(QwenHandoffRequested):
        asyncio.run(
            bridge._on_transcript_completed("caller", "Can I speak to a real person?")
        )
    assert redirects == [CALL_SID]

    # A second matching turn must not fire a second redirect.
    assert asyncio.run(bridge._maybe_hand_off()) is False
    assert redirects == [CALL_SID]


def test_bridge_does_not_hand_off_when_the_route_has_no_number(monkeypatch):
    bridge = TwilioQwenRealtimeBridge(
        twilio_websocket=None,
        call_sid=CALL_SID,
        start_event=start_event(),
        settings=QwenRealtimeSettings(api_key="k", workspace_id="w"),
    )
    bridge._call_state = {"direction": "inbound", "forward_available": False}

    async def should_not_redirect():
        raise AssertionError("redirect must not be attempted")

    monkeypatch.setattr(bridge, "_redirect_to_agent", should_not_redirect)
    bridge._transcript = [{"role": "caller", "text": "let me talk to a human"}]
    assert asyncio.run(bridge._maybe_hand_off()) is False


def test_failed_redirect_keeps_the_ai_on_the_call(monkeypatch):
    bridge = TwilioQwenRealtimeBridge(
        twilio_websocket=None,
        call_sid=CALL_SID,
        start_event=start_event(),
        settings=QwenRealtimeSettings(api_key="k", workspace_id="w"),
    )
    bridge._call_state = {"direction": "inbound", "forward_available": True}

    async def failing_redirect():
        raise RuntimeError("twilio is unreachable")

    monkeypatch.setattr(bridge, "_redirect_to_agent", failing_redirect)
    bridge._transcript = [{"role": "caller", "text": "let me talk to a human"}]
    assert asyncio.run(bridge._maybe_hand_off()) is False
    # Not latched: a later turn may retry rather than being stuck mid-handoff.
    assert bridge._handoff_started is False
