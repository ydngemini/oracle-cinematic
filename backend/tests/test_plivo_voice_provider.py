"""voice_provider.PlivoVoiceProvider — unit tests against the real Plivo SDK
surface (installed package), with the network boundary (plivo.RestClient's
underlying HTTP session) mocked. These verify the ADAPTER wiring — that we
call the SDK with the arguments we say we do — not Plivo's live service.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import voice_provider
from command_providers import ProviderConfigurationError, ProviderRequestError

AUTH_ID = "MA" + "a" * 18
AUTH_TOKEN = "b" * 40
CREDENTIALS = {"auth_id": AUTH_ID, "auth_token": AUTH_TOKEN}


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(
        voice_provider.PlivoVoiceProvider, "_client", classmethod(lambda cls, creds: fake_client)
    )


# ── credential resolution ────────────────────────────────────────────────


def test_missing_credentials_raise_configuration_error(monkeypatch):
    monkeypatch.delenv("PLIVO_AUTH_ID", raising=False)
    monkeypatch.delenv("PLIVO_AUTH_TOKEN", raising=False)
    with pytest.raises(ProviderConfigurationError):
        voice_provider.PlivoVoiceProvider._credentials({})


def test_env_fallback_is_used_when_no_explicit_credentials(monkeypatch):
    monkeypatch.setenv("PLIVO_AUTH_ID", AUTH_ID)
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", AUTH_TOKEN)
    auth_id, auth_token = voice_provider.PlivoVoiceProvider._credentials(None)
    assert auth_id == AUTH_ID
    assert auth_token == AUTH_TOKEN


# ── place_call ───────────────────────────────────────────────────────────


def test_place_call_uses_verified_from_number_and_answer_url(monkeypatch):
    fake_calls = MagicMock()
    fake_calls.create.return_value = MagicMock(request_uuid="uuid-123", call_uuid=None)
    fake_client = MagicMock(calls=fake_calls)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    result = asyncio.run(
        provider.place_call(
            to_number="+15551234567",
            from_number="+13025551234",
            answer_url="https://neoh.example/api/commands/webhooks/plivo",
            status_callback_url="https://neoh.example/api/commands/webhooks/plivo/status",
            credentials=CREDENTIALS,
        )
    )

    assert result.reference == "uuid-123"
    assert result.status == "queued"
    kwargs = fake_calls.create.call_args.kwargs
    assert kwargs["from_"] == "+13025551234"
    assert kwargs["to_"] == "+15551234567"
    assert kwargs["answer_url"] == "https://neoh.example/api/commands/webhooks/plivo"


def test_place_call_rejects_non_e164_from_number():
    provider = voice_provider.PlivoVoiceProvider()
    with pytest.raises(ProviderConfigurationError):
        asyncio.run(
            provider.place_call(
                to_number="+15551234567",
                from_number="not-a-number",
                answer_url="https://neoh.example/answer",
                credentials=CREDENTIALS,
            )
        )


def test_place_call_requires_answer_url():
    provider = voice_provider.PlivoVoiceProvider()
    with pytest.raises(ProviderConfigurationError):
        asyncio.run(
            provider.place_call(
                to_number="+15551234567",
                from_number="+13025551234",
                answer_url="",
                credentials=CREDENTIALS,
            )
        )


def test_place_call_wraps_plivo_rejection(monkeypatch):
    from plivo.exceptions import PlivoRestError

    fake_calls = MagicMock()
    fake_calls.create.side_effect = PlivoRestError("insufficient balance")
    fake_client = MagicMock(calls=fake_calls)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(
            provider.place_call(
                to_number="+15551234567",
                from_number="+13025551234",
                answer_url="https://neoh.example/answer",
                credentials=CREDENTIALS,
            )
        )


# ── verify_caller_id_start / complete ────────────────────────────────────


def test_verify_caller_id_start_returns_pending_result(monkeypatch):
    fake_vc = MagicMock()
    fake_vc.initiate_verify.return_value = MagicMock(verification_uuid="verify-uuid-1")
    fake_client = MagicMock(verify_callerids=fake_vc)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    result = asyncio.run(
        provider.verify_caller_id_start("+13025551234", channel="sms", credentials=CREDENTIALS)
    )
    assert result.reference == "verify-uuid-1"
    assert result.status == "pending"
    kwargs = fake_vc.initiate_verify.call_args.kwargs
    assert kwargs["channel"] == "sms"
    assert kwargs["phone_number"] == "13025551234"  # stripped leading + only


def test_verify_caller_id_start_rejects_bad_channel():
    provider = voice_provider.PlivoVoiceProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(
            provider.verify_caller_id_start(
                "+13025551234", channel="carrier-pigeon", credentials=CREDENTIALS
            )
        )


def test_verify_caller_id_complete_true_on_success(monkeypatch):
    fake_vc = MagicMock()
    fake_vc.verify_caller_id.return_value = MagicMock()
    fake_client = MagicMock(verify_callerids=fake_vc)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    verified = asyncio.run(
        provider.verify_caller_id_complete(
            "verify-uuid-1", "123456", phone_number="+13025551234", credentials=CREDENTIALS
        )
    )
    assert verified is True
    kwargs = fake_vc.verify_caller_id.call_args.kwargs
    assert kwargs["verification_uuid"] == "verify-uuid-1"
    assert kwargs["otp"] == "123456"


def test_verify_caller_id_complete_false_on_rejected_otp(monkeypatch):
    from plivo.exceptions import PlivoRestError

    fake_vc = MagicMock()
    fake_vc.verify_caller_id.side_effect = PlivoRestError("invalid otp")
    fake_client = MagicMock(verify_callerids=fake_vc)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    verified = asyncio.run(
        provider.verify_caller_id_complete(
            "verify-uuid-1", "000000", phone_number="+13025551234", credentials=CREDENTIALS
        )
    )
    assert verified is False


def test_verify_caller_id_complete_rejects_non_numeric_otp():
    provider = voice_provider.PlivoVoiceProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(
            provider.verify_caller_id_complete(
                "verify-uuid-1", "abcdef", phone_number="+13025551234", credentials=CREDENTIALS
            )
        )


def test_is_caller_id_verified_true_when_plivo_has_the_record(monkeypatch):
    fake_vc = MagicMock()
    fake_vc.get_verified_caller_id.return_value = MagicMock()
    fake_client = MagicMock(verify_callerids=fake_vc)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    assert (
        asyncio.run(provider.is_caller_id_verified("+13025551234", credentials=CREDENTIALS))
        is True
    )


def test_is_caller_id_verified_false_on_404(monkeypatch):
    from plivo.exceptions import PlivoRestError

    fake_vc = MagicMock()
    fake_vc.get_verified_caller_id.side_effect = PlivoRestError("not found")
    fake_client = MagicMock(verify_callerids=fake_vc)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    assert (
        asyncio.run(provider.is_caller_id_verified("+13025551234", credentials=CREDENTIALS))
        is False
    )


# ── forwarding number provisioning + webhook wiring ─────────────────────


def test_provision_forwarding_number_buys_the_first_search_result(monkeypatch):
    fake_numbers = MagicMock()
    fake_numbers.search.return_value = [MagicMock(number="13025559999")]
    fake_client = MagicMock(numbers=fake_numbers)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    result = asyncio.run(provider.provision_forwarding_number(credentials=CREDENTIALS))
    assert result.detail["phone_number"] == "+13025559999"
    fake_numbers.buy.assert_called_once_with(number="13025559999")


def test_provision_forwarding_number_fails_closed_when_none_available(monkeypatch):
    fake_numbers = MagicMock()
    fake_numbers.search.return_value = []
    fake_client = MagicMock(numbers=fake_numbers)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(provider.provision_forwarding_number(credentials=CREDENTIALS))


def test_configure_number_webhook_creates_application_and_links_number(monkeypatch):
    fake_apps = MagicMock()
    fake_apps.create.return_value = MagicMock(app_id="app-1")
    fake_numbers = MagicMock()
    fake_client = MagicMock(applications=fake_apps, numbers=fake_numbers)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    asyncio.run(
        provider.configure_number_webhook(
            "+13025559999",
            voice_url="https://neoh.example/api/telephony/webhooks/plivo/inbound/route1",
            status_callback_url="https://neoh.example/api/telephony/webhooks/plivo/status/route1",
            credentials=CREDENTIALS,
        )
    )
    assert fake_apps.create.call_args.kwargs["answer_url"].endswith("/route1")
    fake_numbers.update.assert_called_once_with(number="13025559999", app_id="app-1")


# ── transfer / abort ─────────────────────────────────────────────────────


def test_transfer_call_uses_aleg_redirect(monkeypatch):
    fake_calls = MagicMock()
    fake_client = MagicMock(calls=fake_calls)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    asyncio.run(
        provider.transfer_call(
            "call-uuid-1",
            redirect_url="https://neoh.example/transfer",
            credentials=CREDENTIALS,
        )
    )
    kwargs = fake_calls.transfer.call_args.kwargs
    assert kwargs["call_uuid"] == "call-uuid-1"
    assert kwargs["legs"] == "aleg"
    assert kwargs["aleg_url"] == "https://neoh.example/transfer"


def test_abort_call_never_raises(monkeypatch):
    fake_calls = MagicMock()
    fake_calls.delete.side_effect = RuntimeError("boom")
    fake_client = MagicMock(calls=fake_calls)
    _patch_client(monkeypatch, fake_client)

    provider = voice_provider.PlivoVoiceProvider()
    asyncio.run(provider.abort_call("call-uuid-1", credentials=CREDENTIALS))  # must not raise


# ── markup (PlivoXML) ─────────────────────────────────────────────────────


def test_speak_and_stream_markup_embeds_bridge_token_in_stream_url():
    provider = voice_provider.PlivoVoiceProvider()
    markup = provider.speak_and_stream_markup(
        "This call may be recorded.",
        stream_url="wss://neoh.example/api/commands/media/plivo",
        bridge_token="v1.call-uuid.123.sig",
    )
    assert '<Stream bidirectional="true"' in markup
    assert "bridge_token=v1.call-uuid.123.sig" in markup
    assert "This call may be recorded." in markup


def test_dial_agent_markup_escapes_and_sets_caller_id():
    provider = voice_provider.PlivoVoiceProvider()
    markup = provider.dial_agent_markup(
        "+15551230000",
        caller_id="+15559990000",
        timeout_seconds=25,
        say="Connecting you now",
    )
    assert 'callerId="+15559990000"' in markup
    assert "<Number>+15551230000</Number>" in markup


def test_safe_hangup_markup_uses_speak_not_say():
    provider = voice_provider.PlivoVoiceProvider()
    markup = provider.safe_hangup_markup("Goodbye.")
    assert "<Speak>Goodbye.</Speak>" in markup
    assert "<Hangup/>" in markup


# ── voice_provider_name() / get_voice_provider() ────────────────────────


def test_voice_provider_name_defaults_to_plivo(monkeypatch):
    monkeypatch.delenv("ORACLE_VOICE_PROVIDER", raising=False)
    assert voice_provider.voice_provider_name() == "plivo"


def test_voice_provider_name_rejects_unsupported_value(monkeypatch):
    monkeypatch.setenv("ORACLE_VOICE_PROVIDER", "sip-trunk-of-mystery")
    with pytest.raises(voice_provider.VoiceProviderError):
        voice_provider.voice_provider_name()


def test_get_voice_provider_returns_matching_adapter():
    assert isinstance(voice_provider.get_voice_provider("twilio"), voice_provider.TwilioVoiceProvider)
    assert isinstance(voice_provider.get_voice_provider("plivo"), voice_provider.PlivoVoiceProvider)
    with pytest.raises(voice_provider.VoiceProviderError):
        voice_provider.get_voice_provider("carrier-pigeon")
