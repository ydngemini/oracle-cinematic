"""PlivoQwenRealtimeBridge — audio format adapter and handshake validation.

Mirrors test_twilio_qwen_realtime.py's audio-conversion tests for Plivo's
wire format: inbound is 8 kHz mono G.711 mu-law (same as Twilio), but
OUTBOUND is 8 kHz mono LINEAR PCM (Plivo's playAudio default), not mu-law —
that asymmetry is the one real difference from Twilio's bridge and the
easiest place a copy-paste port would get wrong.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json

import pytest

import plivo_call_handler
import qwen_omni_realtime
from qwen_omni_realtime import (
    PlivoQwenRealtimeBridge,
    QwenRealtimeSettings,
    plivo_checkpoint_frame,
    plivo_clear_audio_frame,
    plivo_play_audio_frame,
)

CALL_UUID = "612ec2a1-d33c-11e8-816c-0630a5643bb6"
STREAM_ID = "788ec2a1-d33c-11e8-816c-0630a5643bb7"


def _start_event() -> dict:
    return {
        "event": "start",
        "streamId": STREAM_ID,
        "start": {
            "callId": CALL_UUID,
            "streamId": STREAM_ID,
            "accountId": "MA" + "a" * 18,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
        },
    }


class _BridgeWebSocket:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.sent = []

    async def receive_text(self):
        return self.messages.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)


def test_plivo_wire_frames_match_audio_streaming_contract():
    assert plivo_play_audio_frame(STREAM_ID, "YWJj") == {
        "event": "playAudio",
        "streamId": STREAM_ID,
        "media": {"contentType": "audio/x-l16", "sampleRate": 8000, "payload": "YWJj"},
    }
    assert plivo_clear_audio_frame(STREAM_ID) == {"event": "clearAudio", "streamId": STREAM_ID}
    assert plivo_checkpoint_frame(STREAM_ID, "response-1") == {
        "event": "checkpoint",
        "streamId": STREAM_ID,
        "name": "response-1",
    }


def test_plivo_inbound_audio_is_mulaw_decoded_and_upsampled():
    pcm_8k = (b"\x00\x00\x10\x00\xf0\xff\x00\x00") * 40
    mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)
    media_event = {
        "event": "media",
        "streamId": STREAM_ID,
        "media": {"track": "inbound", "payload": base64.b64encode(mulaw_8k).decode("ascii")},
    }
    websocket = _BridgeWebSocket([json.dumps(media_event), json.dumps({"event": "stop"})])
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    bridge = PlivoQwenRealtimeBridge(websocket, CALL_UUID, _start_event(), settings=settings)

    sent_to_qwen = []

    async def fake_send_qwen(event):
        sent_to_qwen.append(event)

    bridge._send_qwen = fake_send_qwen
    asyncio.run(bridge._acs_to_qwen())

    assert sent_to_qwen[0]["type"] == "input_audio_buffer.append"
    pcm_16k = base64.b64decode(sent_to_qwen[0]["audio"])
    assert len(pcm_16k) > len(pcm_8k)


def test_plivo_outbound_audio_is_linear_pcm_not_mulaw():
    """The one real gotcha: Plivo's playAudio default is linear PCM, so the
    outbound leg must NOT mu-law-encode — unlike Twilio's bridge, which does.
    """
    websocket = _BridgeWebSocket()
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    bridge = PlivoQwenRealtimeBridge(websocket, CALL_UUID, _start_event(), settings=settings)

    pcm_24k = (b"\x00\x00\x20\x00\xe0\xff\x00\x00") * 120
    asyncio.run(bridge._send_provider_audio(pcm_24k))

    outbound = websocket.sent[-1]
    assert outbound["event"] == "playAudio"
    assert outbound["media"]["contentType"] == "audio/x-l16"
    payload = base64.b64decode(outbound["media"]["payload"])
    # A mu-law-encoded frame would be half this length (1 byte/sample vs 2).
    pcm_8k_expected, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    assert len(payload) == len(pcm_8k_expected)


def test_plivo_clear_and_checkpoint_frames():
    websocket = _BridgeWebSocket()
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    bridge = PlivoQwenRealtimeBridge(websocket, CALL_UUID, _start_event(), settings=settings)

    asyncio.run(bridge._clear_provider_audio())
    asyncio.run(bridge._mark_response_complete())
    assert websocket.sent[-2] == plivo_clear_audio_frame(STREAM_ID)
    assert websocket.sent[-1]["event"] == "checkpoint"


def test_plivo_bridge_rejects_wrong_media_format():
    event = _start_event()
    event["start"]["mediaFormat"]["sampleRate"] = 16000
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    with pytest.raises(qwen_omni_realtime.QwenRealtimeError, match="8 kHz"):
        PlivoQwenRealtimeBridge(_BridgeWebSocket(), CALL_UUID, event, settings=settings)


def test_plivo_bridge_rejects_missing_stream_id():
    event = _start_event()
    del event["start"]["streamId"]
    del event["streamId"]
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="workspace")
    with pytest.raises(qwen_omni_realtime.QwenRealtimeError):
        PlivoQwenRealtimeBridge(_BridgeWebSocket(), CALL_UUID, event, settings=settings)


# ── bridge token: call-bound, short-lived, self-issued ──────────────────


def test_plivo_bridge_token_is_call_bound_and_expires(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-master-key")
    token = plivo_call_handler.create_plivo_bridge_token(CALL_UUID, now=1_000)
    assert plivo_call_handler.verify_plivo_bridge_token(token, CALL_UUID, now=1_010)
    assert not plivo_call_handler.verify_plivo_bridge_token(
        token, "788ec2a1-d33c-11e8-816c-0630a5643bb7", now=1_010
    )
    assert not plivo_call_handler.verify_plivo_bridge_token(token, CALL_UUID, now=100_000)


def test_plivo_bridge_token_rejects_malformed_call_uuid(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-master-key")
    with pytest.raises(ValueError):
        plivo_call_handler.create_plivo_bridge_token("not-a-uuid")
