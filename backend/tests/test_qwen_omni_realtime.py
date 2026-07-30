import asyncio
import base64
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import WebSocketDisconnect

import acs_call_handler
import commands_api
import qwen_omni_realtime
from qwen_omni_realtime import (
    QwenOmniRealtimeBridge,
    QwenRealtimeError,
    QwenRealtimeSettings,
    acs_audio_frame,
    acs_stop_audio_frame,
    verify_acs_websocket_jwt,
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


def _acs_token(
    private_key,
    *,
    audience: str,
    issuer: str = qwen_omni_realtime._ACS_JWT_ISSUER,
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "acs-media",
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_acs_websocket_jwt_validates_signature_issuer_and_audience(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    audience = (
        "/subscriptions/test/resourceGroups/neoh/providers/"
        "Microsoft.Communication/CommunicationServices/neoh-acs"
    )

    class _SigningKey:
        key = private_key.public_key()

    class _JwksClient:
        def get_signing_key_from_jwt(self, _token):
            return _SigningKey()

    monkeypatch.setenv("ORACLE_ACS_RESOURCE_ID", audience)
    monkeypatch.setattr(
        qwen_omni_realtime,
        "_acs_jwks_client",
        lambda: _JwksClient(),
    )

    assert verify_acs_websocket_jwt(
        f"Bearer {_acs_token(private_key, audience=audience)}"
    )
    assert not verify_acs_websocket_jwt(
        f"Bearer {_acs_token(private_key, audience='wrong-audience')}"
    )
    assert not verify_acs_websocket_jwt(
        f"Bearer {_acs_token(private_key, audience=audience, issuer='wrong-issuer')}"
    )
    assert not verify_acs_websocket_jwt("")


def test_acs_websocket_jwt_fails_closed_without_audience(monkeypatch):
    monkeypatch.delenv("ORACLE_ACS_RESOURCE_ID", raising=False)
    assert not verify_acs_websocket_jwt("Bearer token")


class _FakeAcsWebSocket:
    def __init__(
        self,
        *,
        authorization: str = "",
        call_connection_id: str = "",
        query_token: str = "",
    ) -> None:
        self.headers = {
            "authorization": authorization,
            "x-ms-call-connection-id": call_connection_id,
        }
        self.query_params = {"token": query_token} if query_token else {}
        self.accepted = False
        self.closed_code = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int) -> None:
        self.closed_code = code


def test_media_endpoint_rejects_legacy_query_token(monkeypatch):
    websocket = _FakeAcsWebSocket(
        call_connection_id="call-123",
        query_token="legacy-shared-secret",
    )
    monkeypatch.setattr(
        qwen_omni_realtime,
        "verify_acs_websocket_jwt",
        lambda _authorization: False,
    )

    asyncio.run(commands_api.acs_qwen_media(websocket))

    assert not websocket.accepted
    assert websocket.closed_code == 4403


def test_media_endpoint_requires_live_call_binding(monkeypatch):
    websocket = _FakeAcsWebSocket(
        authorization="Bearer signed-acs-token",
        call_connection_id="unknown-call",
    )
    monkeypatch.setattr(
        qwen_omni_realtime,
        "verify_acs_websocket_jwt",
        lambda _authorization: True,
    )

    async def reject_unknown_call(_call_connection_id):
        return False

    monkeypatch.setattr(
        acs_call_handler,
        "authorize_qwen_media_call",
        reject_unknown_call,
    )

    asyncio.run(commands_api.acs_qwen_media(websocket))

    assert not websocket.accepted
    assert websocket.closed_code == 4403


def test_media_endpoint_accepts_signed_jwt_for_live_call(monkeypatch):
    websocket = _FakeAcsWebSocket(
        authorization="Bearer signed-acs-token",
        call_connection_id="active-call",
    )
    monkeypatch.setattr(
        qwen_omni_realtime,
        "verify_acs_websocket_jwt",
        lambda _authorization: True,
    )

    async def accept_active_call(_call_connection_id):
        return True

    class _Bridge:
        def __init__(self, _websocket, call_connection_id):
            assert call_connection_id == "active-call"

        async def run(self):
            raise WebSocketDisconnect(code=1000)

    monkeypatch.setattr(
        acs_call_handler,
        "authorize_qwen_media_call",
        accept_active_call,
    )
    monkeypatch.setattr(qwen_omni_realtime, "QwenOmniRealtimeBridge", _Bridge)

    asyncio.run(commands_api.acs_qwen_media(websocket))

    assert websocket.accepted
    assert websocket.closed_code is None


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
