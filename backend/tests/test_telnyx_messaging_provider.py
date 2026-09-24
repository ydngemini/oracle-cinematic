"""messaging_provider.TelnyxMessagingProvider — unit tests against the real
Telnyx SDK surface (installed package), with the network boundary mocked.
Also proves the Ed25519 webhook signature helper genuinely rejects a bad
signature/altered payload, using real cryptography (PyNaCl), not a stub.
"""

from __future__ import annotations

import asyncio
import base64
import time
from unittest.mock import MagicMock

import pytest
from nacl.signing import SigningKey

import messaging_provider
from command_providers import ProviderConfigurationError, ProviderRejectedError, ProviderRequestError

API_KEY = "KEY_test_" + "a" * 20


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(
        messaging_provider.TelnyxMessagingProvider,
        "_client",
        classmethod(lambda cls, creds, public_key=None: fake_client),
    )


# ── send_message ──────────────────────────────────────────────────────────


def test_send_message_uses_long_code_and_verified_sender(monkeypatch):
    fake_messages = MagicMock()
    fake_messages.send_long_code.return_value = MagicMock(data=MagicMock(id="msg-uuid-1"))
    fake_client = MagicMock(messages=fake_messages)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    result = asyncio.run(
        provider.send_message(
            to="+15551234567", from_="+13025551234", text="hello", credentials={"api_key": API_KEY}
        )
    )
    assert result.reference == "msg-uuid-1"
    kwargs = fake_messages.send_long_code.call_args.kwargs
    assert kwargs["from_"] == "+13025551234"
    assert kwargs["to"] == "+15551234567"
    assert kwargs["text"] == "hello"
    assert kwargs["type"] == "SMS"
    assert "media_urls" not in kwargs  # omitted entirely, not passed as None


def test_send_message_mms_when_media_present(monkeypatch):
    fake_messages = MagicMock()
    fake_messages.send_long_code.return_value = MagicMock(data=MagicMock(id="msg-uuid-2"))
    fake_client = MagicMock(messages=fake_messages)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    asyncio.run(
        provider.send_message(
            to="+15551234567",
            from_="+13025551234",
            text="pic",
            media_urls=["https://example.test/a.jpg"],
            credentials={"api_key": API_KEY},
        )
    )
    kwargs = fake_messages.send_long_code.call_args.kwargs
    assert kwargs["type"] == "MMS"
    assert kwargs["media_urls"] == ["https://example.test/a.jpg"]


def test_send_message_rejects_non_e164_destination():
    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(
            provider.send_message(
                to="not-a-number", from_="+13025551234", text="hi", credentials={"api_key": API_KEY}
            )
        )


def test_send_message_requires_e164_sender():
    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderConfigurationError):
        asyncio.run(
            provider.send_message(
                to="+15551234567", from_="not-a-number", text="hi", credentials={"api_key": API_KEY}
            )
        )


def test_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    with pytest.raises(ProviderConfigurationError):
        messaging_provider.TelnyxMessagingProvider._api_key({})


# ── eligibility ───────────────────────────────────────────────────────────


def test_eligibility_maps_wireless_to_ineligible_wireless(monkeypatch):
    fake_orders = MagicMock()
    fake_orders.check_eligibility.return_value = MagicMock(
        phone_numbers=[
            MagicMock(phone_number="+13025551234", eligible_status="NUMBER_CAN_NOT_BE_WIRELESS", detail="wireless")
        ]
    )
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    results = asyncio.run(
        provider.check_number_eligibility(["+13025551234"], credentials={"api_key": API_KEY})
    )
    assert results[0].status == messaging_provider.ELIGIBILITY_INELIGIBLE_WIRELESS


def test_eligibility_maps_eligible_status(monkeypatch):
    fake_orders = MagicMock()
    fake_orders.check_eligibility.return_value = MagicMock(
        phone_numbers=[MagicMock(phone_number="+13025551234", eligible_status="ELIGIBLE", detail="ok")]
    )
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    results = asyncio.run(
        provider.check_number_eligibility(["+13025551234"], credentials={"api_key": API_KEY})
    )
    assert results[0].status == messaging_provider.ELIGIBILITY_ELIGIBLE


def test_eligibility_unrecognized_status_is_needs_review_not_a_guess(monkeypatch):
    fake_orders = MagicMock()
    fake_orders.check_eligibility.return_value = MagicMock(
        phone_numbers=[MagicMock(phone_number="+13025551234", eligible_status="SOMETHING_NEW", detail="?")]
    )
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    results = asyncio.run(
        provider.check_number_eligibility(["+13025551234"], credentials={"api_key": API_KEY})
    )
    assert results[0].status == messaging_provider.ELIGIBILITY_NEEDS_REVIEW


def test_eligibility_empty_provider_response_is_needs_review(monkeypatch):
    fake_orders = MagicMock()
    fake_orders.check_eligibility.return_value = MagicMock(phone_numbers=[])
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    results = asyncio.run(
        provider.check_number_eligibility(["+13025551234"], credentials={"api_key": API_KEY})
    )
    assert results[0].status == messaging_provider.ELIGIBILITY_NEEDS_REVIEW


# ── hosted messaging order + OTP verification ───────────────────────────


def test_begin_hosted_messaging_omits_unset_profile_id(monkeypatch):
    fake_orders = MagicMock()
    fake_orders.create.return_value = MagicMock(data=MagicMock(id="order-1", status="pending"))
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    result = asyncio.run(provider.begin_hosted_messaging("+13025551234", credentials={"api_key": API_KEY}))
    assert result.reference == "order-1"
    kwargs = fake_orders.create.call_args.kwargs
    assert "messaging_profile_id" not in kwargs


def test_start_ownership_verification_rejects_bad_method():
    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(
            provider.start_ownership_verification(
                "order-1", "+13025551234", method="carrier-pigeon", credentials={"api_key": API_KEY}
            )
        )


def test_complete_ownership_verification_true_on_success(monkeypatch):
    fake_orders = MagicMock()
    fake_orders.validate_codes.return_value = MagicMock()
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    verified = asyncio.run(
        provider.complete_ownership_verification(
            "order-1", "+13025551234", "123456", credentials={"api_key": API_KEY}
        )
    )
    assert verified is True
    kwargs = fake_orders.validate_codes.call_args
    assert kwargs.args[0] == "order-1"
    assert kwargs.kwargs["verification_codes"] == [{"phone_number": "+13025551234", "code": "123456"}]


def test_complete_ownership_verification_false_on_rejection(monkeypatch):
    from telnyx import APIStatusError

    fake_orders = MagicMock()
    fake_orders.validate_codes.side_effect = APIStatusError(
        "invalid code", response=MagicMock(status_code=422, request=MagicMock()), body=None
    )
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    verified = asyncio.run(
        provider.complete_ownership_verification(
            "order-1", "+13025551234", "000000", credentials={"api_key": API_KEY}
        )
    )
    assert verified is False


def test_complete_ownership_verification_rejects_non_numeric_code():
    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderRequestError):
        asyncio.run(
            provider.complete_ownership_verification(
                "order-1", "+13025551234", "abcdef", credentials={"api_key": API_KEY}
            )
        )


def test_upload_hosted_documents_omits_absent_file(monkeypatch):
    fake_actions = MagicMock()
    fake_orders = MagicMock(actions=fake_actions)
    fake_client = MagicMock(messaging_hosted_number_orders=fake_orders)
    _patch_client(monkeypatch, fake_client)

    provider = messaging_provider.TelnyxMessagingProvider()
    asyncio.run(
        provider.upload_hosted_documents("order-1", loa_bytes=b"%PDF-1.4", credentials={"api_key": API_KEY})
    )
    kwargs = fake_actions.upload_file.call_args.kwargs
    assert "loa" in kwargs
    assert "bill" not in kwargs


# ── webhook signature verification (real Ed25519) ───────────────────────


def _sign(sk: SigningKey, payload: str, timestamp: str) -> str:
    signed = f"{timestamp}|{payload}".encode()
    return base64.b64encode(sk.sign(signed).signature).decode()


def test_validate_webhook_accepts_a_genuine_signature():
    sk = SigningKey.generate()
    public_key = base64.b64encode(bytes(sk.verify_key)).decode()
    payload = '{"data":{"event_type":"message.received"}}'
    timestamp = str(int(time.time()))
    signature = _sign(sk, payload, timestamp)

    provider = messaging_provider.TelnyxMessagingProvider()
    event = provider.validate_webhook(
        payload,
        {"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp},
        credentials={"api_key": API_KEY, "public_key": public_key},
    )
    assert event.data.event_type == "message.received"


def test_validate_webhook_rejects_wrong_key_signature():
    signer = SigningKey.generate()
    wrong_key_owner = SigningKey.generate()
    public_key = base64.b64encode(bytes(wrong_key_owner.verify_key)).decode()
    payload = '{"data":{"event_type":"message.received"}}'
    timestamp = str(int(time.time()))
    signature = _sign(signer, payload, timestamp)  # signed by a DIFFERENT key

    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderRejectedError):
        provider.validate_webhook(
            payload,
            {"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp},
            credentials={"api_key": API_KEY, "public_key": public_key},
        )


def test_validate_webhook_rejects_altered_payload():
    sk = SigningKey.generate()
    public_key = base64.b64encode(bytes(sk.verify_key)).decode()
    original = '{"data":{"event_type":"message.received"}}'
    timestamp = str(int(time.time()))
    signature = _sign(sk, original, timestamp)
    tampered = '{"data":{"event_type":"message.finalized"}}'

    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderRejectedError):
        provider.validate_webhook(
            tampered,
            {"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp},
            credentials={"api_key": API_KEY, "public_key": public_key},
        )


def test_validate_webhook_rejects_missing_signature_header():
    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises((ProviderRejectedError, ValueError)):
        provider.validate_webhook(
            "{}", {}, credentials={"api_key": API_KEY, "public_key": "AAAA"}
        )


def test_validate_webhook_requires_public_key(monkeypatch):
    """No credential and no env var means refuse — not "fall through to the SDK".

    The env var has to be cleared explicitly. `validate_webhook` falls back to
    `TELNYX_PUBLIC_KEY`, and a developer machine with a real one in .env made
    this test take the configured path instead, where the SDK complains about a
    missing signature header. It passed alone and failed in the full suite,
    which is the worst combination: it reads as flakiness rather than as a test
    that never said what it depended on.
    """
    monkeypatch.delenv("TELNYX_PUBLIC_KEY", raising=False)
    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises(ProviderConfigurationError):
        provider.validate_webhook("{}", {}, credentials={"api_key": API_KEY})


def test_validate_webhook_rejects_stale_timestamp():
    sk = SigningKey.generate()
    public_key = base64.b64encode(bytes(sk.verify_key)).decode()
    payload = '{"data":{"event_type":"message.received"}}'
    old_timestamp = str(int(time.time()) - 1000)  # older than the 300s replay window
    signature = _sign(sk, payload, old_timestamp)

    provider = messaging_provider.TelnyxMessagingProvider()
    with pytest.raises((ProviderRejectedError, ValueError)):
        provider.validate_webhook(
            payload,
            {"telnyx-signature-ed25519": signature, "telnyx-timestamp": old_timestamp},
            credentials={"api_key": API_KEY, "public_key": public_key},
        )


# ── normalize_inbound_message / normalize_delivery_event ────────────────


def test_normalize_inbound_message_extracts_expected_fields():
    from types import SimpleNamespace

    event = SimpleNamespace(
        data=SimpleNamespace(
            event_type="message.received",
            payload=SimpleNamespace(
                id="msg-1",
                text="hello",
                type="SMS",
                from_=SimpleNamespace(phone_number="+15551234567"),
                to=[SimpleNamespace(phone_number="+13025551234")],
                media=[],
            ),
        )
    )
    provider = messaging_provider.TelnyxMessagingProvider()
    normalized = provider.normalize_inbound_message(event)
    assert normalized is not None
    assert normalized.from_e164 == "+15551234567"
    assert normalized.to_e164 == "+13025551234"
    assert normalized.text == "hello"
    assert normalized.provider_message_id == "msg-1"


def test_normalize_inbound_message_ignores_non_receive_events():
    from types import SimpleNamespace

    event = SimpleNamespace(data=SimpleNamespace(event_type="message.sent", payload=None))
    provider = messaging_provider.TelnyxMessagingProvider()
    assert provider.normalize_inbound_message(event) is None


def test_normalize_delivery_event_maps_delivered_status():
    from types import SimpleNamespace

    event = SimpleNamespace(
        data=SimpleNamespace(
            event_type="message.finalized",
            payload=SimpleNamespace(
                id="msg-2",
                to=[SimpleNamespace(status="delivered")],
                errors=[],
            ),
        )
    )
    provider = messaging_provider.TelnyxMessagingProvider()
    normalized = provider.normalize_delivery_event(event)
    assert normalized is not None
    assert normalized.status == messaging_provider.MSG_DELIVERED


def test_normalize_delivery_event_maps_failed_status_with_reason():
    from types import SimpleNamespace

    event = SimpleNamespace(
        data=SimpleNamespace(
            event_type="message.finalized",
            payload=SimpleNamespace(
                id="msg-3",
                to=[SimpleNamespace(status="delivery_failed")],
                errors=[SimpleNamespace(detail="carrier violation")],
            ),
        )
    )
    provider = messaging_provider.TelnyxMessagingProvider()
    normalized = provider.normalize_delivery_event(event)
    assert normalized is not None
    assert normalized.status == messaging_provider.MSG_FAILED
    assert normalized.error_reason == "carrier violation"


# ── messaging_provider_name() / get_messaging_provider() ────────────────


def test_messaging_provider_name_defaults_to_telnyx(monkeypatch):
    monkeypatch.delenv("ORACLE_MESSAGING_PROVIDER", raising=False)
    assert messaging_provider.messaging_provider_name() == "telnyx"


def test_messaging_provider_name_rejects_unsupported_value(monkeypatch):
    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "carrier-pigeon")
    with pytest.raises(messaging_provider.MessagingProviderError):
        messaging_provider.messaging_provider_name()


def test_get_messaging_provider_returns_matching_adapter():
    assert isinstance(
        messaging_provider.get_messaging_provider("twilio"), messaging_provider.TwilioMessagingProvider
    )
    assert isinstance(
        messaging_provider.get_messaging_provider("telnyx"), messaging_provider.TelnyxMessagingProvider
    )
    with pytest.raises(messaging_provider.MessagingProviderError):
        messaging_provider.get_messaging_provider("carrier-pigeon")
