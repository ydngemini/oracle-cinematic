import asyncio
import audioop
import base64
import json
from types import SimpleNamespace

from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

import commands_api
import command_providers
import qwen_omni_realtime
import twilio_call_handler
from qwen_omni_realtime import (
    QwenRealtimeSettings,
    TwilioQwenRealtimeBridge,
    twilio_audio_frame,
    twilio_clear_audio_frame,
    twilio_mark_frame,
)


CALL_SID = "CA" + ("a" * 32)
ACCOUNT_SID = "AC" + ("b" * 32)
STREAM_SID = "MZ" + ("c" * 32)


def _start_event(*, bridge_token: str = "token") -> dict:
    return {
        "event": "start",
        "streamSid": STREAM_SID,
        "start": {
            "accountSid": ACCOUNT_SID,
            "callSid": CALL_SID,
            "streamSid": STREAM_SID,
            "tracks": ["inbound"],
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
            "customParameters": {"bridge_token": bridge_token},
        },
    }


class _MemoryRedis:
    def __init__(self):
        self.values = {}

    async def ping(self):
        return True

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)


class _BridgeWebSocket:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.sent = []

    async def receive_text(self):
        return self.messages.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)


class _QwenSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


def test_twilio_wire_frames_match_media_streams_contract():
    assert twilio_audio_frame(STREAM_SID, "YWJj") == {
        "event": "media",
        "streamSid": STREAM_SID,
        "media": {"payload": "YWJj"},
    }
    assert twilio_clear_audio_frame(STREAM_SID) == {
        "event": "clear",
        "streamSid": STREAM_SID,
    }
    assert twilio_mark_frame(STREAM_SID, "response-1") == {
        "event": "mark",
        "streamSid": STREAM_SID,
        "mark": {"name": "response-1"},
    }


def test_twilio_audio_is_converted_between_mulaw_8k_and_pcm(monkeypatch):
    pcm_8k = (b"\x00\x00\x10\x00\xf0\xff\x00\x00") * 40
    mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
    media_event = {
        "event": "media",
        "streamSid": STREAM_SID,
        "media": {"payload": base64.b64encode(mulaw_8k).decode("ascii")},
    }
    websocket = _BridgeWebSocket(
        [json.dumps(media_event), json.dumps({"event": "stop"})]
    )
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    bridge = TwilioQwenRealtimeBridge(
        websocket,
        CALL_SID,
        _start_event(),
        settings=settings,
    )
    qwen_socket = _QwenSocket()
    bridge.qwen_websocket = qwen_socket

    asyncio.run(bridge._acs_to_qwen())

    assert qwen_socket.sent[0]["type"] == "input_audio_buffer.append"
    pcm_16k = base64.b64decode(qwen_socket.sent[0]["audio"])
    assert len(pcm_16k) > len(pcm_8k)

    pcm_24k = (b"\x00\x00\x20\x00\xe0\xff\x00\x00") * 120
    asyncio.run(bridge._send_provider_audio(pcm_24k))
    outbound = websocket.sent[-1]
    assert outbound["event"] == "media"
    assert outbound["streamSid"] == STREAM_SID
    assert base64.b64decode(outbound["media"]["payload"])

    asyncio.run(bridge._clear_provider_audio())
    asyncio.run(bridge._mark_response_complete())
    assert websocket.sent[-2] == twilio_clear_audio_frame(STREAM_SID)
    assert websocket.sent[-1]["event"] == "mark"


def test_twilio_bridge_rejects_wrong_media_format():
    event = _start_event()
    event["start"]["mediaFormat"]["sampleRate"] = 16000
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    try:
        TwilioQwenRealtimeBridge(
            _BridgeWebSocket(),
            CALL_SID,
            event,
            settings=settings,
        )
    except qwen_omni_realtime.QwenRealtimeError as exc:
        assert "8 kHz" in str(exc)
    else:
        raise AssertionError("invalid Twilio media format was accepted")


def test_twilio_websocket_signature_uses_canonical_wss_url(monkeypatch):
    auth_token = "twilio-auth-token-for-tests"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_ENV", "production")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")
    url = "wss://api.example.test/api/commands/media/twilio"
    signature = RequestValidator(auth_token).compute_signature(url, {})

    assert twilio_call_handler.twilio_media_websocket_url() == url
    assert twilio_call_handler.verify_twilio_websocket_signature(signature)
    assert not twilio_call_handler.verify_twilio_websocket_signature("invalid")


def test_twilio_bridge_token_is_call_bound_and_expires(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "master-key-for-tests")
    token = twilio_call_handler.create_twilio_bridge_token(CALL_SID, now=1_000)

    assert twilio_call_handler.verify_twilio_bridge_token(
        token,
        CALL_SID,
        now=1_100,
    )
    assert not twilio_call_handler.verify_twilio_bridge_token(
        token,
        "CA" + ("d" * 32),
        now=1_100,
    )
    assert not twilio_call_handler.verify_twilio_bridge_token(
        token,
        CALL_SID,
        now=1_301,
    )


def test_twilio_media_authorization_requires_live_matching_state(monkeypatch):
    redis = _MemoryRedis()
    monkeypatch.setattr(twilio_call_handler, "_get_redis", lambda: _async_value(redis))
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "master-key-for-tests")
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")

    asyncio.run(
        twilio_call_handler.initialize_twilio_call_state(
            CALL_SID,
            "+15555550101",
            tenant_id="tenant-test",
            account_sid=ACCOUNT_SID,
        )
    )
    token = twilio_call_handler.create_twilio_bridge_token(CALL_SID)

    assert asyncio.run(
        twilio_call_handler.authorize_twilio_media(
            CALL_SID,
            ACCOUNT_SID,
            token,
        )
    )
    assert not asyncio.run(
        twilio_call_handler.authorize_twilio_media(
            CALL_SID,
            "AC" + ("e" * 32),
            token,
        )
    )


def test_twilio_realtime_is_disabled_for_voice_trial(monkeypatch):
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")
    monkeypatch.setenv("ORACLE_TWILIO_ACCOUNT_TIER", "trial")

    assert twilio_call_handler.twilio_qwen_enabled() is False


def test_twilio_realtime_is_enabled_after_upgrade(monkeypatch):
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")
    monkeypatch.setenv("ORACLE_TWILIO_ACCOUNT_TIER", "full")

    assert twilio_call_handler.twilio_qwen_enabled() is True


async def _async_value(value):
    return value


class _FakeRequest:
    def __init__(self, form, signature):
        self._form = FormData(form)
        self.headers = {"X-Twilio-Signature": signature}
        self.query_params = {}
        self.url = SimpleNamespace(netloc="internal", scheme="http")

    async def form(self):
        return self._form


class _EndpointWebSocket:
    def __init__(self, messages, signature="signed"):
        self.headers = {"x-twilio-signature": signature}
        self.messages = list(messages)
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, *, code):
        self.closed_code = code

    async def receive_text(self):
        return self.messages.pop(0)


def test_twiml_switches_approved_call_to_bidirectional_stream(monkeypatch):
    public_base = "https://api.example.test"
    webhook_url = f"{public_base}/api/commands/webhooks/twilio"
    auth_token = "twilio-auth-token-for-tests"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "in-progress",
        "To": "+15555550101",
    }
    signature = RequestValidator(auth_token).compute_signature(webhook_url, form)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", public_base)
    monkeypatch.setenv("ORACLE_ENV", "production")
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "master-key-for-tests")
    monkeypatch.setenv("ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "true")

    async def _load_state(_call_sid, *, wait_for_initialization=False):
        assert wait_for_initialization
        return {
            "account_sid": ACCOUNT_SID,
            "qwen_realtime_enabled": True,
        }

    monkeypatch.setattr(
        twilio_call_handler,
        "load_twilio_call_state",
        _load_state,
    )
    response = asyncio.run(commands_api.twilio_webhook(_FakeRequest(form, signature)))
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<Connect>" in body
    assert '<Stream url="wss://api.example.test/api/commands/media/twilio">' in body
    assert '<Parameter name="bridge_token"' in body
    assert "TWILIO_AUTH_TOKEN" not in body


def test_twilio_media_endpoint_rejects_bad_handshake_signature(monkeypatch):
    websocket = _EndpointWebSocket([], signature="invalid")
    monkeypatch.setattr(
        twilio_call_handler,
        "verify_twilio_websocket_signature",
        lambda _signature: False,
    )

    asyncio.run(commands_api.twilio_qwen_media(websocket))

    assert not websocket.accepted
    assert websocket.closed_code == 4403


def test_twilio_media_endpoint_binds_start_frame_to_live_call(monkeypatch):
    websocket = _EndpointWebSocket(
        [
            json.dumps(
                {"event": "connected", "protocol": "Call", "version": "1.0.0"}
            ),
            json.dumps(_start_event(bridge_token="bound-token")),
        ]
    )
    monkeypatch.setattr(
        twilio_call_handler,
        "verify_twilio_websocket_signature",
        lambda _signature: True,
    )

    async def _authorize(call_sid, account_sid, bridge_token):
        return (
            call_sid == CALL_SID
            and account_sid == ACCOUNT_SID
            and bridge_token == "bound-token"
        )

    async def _mark(_call_sid):
        return None

    class _Bridge:
        def __init__(self, _websocket, call_sid, start_event):
            assert call_sid == CALL_SID
            assert start_event["start"]["streamSid"] == STREAM_SID

        async def run(self):
            return None

    monkeypatch.setattr(
        twilio_call_handler,
        "authorize_twilio_media",
        _authorize,
    )
    monkeypatch.setattr(twilio_call_handler, "mark_twilio_streaming", _mark)
    monkeypatch.setattr(qwen_omni_realtime, "TwilioQwenRealtimeBridge", _Bridge)

    asyncio.run(commands_api.twilio_qwen_media(websocket))

    assert websocket.accepted
    assert websocket.closed_code is None


def test_twilio_calls_api_uses_separate_twiml_and_status_urls(monkeypatch):
    captured = {}

    class _Calls:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(sid=CALL_SID)

    class _Client:
        def __init__(self, *_args):
            self.calls = _Calls()

    import twilio.rest

    monkeypatch.setattr(twilio.rest, "Client", _Client)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15555550100")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.example.test")

    result = asyncio.run(
        command_providers.place_twilio_call(
            {"target": {"phone": "+15555550101"}}
        )
    )

    assert result.provider == "twilio"
    assert result.reference == CALL_SID
    assert captured["url"] == "https://api.example.test/api/commands/webhooks/twilio"
    assert (
        captured["status_callback"]
        == "https://api.example.test/api/commands/webhooks/twilio/status"
    )
    assert captured["status_callback_event"] == [
        "initiated",
        "ringing",
        "answered",
        "completed",
    ]
