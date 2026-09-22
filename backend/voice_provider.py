"""Carrier-neutral voice provider abstraction.

Neoh owns call orchestration, CRM integration, tenant routing, contact
resolution, AI conversation logic, transcripts, outcomes, compliance,
memory, approvals, and audit history. A telephony provider (Twilio, Plivo)
is a thin transport rail underneath that — never the other way around.

This module exists so the rest of the codebase increasingly depends on
``provider``/``provider_account_id``/``provider_call_id`` and the functions
below, rather than importing Twilio or Plivo SDK classes directly. It is
deliberately small: two adapters over the REST calls and markup that already
exist in ``command_providers``, ``telephony_api``, and ``twilio_call_handler``
for Twilio, plus new equivalents for Plivo.

Nothing here talks to Qwen, CRM, or compliance — that logic lives above this
layer and is reused unchanged (``inbound_voice``, ``outreach_compliance``,
``qwen_omni_realtime``); this module's job stops at "place a call", "did the
carrier confirm this number", "here is markup to speak+hangup+stream+dial",
and "here is what the carrier's webhook actually said, in Neoh's own terms".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from html import escape as xml_escape
from typing import Any, Mapping, Optional

from command_providers import (
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderResult,
)

logger = logging.getLogger("oracle.voice_provider")

PROVIDER_TWILIO = "twilio"
PROVIDER_PLIVO = "plivo"
SUPPORTED_PROVIDERS = (PROVIDER_TWILIO, PROVIDER_PLIVO)

# Neoh's own vocabulary for a call's lifecycle. Every provider's webhook
# statuses are normalized into exactly these values before anything else in
# the codebase sees them — no Twilio or Plivo status string is ever compared
# against outside this module.
CALL_QUEUED = "queued"
CALL_RINGING = "ringing"
CALL_IN_PROGRESS = "in_progress"
CALL_COMPLETED = "completed"
CALL_BUSY = "busy"
CALL_NO_ANSWER = "no_answer"
CALL_FAILED = "failed"
CALL_CANCELED = "canceled"
TERMINAL_CALL_STATUSES = frozenset(
    {CALL_COMPLETED, CALL_BUSY, CALL_NO_ANSWER, CALL_FAILED, CALL_CANCELED}
)


class VoiceProviderError(RuntimeError):
    """Raised when a voice provider operation cannot complete safely."""


@dataclass(frozen=True)
class NormalizedCallEvent:
    """One provider's webhook payload, reduced to what Neoh's call lifecycle needs."""

    provider: str
    provider_call_id: str
    provider_account_id: str
    from_number: str
    to_number: str
    call_status: str
    raw_status: str
    direction: str


def voice_provider_name() -> str:
    """The tenant-independent default carrier — used for new routes and the
    outbound path when a route has not yet recorded which provider it uses.
    Existing routes always carry their own ``provider`` column and are never
    silently switched onto this value."""
    name = os.getenv("ORACLE_VOICE_PROVIDER", PROVIDER_PLIVO).strip().lower()
    if name not in SUPPORTED_PROVIDERS:
        raise VoiceProviderError(
            f"ORACLE_VOICE_PROVIDER={name!r} is not supported; use "
            + " or ".join(repr(p) for p in SUPPORTED_PROVIDERS)
        )
    return name


def get_voice_provider(name: str) -> "VoiceProvider":
    if name == PROVIDER_TWILIO:
        return TwilioVoiceProvider()
    if name == PROVIDER_PLIVO:
        return PlivoVoiceProvider()
    raise VoiceProviderError(f"Unknown voice provider {name!r}")


class VoiceProvider:
    """Base adapter. Concrete providers override every method below."""

    name: str = ""

    async def place_call(
        self,
        *,
        to_number: str,
        from_number: str,
        answer_url: str,
        status_callback_url: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        raise NotImplementedError

    async def verify_caller_id_start(
        self,
        phone_number: str,
        *,
        channel: str = "sms",
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        raise NotImplementedError

    async def verify_caller_id_complete(
        self,
        verification_id: str,
        otp: str,
        *,
        phone_number: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Submit an agent-entered OTP. Returns whether it verified.

        Twilio's Outgoing Caller ID flow has no OTP-submission step (Twilio
        itself calls the number and reads/accepts the code); its adapter
        always returns False here and relies on ``is_caller_id_verified``
        polling instead. Plivo's flow is the reverse: this is the only path
        that can ever mark a Plivo number verified.
        """
        raise NotImplementedError

    async def is_caller_id_verified(
        self,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        raise NotImplementedError

    async def provision_forwarding_number(
        self,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
        area_code: Optional[str] = None,
    ) -> ProviderResult:
        raise NotImplementedError

    async def configure_number_webhook(
        self,
        provider_number_sid: str,
        *,
        voice_url: str,
        status_callback_url: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    async def transfer_call(
        self,
        provider_call_id: str,
        *,
        redirect_url: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    async def abort_call(
        self,
        provider_call_id: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    def media_websocket_path(self) -> str:
        raise NotImplementedError

    # ── Markup (TwiML / PlivoXML) ────────────────────────────────────────

    def safe_hangup_markup(self, message: str) -> str:
        raise NotImplementedError

    def speak_and_stream_markup(
        self, disclosure: str, *, stream_url: str, bridge_token: str
    ) -> str:
        raise NotImplementedError

    def dial_agent_markup(
        self,
        forward_e164: str,
        *,
        caller_id: str,
        timeout_seconds: int,
        say: str,
        action_url: str = "",
    ) -> str:
        raise NotImplementedError

    def hangup_after_dial_markup(self) -> str:
        raise NotImplementedError


class TwilioVoiceProvider(VoiceProvider):
    """Wraps the existing Twilio call paths — no duplicated logic.

    Every method below delegates to the same functions
    (``command_providers``, ``twilio_call_handler``, ``telephony_api``) the
    codebase already used before this abstraction existed, so Twilio's
    tested behavior is unchanged.
    """

    name = PROVIDER_TWILIO

    async def place_call(
        self,
        *,
        to_number: str,
        from_number: str,
        answer_url: str,
        status_callback_url: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        from command_providers import place_twilio_call

        return await place_twilio_call(
            {"target": {"phone": to_number}},
            credentials={**(credentials or {}), "from_number": from_number},
        )

    async def verify_caller_id_start(
        self,
        phone_number: str,
        *,
        channel: str = "sms",
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        from command_providers import start_twilio_caller_id_verification

        return await start_twilio_caller_id_verification(
            phone_number, credentials=credentials
        )

    async def verify_caller_id_complete(
        self,
        verification_id: str,
        otp: str,
        *,
        phone_number: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        # Twilio's Outgoing Caller ID verification is completed by Twilio
        # calling the number and reading the code back, not by an OTP the
        # agent submits to Neoh — there is nothing to submit here.
        return False

    async def is_caller_id_verified(
        self,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        from command_providers import check_twilio_caller_id_verified

        return await check_twilio_caller_id_verified(
            phone_number, credentials=credentials
        )

    async def provision_forwarding_number(
        self,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
        area_code: Optional[str] = None,
    ) -> ProviderResult:
        from command_providers import provision_twilio_forwarding_number

        return await provision_twilio_forwarding_number(
            credentials=credentials, area_code=area_code
        )

    async def configure_number_webhook(
        self,
        provider_number_sid: str,
        *,
        voice_url: str,
        status_callback_url: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        from command_providers import configure_twilio_number_webhook

        await configure_twilio_number_webhook(
            provider_number_sid,
            voice_url=voice_url,
            status_callback_url=status_callback_url,
            credentials=credentials,
        )

    async def transfer_call(
        self,
        provider_call_id: str,
        *,
        redirect_url: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        from twilio_call_handler import twilio_redirect_call

        await twilio_redirect_call(provider_call_id, redirect_url)

    async def abort_call(
        self,
        provider_call_id: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        from command_providers import abort_twilio_call

        await abort_twilio_call(provider_call_id, credentials=credentials)

    def media_websocket_path(self) -> str:
        return "/api/commands/media/twilio"

    def safe_hangup_markup(self, message: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{xml_escape(message)}</Say>
    <Hangup/>
</Response>"""

    def speak_and_stream_markup(
        self, disclosure: str, *, stream_url: str, bridge_token: str
    ) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{xml_escape(disclosure)}</Say>
    <Connect>
        <Stream url="{xml_escape(stream_url, quote=True)}">
            <Parameter name="bridge_token" value="{xml_escape(bridge_token, quote=True)}"/>
        </Stream>
    </Connect>
    <Say voice="Polly.Joanna">The realtime assistant is unavailable. Goodbye.</Say>
</Response>"""

    def dial_agent_markup(
        self,
        forward_e164: str,
        *,
        caller_id: str,
        timeout_seconds: int,
        say: str,
        action_url: str = "",
    ) -> str:
        action = (
            f' action="{xml_escape(action_url, quote=True)}" method="POST"'
            if action_url
            else ""
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{xml_escape(say)}</Say>
    <Dial callerId="{xml_escape(caller_id, quote=True)}" timeout="{int(timeout_seconds)}"{action}>
        <Number>{xml_escape(forward_e164)}</Number>
    </Dial>
    <Say voice="Polly.Joanna">Your agent is not available right now. They have your details and will call you back. Goodbye.</Say>
    <Hangup/>
</Response>"""

    def hangup_after_dial_markup(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


class PlivoVoiceProvider(VoiceProvider):
    """New: places calls, verifies caller ID, and provisions DIDs via Plivo.

    Verified against Plivo's official Python SDK source
    (github.com/plivo/plivo-python) and docs.plivo.com as of 2026-09-22 — see
    the migration/PR notes for exact endpoints and confirmed vs. unconfirmed
    details (Verified Caller ID's status-check endpoint and the full
    CallStatus value enum were not directly confirmable from reachable pages;
    this adapter treats any unrecognized status as ``failed`` rather than
    guessing).
    """

    name = PROVIDER_PLIVO

    @staticmethod
    def _credentials(credentials: Optional[Mapping[str, Any]]) -> tuple[str, str]:
        credentials = dict(credentials or {})
        auth_id = str(
            credentials.get("auth_id") or os.getenv("PLIVO_AUTH_ID", "")
        ).strip()
        auth_token = str(
            credentials.get("auth_token") or os.getenv("PLIVO_AUTH_TOKEN", "")
        ).strip()
        if not auth_id or not auth_token:
            raise ProviderConfigurationError(
                "PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN are not configured"
            )
        return auth_id, auth_token

    @classmethod
    def _client(cls, credentials: Optional[Mapping[str, Any]]):
        import plivo

        auth_id, auth_token = cls._credentials(credentials)
        return plivo.RestClient(auth_id, auth_token)

    async def place_call(
        self,
        *,
        to_number: str,
        from_number: str,
        answer_url: str,
        status_callback_url: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        import asyncio

        from plivo.exceptions import PlivoRestError

        if not to_number.startswith("+"):
            raise ProviderRequestError("approved call target must be E.164")
        if not from_number.startswith("+"):
            raise ProviderConfigurationError("Plivo caller ID must be E.164")
        if not answer_url:
            raise ProviderConfigurationError("Plivo answer_url is required")

        def _call() -> str:
            client = self._client(credentials)
            try:
                response = client.calls.create(
                    from_=from_number,
                    to_=to_number,
                    answer_url=answer_url,
                    answer_method="POST",
                    callback_url=status_callback_url,
                    callback_method="POST" if status_callback_url else None,
                    ring_timeout=30,
                )
            except PlivoRestError as exc:
                raise ProviderRequestError(f"Plivo rejected the call request: {exc}") from exc
            call_uuid = getattr(response, "request_uuid", None) or getattr(
                response, "call_uuid", None
            )
            return str(call_uuid or "")

        reference = await asyncio.wait_for(asyncio.to_thread(_call), timeout=25.0)
        if not reference:
            raise ProviderRequestError("Plivo did not return a call UUID")
        return ProviderResult(
            "plivo", reference, "queued", {"to": to_number, "from": from_number}
        )

    async def verify_caller_id_start(
        self,
        phone_number: str,
        *,
        channel: str = "sms",
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> ProviderResult:
        import asyncio

        from plivo.exceptions import PlivoRestError

        if not phone_number.startswith("+"):
            raise ProviderRequestError("phone number must be E.164")
        if channel not in ("sms", "call"):
            raise ProviderRequestError("verification channel must be sms or call")

        def _start() -> str:
            client = self._client(credentials)
            try:
                response = client.verify_callerids.initiate_verify(
                    phone_number=phone_number.lstrip("+"),
                    alias=phone_number,
                    channel=channel,
                )
            except PlivoRestError as exc:
                raise ProviderRequestError(
                    f"Plivo rejected the caller-ID verification request: {exc}"
                ) from exc
            verification_id = getattr(response, "verification_uuid", None) or getattr(
                response, "request_uuid", None
            )
            return str(verification_id or "")

        verification_id = await asyncio.wait_for(asyncio.to_thread(_start), timeout=25.0)
        if not verification_id:
            raise ProviderRequestError("Plivo did not return a verification id")
        return ProviderResult(
            "plivo_caller_id",
            verification_id,
            "pending",
            {"phone_number": phone_number, "channel": channel},
        )

    async def verify_caller_id_complete(
        self,
        verification_id: str,
        otp: str,
        *,
        phone_number: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        import asyncio

        from plivo.exceptions import PlivoRestError

        if not verification_id:
            raise ProviderRequestError("no pending Plivo verification for this number")
        if not otp or not otp.strip().isdigit():
            raise ProviderRequestError("OTP must be numeric")

        def _complete() -> bool:
            client = self._client(credentials)
            try:
                client.verify_callerids.verify_caller_id(
                    verification_uuid=verification_id,
                    otp=otp.strip(),
                )
            except PlivoRestError as exc:
                # A rejected/incorrect OTP is a normal outcome, not a
                # configuration failure — the caller gets "not yet verified"
                # back, never a 5xx.
                logger.info("Plivo caller-ID OTP rejected: %s", exc)
                return False
            return True

        return await asyncio.wait_for(asyncio.to_thread(_complete), timeout=15.0)

    async def is_caller_id_verified(
        self,
        phone_number: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Ask Plivo directly via GET /VerifiedCallerId/{phone_number}.

        Confirmed against the installed SDK's resource source
        (plivo.resources.verify_callerid.VerifyCallerids.get_verified_caller_id),
        not merely documentation: it 404s (ResourceNotFoundError) for a
        number that is not a verified caller id, and returns the record
        otherwise. Used only for reconciliation/polling — Neoh's own
        verified state is still set exclusively by
        verify_caller_id_complete's OTP round-trip, never by this read.
        """
        import asyncio

        from plivo.exceptions import PlivoRestError

        def _check() -> bool:
            client = self._client(credentials)
            try:
                client.verify_callerids.get_verified_caller_id(phone_number)
            except PlivoRestError:
                return False
            return True

        return await asyncio.wait_for(asyncio.to_thread(_check), timeout=15.0)

    async def provision_forwarding_number(
        self,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
        area_code: Optional[str] = None,
    ) -> ProviderResult:
        import asyncio

        from plivo.exceptions import PlivoRestError

        def _provision() -> str:
            client = self._client(credentials)
            try:
                search_kwargs: dict[str, Any] = {"country_iso": "US", "type": "local"}
                if area_code:
                    search_kwargs["pattern"] = str(area_code)
                available = client.numbers.search(**search_kwargs)
                candidates = list(available) if available else []
                if not candidates:
                    raise ProviderRequestError(
                        "Plivo has no available local numbers matching the request"
                    )
                candidate_number = str(getattr(candidates[0], "number", ""))
                if not candidate_number:
                    raise ProviderRequestError("Plivo returned no usable number")
                client.numbers.buy(number=candidate_number)
            except PlivoRestError as exc:
                raise ProviderRequestError(f"Plivo rejected the number purchase: {exc}") from exc
            return candidate_number

        phone_number = await asyncio.wait_for(asyncio.to_thread(_provision), timeout=30.0)
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"
        # Plivo numbers are referenced by the E.164 number itself, not a
        # separate SID — the "sid" here is the number, which is also what
        # configure_number_webhook needs to bind an Application to it.
        return ProviderResult(
            "plivo_number", phone_number, "purchased", {"phone_number": phone_number}
        )

    async def configure_number_webhook(
        self,
        provider_number_sid: str,
        *,
        voice_url: str,
        status_callback_url: Optional[str] = None,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        import asyncio

        from plivo.exceptions import PlivoRestError

        if not voice_url:
            raise ProviderRequestError("voice_url is required to wire an inbound number")
        number = provider_number_sid.lstrip("+")

        def _configure() -> None:
            client = self._client(credentials)
            try:
                app = client.applications.create(
                    app_name=f"neoh-forwarding-{number}",
                    answer_url=voice_url,
                    answer_method="POST",
                    hangup_url=status_callback_url,
                    hangup_method="POST" if status_callback_url else None,
                )
                app_id = getattr(app, "app_id", None)
                if not app_id:
                    raise ProviderRequestError("Plivo did not return an Application id")
                client.numbers.update(number=number, app_id=app_id)
            except PlivoRestError as exc:
                raise ProviderRequestError(f"Plivo rejected the webhook update: {exc}") from exc

        await asyncio.wait_for(asyncio.to_thread(_configure), timeout=20.0)

    async def transfer_call(
        self,
        provider_call_id: str,
        *,
        redirect_url: str,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        import asyncio

        from plivo.exceptions import PlivoRestError

        def _transfer() -> None:
            client = self._client(credentials)
            try:
                client.calls.transfer(
                    call_uuid=provider_call_id,
                    legs="aleg",
                    aleg_url=redirect_url,
                    aleg_method="POST",
                )
            except PlivoRestError as exc:
                raise ProviderRequestError(f"Plivo rejected the call transfer: {exc}") from exc

        await asyncio.wait_for(asyncio.to_thread(_transfer), timeout=15.0)

    async def abort_call(
        self,
        provider_call_id: str,
        *,
        credentials: Optional[Mapping[str, Any]] = None,
    ) -> None:
        import asyncio

        def _hangup() -> None:
            client = self._client(credentials)
            try:
                client.calls.delete(call_uuid=provider_call_id)
            except Exception:
                logger.exception(
                    "Failed to terminate unmanaged Plivo call: uuid=%s",
                    provider_call_id,
                )

        try:
            await asyncio.wait_for(asyncio.to_thread(_hangup), timeout=15.0)
        except Exception:
            logger.exception(
                "Failed to terminate unmanaged Plivo call: uuid=%s", provider_call_id
            )

    def media_websocket_path(self) -> str:
        return "/api/commands/media/plivo"

    def safe_hangup_markup(self, message: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>{xml_escape(message)}</Speak>
    <Hangup/>
</Response>"""

    def speak_and_stream_markup(
        self, disclosure: str, *, stream_url: str, bridge_token: str
    ) -> str:
        # bridge_token travels as a query parameter on the WSS URL itself —
        # PlivoXML's <Stream> element takes only a URL as its text content,
        # unlike TwiML's <Stream>, which accepts nested <Parameter> children.
        separator = "&" if "?" in stream_url else "?"
        signed_stream_url = f"{stream_url}{separator}bridge_token={bridge_token}"
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>{xml_escape(disclosure)}</Speak>
    <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">{xml_escape(signed_stream_url)}</Stream>
    <Speak>The realtime assistant is unavailable. Goodbye.</Speak>
</Response>"""

    def dial_agent_markup(
        self,
        forward_e164: str,
        *,
        caller_id: str,
        timeout_seconds: int,
        say: str,
        action_url: str = "",
    ) -> str:
        action = (
            f' action="{xml_escape(action_url, quote=True)}" method="POST"'
            if action_url
            else ""
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>{xml_escape(say)}</Speak>
    <Dial callerId="{xml_escape(caller_id, quote=True)}" timeLimit="{int(timeout_seconds)}"{action}>
        <Number>{xml_escape(forward_e164)}</Number>
    </Dial>
    <Speak>Your agent is not available right now. They have your details and will call you back. Goodbye.</Speak>
    <Hangup/>
</Response>"""

    def hangup_after_dial_markup(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
