import base64
import pytest

from qwen_omni_realtime import (
    QwenOmniRealtimeBridge,
    QwenRealtimeError,
    QwenRealtimeSettings,
    acs_audio_frame,
    acs_stop_audio_frame,
    verify_media_token,
)


def test_international_workspace_url_uses_flash_realtime():
    settings = QwenRealtimeSettings(
        api_key="secret",
        workspace_id="ws-123",
        region="intl",
    )
    assert settings.websocket_url == (
        "wss://ws-123.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3.5-omni-flash-realtime"
    )


def test_beijing_workspace_url_and_override():
    settings = QwenRealtimeSettings(
        api_key="secret",
        workspace_id="ws-cn",
        region="cn",
        model="qwen3.5-omni-flash-realtime",
    )
    assert ".cn-beijing.maas.aliyuncs.com/" in settings.websocket_url

    override = QwenRealtimeSettings(
        api_key="secret",
        workspace_id="",
        base_url="wss://example.invalid/realtime",
        model="snapshot-model",
    )
    assert override.websocket_url == (
        "wss://example.invalid/realtime?model=snapshot-model"
    )


def test_settings_fail_closed_without_credentials():
    with pytest.raises(QwenRealtimeError):
        QwenRealtimeSettings(api_key="", workspace_id="ws").validate()
    with pytest.raises(QwenRealtimeError):
        QwenRealtimeSettings(api_key="secret", workspace_id="").validate()


def test_media_token_uses_configured_webhook_secret(monkeypatch):
    monkeypatch.setenv("ORACLE_ACS_WEBHOOK_SECRET", "media-secret")
    assert verify_media_token("media-secret")
    assert not verify_media_token("wrong")
    assert not verify_media_token("")


def test_acs_outbound_media_frames_match_wire_contract():
    pcm = b"\x00\x01\x02\x03"
    encoded = base64.b64encode(pcm).decode("ascii")
    assert acs_audio_frame(encoded) == {
        "Kind": "AudioData",
        "AudioData": {"Data": encoded},
        "StopAudio": None,
    }
    assert acs_stop_audio_frame() == {
        "Kind": "StopAudio",
        "AudioData": None,
        "StopAudio": {},
    }



def test_turn_limit_is_bounded(monkeypatch):
    settings = QwenRealtimeSettings(api_key="secret", workspace_id="ws")
    monkeypatch.setenv("QWEN_REALTIME_MAX_TURNS", "999")
    bridge = QwenOmniRealtimeBridge(object(), "call-123", settings=settings)
    assert bridge._max_turns == 80
