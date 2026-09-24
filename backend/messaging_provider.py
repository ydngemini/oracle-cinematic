"""Carrier-neutral messaging (SMS/MMS) provider abstraction.

Mirrors voice_provider.py's shape deliberately, but is NOT merged into it —
voice (Plivo) and messaging (Telnyx) are separate rails per the migration
brief. Neoh keeps owning compliance, consent, CRM timeline, and AI drafting;
a MessagingProvider is a thin transport adapter underneath that.

TelnyxMessagingProvider is built and verified against the installed `telnyx`
4.180.0 SDK (every method/field name below was confirmed via
`inspect.signature`/`inspect.getsource` against the real installed package,
not from documentation alone — the SDK's public API changed generations
since most cached documentation/knowledge of a Stripe-style `telnyx.Message.
create()` interface; the actual current SDK is a typed `telnyx.Telnyx(...)`
client with resource objects, e.g. `client.messages.send(...)`).
"""

from __future__ import annotations

import logging
import os
import recovery_mode
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from command_providers import (
    ProviderConfigurationError,
    ProviderRejectedError,
    ProviderRequestError,
    ProviderResult,
)

logger = logging.getLogger("oracle.messaging_provider")

PROVIDER_TELNYX = "telnyx"
PROVIDER_TWILIO = "twilio"
SUPPORTED_MESSAGING_PROVIDERS = (PROVIDER_TELNYX, PROVIDER_TWILIO)

# Neoh's own vocabulary for message status — provider status strings are
# normalized into exactly these before anything else in the codebase sees
# them (mirrors voice_provider's CALL_* normalization).
MSG_QUEUED = "queued"
MSG_SENT = "sent"
MSG_DELIVERED = "delivered"
MSG_FAILED = "failed"
MSG_UNDELIVERED = "undelivered"

# Eligibility states — see migration 0108's CHECK constraint for the
# authoritative list. Telnyx's own enum strings are mapped into these; a
# status this module cannot confidently classify becomes "needs_review",
# never a guess.
ELIGIBILITY_UNKNOWN = "unknown"
ELIGIBILITY_ELIGIBLE = "eligible"
ELIGIBILITY_INELIGIBLE_WIRELESS = "ineligible_wireless"
ELIGIBILITY_INELIGIBLE_GOOGLE_VOICE = "ineligible_google_voice"
ELIGIBILITY_INELIGIBLE_PROVIDER = "ineligible_provider"
ELIGIBILITY_INELIGIBLE_NUMBER_TYPE = "ineligible_number_type"
ELIGIBILITY_INELIGIBLE_REGION = "ineligible_region"
ELIGIBILITY_NEEDS_REVIEW = "needs_review"


class MessagingProviderError(RuntimeError):
    """Raised when a messaging provider operation cannot complete safely."""


@dataclass(frozen=True)
class NormalizedInboundMessage:
    provider: str
    provider_message_id: str
    from_e164: str
    to_e164: str
    text: str
    media: list[dict[str, str]]
    message_type: str  # "SMS" or "MMS"


@dataclass(frozen=True)
class NormalizedDeliveryEvent:
    provider: str
    provider_message_id: str
    status: str  # one of MSG_*
    provider_status: str
    error_reason: Optional[str]


@dataclass(frozen=True)
class EligibilityResult:
    phone_number: str
    status: str  # one of ELIGIBILITY_*
    detail: str


def messaging_provider_name() -> str:
    name = os.getenv("ORACLE_MESSAGING_PROVIDER", PROVIDER_TELNYX).strip().lower()
    if name not in SUPPORTED_MESSAGING_PROVIDERS:
        raise MessagingProviderError(
            f"ORACLE_MESSAGING_PROVIDER={name!r} is not supported; use "
            + " or ".join(repr(p) for p in SUPPORTED_MESSAGING_PROVIDERS)
        )
    return name


def get_messaging_provider(name: str) -> "MessagingProvider":
    if name == PROVIDER_TELNYX:
        return TelnyxMessagingProvider()
    if name == PROVIDER_TWILIO:
        return TwilioMessagingProvider()
    raise MessagingProviderError(f"Unknown messaging provider {name!r}")


class MessagingProvider:
    """Base adapter. Concrete providers override every method below."""

    name: str = ""

    async def send_message(
        self,
        *,
        to: str,
        from_: str,
        text: str,
        media_urls: Optional[list[str]] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        raise NotImplementedError

    def validate_webhook(
        self,
        raw_body: str,
        headers: Mapping[str, str],
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Verify signature and return the parsed, typed event. Raises on
        missing/invalid signature — callers must not proceed on failure."""
        raise NotImplementedError

    def normalize_inbound_message(self, event: Any) -> Optional[NormalizedInboundMessage]:
        raise NotImplementedError

    def normalize_delivery_event(self, event: Any) -> Optional[NormalizedDeliveryEvent]:
        raise NotImplementedError

    async def check_number_eligibility(
        self,
        phone_numbers: list[str],
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> list[EligibilityResult]:
        raise NotImplementedError

    async def begin_hosted_messaging(
        self,
        phone_number: str,
        *,
        messaging_profile_id: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        raise NotImplementedError

    async def start_ownership_verification(
        self,
        order_id: str,
        phone_number: str,
        *,
        method: str = "sms",
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    async def complete_ownership_verification(
        self,
        order_id: str,
        phone_number: str,
        code: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        raise NotImplementedError

    async def upload_hosted_documents(
        self,
        order_id: str,
        *,
        loa_bytes: Optional[bytes] = None,
        invoice_bytes: Optional[bytes] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    async def get_hosted_order_status(
        self,
        order_id: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def disconnect_hosted_number(
        self,
        order_id: str,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    async def create_messaging_profile(
        self,
        name: str,
        *,
        webhook_url: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        raise NotImplementedError

    # ── 10DLC (Telnyx-specific; no-op/unsupported on legacy Twilio path) ────

    async def create_brand(
        self, fields: Mapping[str, Any], *, credentials: Optional[Mapping[str, Any]] = None
    ) -> ProviderResult:
        raise NotImplementedError

    async def get_brand_status(
        self, brand_id: str, *, credentials: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def submit_campaign(
        self,
        *,
        brand_id: str,
        description: str,
        usecase: str,
        credentials: Optional[Mapping[str, Any]] = None,
        **fields: Any,
    ) -> ProviderResult:
        raise NotImplementedError

    async def get_campaign_status(
        self, campaign_id: str, *, credentials: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def assign_number_to_campaign(
        self,
        campaign_id: str,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        raise NotImplementedError


class TwilioMessagingProvider(MessagingProvider):
    """Legacy path — wraps the existing send_twilio_sms unchanged. No hosted-
    number/10DLC support is implemented here; Twilio SMS in Neoh has always
    used a directly-registered/ported/toll-free-verified sender
    (telephony_routes.sms_sender_type), not a same-business-number hosting
    flow, and this migration does not change that.
    """

    name = PROVIDER_TWILIO

    async def send_message(
        self,
        *,
        to: str,
        from_: str,
        text: str,
        media_urls: Optional[list[str]] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        recovery_mode.guard("send_message via TwilioMessagingProvider")
        from command_providers import send_twilio_sms

        return await send_twilio_sms(
            {"target": {"phone": to}, "body": text},
            credentials={**(credentials or {}), "sms_sender": from_},
        )


class TelnyxMessagingProvider(MessagingProvider):
    """New: SMS/MMS send, Hosted Messaging, and 10DLC via the real Telnyx SDK.

    Confirmed against telnyx==4.180.0's actual resource surface — not the
    older Stripe-style interface some documentation/training data describes.
    """

    name = PROVIDER_TELNYX

    @staticmethod
    def _api_key(credentials: Optional[Mapping[str, Any]]) -> str:
        credentials = dict(credentials or {})
        api_key = str(
            credentials.get("api_key") or os.getenv("TELNYX_API_KEY", "")
        ).strip()
        if not api_key:
            raise ProviderConfigurationError("TELNYX_API_KEY is not configured")
        return api_key

    @classmethod
    def _client(cls, credentials: Optional[Mapping[str, Any]], *, public_key: Optional[str] = None):
        import telnyx

        api_key = cls._api_key(credentials)
        return telnyx.Telnyx(api_key=api_key, public_key=public_key)

    async def send_message(
        self,
        *,
        to: str,
        from_: str,
        text: str,
        media_urls: Optional[list[str]] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        recovery_mode.guard("send_message via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        if not to.startswith("+"):
            raise ProviderRequestError("approved SMS/MMS target must be E.164")
        if not from_.startswith("+"):
            raise ProviderConfigurationError("Telnyx sender must be E.164")

        def _send():
            client = self._client(credentials)
            # `text`/`media_urls` are typed `str | Omit` / `SequenceNotStr[str] | Omit`
            # in the SDK (no `None` in the union) — an unset field must be left out
            # of kwargs entirely, not passed as None, or the request serializer
            # sends a literal null the API does not expect.
            kwargs: dict[str, Any] = {"from_": from_, "to": to, "type": "MMS" if media_urls else "SMS"}
            if text:
                kwargs["text"] = text
            if media_urls:
                kwargs["media_urls"] = media_urls
            try:
                return client.messages.send_long_code(**kwargs)
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the message: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_send), timeout=25.0)
        data = getattr(response, "data", None)
        message_id = str(getattr(data, "id", "") or "")
        if not message_id:
            raise ProviderRequestError("Telnyx did not return a message id")
        return ProviderResult("telnyx", message_id, "queued", {"to": to, "from": from_})

    def validate_webhook(
        self,
        raw_body: str,
        headers: Mapping[str, str],
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Ed25519 signature verification via the SDK's own `webhooks.unwrap`
        helper — confirmed source: it checks telnyx-signature-ed25519 +
        telnyx-timestamp, enforces a 300s replay window, and raises
        ValueError on any failure. Never disabled in production."""
        public_key = str(
            (credentials or {}).get("public_key") or os.getenv("TELNYX_PUBLIC_KEY", "")
        ).strip()
        if not public_key:
            raise ProviderConfigurationError("TELNYX_PUBLIC_KEY is not configured")
        client = self._client(credentials, public_key=public_key)
        try:
            return client.webhooks.unwrap(raw_body, headers=dict(headers))
        except ValueError as exc:
            raise ProviderRejectedError(f"Invalid Telnyx webhook signature: {exc}") from exc

    def normalize_inbound_message(self, event: Any) -> Optional[NormalizedInboundMessage]:
        data = getattr(event, "data", None)
        if data is None or str(getattr(data, "event_type", "")) != "message.received":
            return None
        payload = getattr(data, "payload", None)
        if payload is None:
            return None
        from_obj = getattr(payload, "from_", None)
        from_number = str(getattr(from_obj, "phone_number", "") or "")
        to_field = getattr(payload, "to", None)
        to_number = ""
        if isinstance(to_field, list) and to_field:
            to_number = str(getattr(to_field[0], "phone_number", "") or "")
        elif isinstance(to_field, str):
            to_number = to_field
        media = [
            {
                "url": str(getattr(item, "url", "") or ""),
                "content_type": str(getattr(item, "content_type", "") or ""),
            }
            for item in (getattr(payload, "media", None) or [])
        ]
        message_id = str(getattr(payload, "id", "") or "")
        if not message_id or not from_number or not to_number:
            return None
        return NormalizedInboundMessage(
            provider=PROVIDER_TELNYX,
            provider_message_id=message_id,
            from_e164=from_number,
            to_e164=to_number,
            text=str(getattr(payload, "text", "") or ""),
            media=media,
            message_type=str(getattr(payload, "type", "") or "SMS"),
        )

    def normalize_delivery_event(self, event: Any) -> Optional[NormalizedDeliveryEvent]:
        data = getattr(event, "data", None)
        if data is None:
            return None
        event_type = str(getattr(data, "event_type", "") or "")
        if event_type not in ("message.sent", "message.finalized"):
            return None
        payload = getattr(data, "payload", None)
        message_id = str(getattr(payload, "id", "") or "")
        if not message_id:
            return None
        to_field = getattr(payload, "to", None) or []
        to_status = ""
        if isinstance(to_field, list) and to_field:
            to_status = str(getattr(to_field[0], "status", "") or "")
        status_map = {
            "delivered": MSG_DELIVERED,
            "sending_failed": MSG_FAILED,
            "delivery_failed": MSG_FAILED,
            "sent": MSG_SENT,
            "queued": MSG_QUEUED,
        }
        normalized = status_map.get(to_status, MSG_SENT if event_type == "message.sent" else MSG_FAILED)
        errors = getattr(payload, "errors", None) or []
        error_reason = str(getattr(errors[0], "detail", "") or "") if errors else None
        return NormalizedDeliveryEvent(
            provider=PROVIDER_TELNYX,
            provider_message_id=message_id,
            status=normalized,
            provider_status=to_status or event_type,
            error_reason=error_reason,
        )

    async def check_number_eligibility(
        self,
        phone_numbers: list[str],
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> list[EligibilityResult]:
        import asyncio

        from telnyx import APIStatusError

        def _check():
            client = self._client(credentials)
            try:
                return client.messaging_hosted_number_orders.check_eligibility(
                    phone_numbers=phone_numbers
                )
            except APIStatusError as exc:
                raise ProviderRequestError(f"Telnyx eligibility check failed: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_check), timeout=20.0)
        # Confirmed enum (telnyx.types.messaging_hosted_number_order_check_eligibility_response):
        # NUMBER_CAN_NOT_BE_WIRELESS, NUMBER_IS_NOT_A_US_NUMBER,
        # NUMBER_CAN_NOT_BE_IN_TELNYX, NUMBER_CAN_NOT_HOSTED_WITH_A_TELNYX_SUBSCRIBER,
        # NUMBER_CAN_NOT_BE_ACTIVE_IN_YOUR_ACCOUNT, NUMBER_CAN_NOT_BE_REPEATED,
        # NUMBER_CAN_NOT_BE_VALIDATED, NUMBER_IS_NOT_A_VALID_ROUTING_NUMBER,
        # NUMBER_IS_NOT_IN_E164_FORMAT, BILLING_ACCOUNT_CHECK_FAILED,
        # BILLING_ACCOUNT_IS_ABOLISHED, ELIGIBLE.
        status_map = {
            "ELIGIBLE": ELIGIBILITY_ELIGIBLE,
            "NUMBER_CAN_NOT_BE_WIRELESS": ELIGIBILITY_INELIGIBLE_WIRELESS,
            "NUMBER_IS_NOT_A_US_NUMBER": ELIGIBILITY_INELIGIBLE_REGION,
            "NUMBER_CAN_NOT_BE_IN_TELNYX": ELIGIBILITY_INELIGIBLE_PROVIDER,
            "NUMBER_CAN_NOT_HOSTED_WITH_A_TELNYX_SUBSCRIBER": ELIGIBILITY_INELIGIBLE_PROVIDER,
            "NUMBER_CAN_NOT_BE_ACTIVE_IN_YOUR_ACCOUNT": ELIGIBILITY_NEEDS_REVIEW,
            "NUMBER_CAN_NOT_BE_REPEATED": ELIGIBILITY_NEEDS_REVIEW,
            "NUMBER_CAN_NOT_BE_VALIDATED": ELIGIBILITY_NEEDS_REVIEW,
            "NUMBER_IS_NOT_A_VALID_ROUTING_NUMBER": ELIGIBILITY_INELIGIBLE_NUMBER_TYPE,
            "NUMBER_IS_NOT_IN_E164_FORMAT": ELIGIBILITY_NEEDS_REVIEW,
            "BILLING_ACCOUNT_CHECK_FAILED": ELIGIBILITY_NEEDS_REVIEW,
            "BILLING_ACCOUNT_IS_ABOLISHED": ELIGIBILITY_NEEDS_REVIEW,
        }
        results: list[EligibilityResult] = []
        for entry in getattr(response, "phone_numbers", None) or []:
            raw_status = str(getattr(entry, "eligible_status", "") or "")
            results.append(
                EligibilityResult(
                    phone_number=str(getattr(entry, "phone_number", "") or ""),
                    status=status_map.get(raw_status, ELIGIBILITY_NEEDS_REVIEW),
                    detail=str(getattr(entry, "detail", "") or raw_status),
                )
            )
        if not results:
            # Telnyx returned nothing usable — this is not the same as
            # "eligible"; represent it honestly as needs_review rather than
            # silently treating an empty response as a pass.
            results = [
                EligibilityResult(phone_number=number, status=ELIGIBILITY_NEEDS_REVIEW, detail="no eligibility data returned")
                for number in phone_numbers
            ]
        return results

    async def begin_hosted_messaging(
        self,
        phone_number: str,
        *,
        messaging_profile_id: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        recovery_mode.guard("begin_hosted_messaging via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        def _create():
            client = self._client(credentials)
            kwargs: dict[str, Any] = {"phone_numbers": [phone_number]}
            if messaging_profile_id:
                kwargs["messaging_profile_id"] = messaging_profile_id
            try:
                return client.messaging_hosted_number_orders.create(**kwargs)
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the hosted order: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_create), timeout=25.0)
        data = getattr(response, "data", None)
        order_id = str(getattr(data, "id", "") or "")
        status = str(getattr(data, "status", "") or "pending")
        if not order_id:
            raise ProviderRequestError("Telnyx did not return a hosted order id")
        return ProviderResult("telnyx_hosted_order", order_id, status, {"phone_number": phone_number})

    async def start_ownership_verification(
        self,
        order_id: str,
        phone_number: str,
        *,
        method: str = "sms",
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        recovery_mode.guard("start_ownership_verification via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        if method not in ("sms", "call"):
            raise ProviderRequestError("verification method must be sms or call")

        def _start():
            client = self._client(credentials)
            try:
                client.messaging_hosted_number_orders.create_verification_codes(
                    order_id, phone_numbers=[phone_number], verification_method=method
                )
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the verification request: {exc}") from exc

        await asyncio.wait_for(asyncio.to_thread(_start), timeout=20.0)

    async def complete_ownership_verification(
        self,
        order_id: str,
        phone_number: str,
        code: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        recovery_mode.guard("complete_ownership_verification via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        if not code or not code.strip().isdigit():
            raise ProviderRequestError("verification code must be numeric")

        def _validate() -> bool:
            client = self._client(credentials)
            try:
                client.messaging_hosted_number_orders.validate_codes(
                    order_id,
                    verification_codes=[{"phone_number": phone_number, "code": code.strip()}],
                )
            except APIStatusError as exc:
                logger.info("Telnyx hosted-number OTP rejected: %s", exc)
                return False
            return True

        return await asyncio.wait_for(asyncio.to_thread(_validate), timeout=20.0)

    async def upload_hosted_documents(
        self,
        order_id: str,
        *,
        loa_bytes: Optional[bytes] = None,
        invoice_bytes: Optional[bytes] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        recovery_mode.guard("upload_hosted_documents via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        if loa_bytes is None and invoice_bytes is None:
            raise ProviderRequestError("at least one of loa/invoice bytes is required")

        def _upload():
            client = self._client(credentials)
            kwargs: dict[str, Any] = {}
            if loa_bytes:
                kwargs["loa"] = ("loa.pdf", loa_bytes, "application/pdf")
            if invoice_bytes:
                kwargs["bill"] = ("invoice.pdf", invoice_bytes, "application/pdf")
            try:
                client.messaging_hosted_number_orders.actions.upload_file(order_id, **kwargs)
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the document upload: {exc}") from exc

        await asyncio.wait_for(asyncio.to_thread(_upload), timeout=30.0)

    async def get_hosted_order_status(
        self,
        order_id: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        import asyncio

        def _get():
            client = self._client(credentials)
            return client.messaging_hosted_number_orders.retrieve(order_id)

        response = await asyncio.wait_for(asyncio.to_thread(_get), timeout=15.0)
        data = getattr(response, "data", response)
        numbers = getattr(data, "phone_numbers", None) or []
        return {
            "status": str(getattr(data, "status", "") or ""),
            "phone_numbers": [
                {
                    "phone_number": str(getattr(n, "phone_number", "") or ""),
                    "status": str(getattr(n, "status", "") or ""),
                }
                for n in numbers
            ],
        }

    async def disconnect_hosted_number(
        self,
        order_id: str,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        recovery_mode.guard("disconnect_hosted_number via TelnyxMessagingProvider")
        import asyncio

        def _delete():
            client = self._client(credentials)
            try:
                client.messaging_hosted_numbers.delete(phone_number)
            except Exception:
                logger.exception("Failed to disconnect hosted number: %s", phone_number)

        await asyncio.wait_for(asyncio.to_thread(_delete), timeout=20.0)

    async def create_messaging_profile(
        self,
        name: str,
        *,
        webhook_url: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        recovery_mode.guard("create_messaging_profile via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        def _create():
            client = self._client(credentials)
            try:
                return client.messaging_profiles.create(
                    name=name,
                    whitelisted_destinations=["US"],
                    webhook_url=webhook_url,
                )
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the messaging profile: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_create), timeout=20.0)
        data = getattr(response, "data", response)
        profile_id = str(getattr(data, "id", "") or "")
        if not profile_id:
            raise ProviderRequestError("Telnyx did not return a messaging profile id")
        return ProviderResult("telnyx_messaging_profile", profile_id, "active", {"name": name})

    # ── 10DLC ────────────────────────────────────────────────────────────

    async def create_brand(
        self, fields: Mapping[str, Any], *, credentials: Optional[Mapping[str, Any]] = None
    ) -> ProviderResult:
        recovery_mode.guard("create_brand via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        required = ("country", "display_name", "email", "entity_type", "vertical")
        missing = [key for key in required if not fields.get(key)]
        if missing:
            raise ProviderRequestError(f"Brand registration missing required fields: {', '.join(missing)}")

        def _create():
            client = self._client(credentials)
            try:
                return client.messaging_10dlc.brand.create(**fields)
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the brand registration: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_create), timeout=25.0)
        brand_id = str(getattr(response, "brand_id", "") or getattr(response, "id", "") or "")
        if not brand_id:
            raise ProviderRequestError("Telnyx did not return a brand id")
        status = str(getattr(response, "identity_status", "") or "pending")
        return ProviderResult("telnyx_brand", brand_id, status, {})

    async def get_brand_status(
        self, brand_id: str, *, credentials: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        import asyncio

        def _get():
            client = self._client(credentials)
            return client.messaging_10dlc.brand.retrieve(brand_id)

        response = await asyncio.wait_for(asyncio.to_thread(_get), timeout=15.0)
        return {
            "status": str(getattr(response, "identity_status", "") or ""),
            "failure_reason": getattr(response, "failure_reason", None),
        }

    async def submit_campaign(
        self,
        *,
        brand_id: str,
        description: str,
        usecase: str,
        credentials: Optional[Mapping[str, Any]] = None,
        **fields: Any,
    ) -> ProviderResult:
        recovery_mode.guard("submit_campaign via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        def _submit():
            client = self._client(credentials)
            try:
                return client.messaging_10dlc.campaign_builder.submit(
                    brand_id=brand_id, description=description, usecase=usecase, **fields
                )
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the campaign submission: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_submit), timeout=25.0)
        campaign_id = str(getattr(response, "campaign_id", "") or getattr(response, "id", "") or "")
        if not campaign_id:
            raise ProviderRequestError("Telnyx did not return a campaign id")
        return ProviderResult("telnyx_campaign", campaign_id, "pending", {})

    async def get_campaign_status(
        self, campaign_id: str, *, credentials: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        import asyncio

        def _get():
            client = self._client(credentials)
            return client.messaging_10dlc.campaign.retrieve(campaign_id)

        response = await asyncio.wait_for(asyncio.to_thread(_get), timeout=15.0)
        return {
            "status": str(getattr(response, "campaign_status", "") or getattr(response, "status", "") or ""),
        }

    async def assign_number_to_campaign(
        self,
        campaign_id: str,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        recovery_mode.guard("assign_number_to_campaign via TelnyxMessagingProvider")
        import asyncio

        from telnyx import APIStatusError

        def _assign():
            client = self._client(credentials)
            try:
                return client.messaging_10dlc.phone_number_campaigns.create(
                    campaign_id=campaign_id, phone_number=phone_number
                )
            except APIStatusError as exc:
                raise ProviderRejectedError(f"Telnyx rejected the number assignment: {exc}") from exc

        response = await asyncio.wait_for(asyncio.to_thread(_assign), timeout=20.0)
        return ProviderResult("telnyx_number_campaign", phone_number, "assigned", {"campaign_id": campaign_id})
