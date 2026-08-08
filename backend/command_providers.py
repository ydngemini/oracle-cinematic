"""Narrow provider adapters for approved command jobs.

Secrets come from the environment/secret-injection layer or encrypted provider
credentials, never from a command payload.  All network calls have finite
timeouts and return provider references suitable for the audit trail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.command_providers")


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    pass


class ProviderRejectedError(ProviderRequestError):
    """The provider rejected the request before creating a remote action."""


def _authenticated_callback_url(path: str, secret_env: str) -> str:
    base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    secret = os.getenv(secret_env, "").strip()
    if not base:
        raise ProviderConfigurationError("ORACLE_PUBLIC_BASE_URL is not set — callback URL required")
    if not secret:
        raise ProviderConfigurationError(f"{secret_env} is not configured")
    return f"{base}{path}?token={urllib.parse.quote(secret, safe='')}"


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    reference: str
    status: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _http_json(
    request: urllib.request.Request,
    *,
    timeout: float = 20.0,
    allow_http: bool = False,
) -> tuple[int, dict[str, Any]]:
    if not allow_http and not request.full_url.startswith("https://"):
        raise ProviderRequestError(
            f"refusing non-HTTPS provider URL: {request.full_url[:80]}"
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body else {}
            return response.status, parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1_000]
        raise ProviderRequestError(f"provider returned HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderRequestError("provider request timed out or was unavailable") from exc


async def send_ses_email(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    credentials = dict(credentials or {})
    sender = str(credentials.get("from_email") or os.getenv("ORACLE_SES_FROM_EMAIL", ""))
    recipient = str((draft.get("target") or {}).get("email") or "").strip()
    subject = str(draft.get("subject") or "").strip()
    body_text = str(draft.get("body") or "").strip()
    if not sender:
        raise ProviderConfigurationError("ORACLE_SES_FROM_EMAIL is not configured")
    if not recipient or "@" not in recipient:
        raise ProviderRequestError("approved email target is invalid")
    if not subject or not body_text:
        raise ProviderRequestError("approved email subject and body are required")

    def _send() -> dict[str, Any]:
        import boto3
        from botocore.config import Config

        client_options: dict[str, Any] = {
            "region_name": str(
                credentials.get("region") or os.getenv("AWS_REGION", "us-east-2")
            ),
            "config": Config(
                connect_timeout=8,
                read_timeout=15,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        }
        access_key = str(credentials.get("aws_access_key_id") or "").strip()
        secret_key = str(credentials.get("aws_secret_access_key") or "").strip()
        if bool(access_key) != bool(secret_key):
            raise ProviderConfigurationError(
                "SES access key id and secret access key must be configured together"
            )
        if access_key:
            client_options["aws_access_key_id"] = access_key
            client_options["aws_secret_access_key"] = secret_key
            session_token = str(credentials.get("aws_session_token") or "").strip()
            if session_token:
                client_options["aws_session_token"] = session_token
        client = boto3.client("sesv2", **client_options)
        return client.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
                }
            },
        )

    response = await asyncio.wait_for(asyncio.to_thread(_send), timeout=25.0)
    reference = str(response.get("MessageId") or "")
    if not reference:
        raise ProviderRequestError("SES did not return a message id")
    return ProviderResult("ses", reference, "submitted", {"recipient": recipient})


async def send_smtp_email(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    """Send one previously-approved email over SMTP.

    This is the bring-your-own-mail-server path: `credentials` comes from the
    tenant's encrypted provider vault, so a brokerage can send through their own
    server (or their own Google account) without the platform holding it. With
    no tenant credential it falls back to the platform SMTP environment."""
    credentials = dict(credentials or {})
    recipient = str((draft.get("target") or {}).get("email") or "").strip()
    subject = str(draft.get("subject") or "").strip()
    body_text = str(draft.get("body") or "").strip()
    if not recipient or "@" not in recipient:
        raise ProviderRequestError("approved email target is invalid")
    if not subject or not body_text:
        raise ProviderRequestError("approved email subject and body are required")

    def _send() -> str:
        import smtp_mailer

        try:
            return smtp_mailer.send(
                recipient=recipient,
                subject=subject,
                text=body_text,
                credentials=credentials,
            )
        except smtp_mailer.SmtpConfigurationError as exc:
            raise ProviderConfigurationError(str(exc)[:500]) from exc
        except smtp_mailer.SmtpSendError as exc:
            raise ProviderRejectedError(str(exc)[:500]) from exc

    reference = await asyncio.wait_for(asyncio.to_thread(_send), timeout=40.0)
    if not reference:
        raise ProviderRequestError("SMTP did not return a message id")
    return ProviderResult("smtp", reference, "submitted", {"recipient": recipient})


async def send_acs_email(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    """Send one previously-approved email through Azure Communication Services.

    The ACS counterpart to send_ses_email — same validation, same approved-draft
    contract, so the caller does not care which cloud is behind it."""
    credentials = dict(credentials or {})
    connection_string = str(
        credentials.get("connection_string") or os.getenv("ACS_CONNECTION_STRING", "")
    ).strip()
    sender = str(
        credentials.get("from_email") or os.getenv("ORACLE_ACS_FROM_EMAIL", "")
    ).strip()
    recipient = str((draft.get("target") or {}).get("email") or "").strip()
    subject = str(draft.get("subject") or "").strip()
    body_text = str(draft.get("body") or "").strip()
    if not connection_string:
        raise ProviderConfigurationError("ACS connection string is not configured")
    if not sender:
        raise ProviderConfigurationError("ORACLE_ACS_FROM_EMAIL is not configured")
    if not recipient or "@" not in recipient:
        raise ProviderRequestError("approved email target is invalid")
    if not subject or not body_text:
        raise ProviderRequestError("approved email subject and body are required")

    def _send() -> str:
        from azure.communication.email import EmailClient

        client = EmailClient.from_connection_string(connection_string)
        poller = client.begin_send(
            {
                "senderAddress": sender,
                "recipients": {"to": [{"address": recipient}]},
                "content": {"subject": subject, "plainText": body_text},
            }
        )
        result = poller.result() or {}
        status = str(result.get("status") or "")
        # ACS reports a terminal per-message status; anything but Succeeded means
        # the message was accepted by the SDK but rejected downstream.
        if status and status.lower() not in ("succeeded", "running", "notstarted"):
            raise ProviderRejectedError(f"ACS email status {status}"[:500])
        return str(result.get("id") or "")

    reference = await asyncio.wait_for(asyncio.to_thread(_send), timeout=25.0)
    if not reference:
        raise ProviderRequestError("ACS did not return a message id")
    return ProviderResult("acs_email", reference, "submitted", {"recipient": recipient})


def _twilio_credential_error(
    account_sid: str, auth_token: str, api_key: str, api_secret: str
) -> Optional[str]:
    """Validate a Twilio credential set. Returns an error message, or None.

    A usable credential is a *complete* API key pair or an auth token. A half-
    configured pair is not usable on its own: it must not be preferred over a
    working auth token, because Client(key, "", sid) 401s on every request.
    """
    if not account_sid:
        return "TWILIO_ACCOUNT_SID is not configured"
    if bool(api_key) != bool(api_secret):
        if not auth_token:
            return "Twilio API key and API secret must be configured together"
        logger.warning(
            "Twilio API key pair is incomplete (key=%s secret=%s) — "
            "falling back to TWILIO_AUTH_TOKEN",
            bool(api_key),
            bool(api_secret),
        )
    if not auth_token and not (api_key and api_secret):
        return "Twilio auth token or API key pair is not configured"
    return None


def _twilio_client(account_sid: str, auth_token: str, api_key: str, api_secret: str):
    """Build a Twilio REST client, preferring a complete API key pair.

    An incomplete pair is ignored rather than used — _twilio_credential_error
    has already guaranteed an auth token exists in that case.
    """
    from twilio.rest import Client

    if api_key and api_secret:
        return Client(api_key, api_secret, account_sid)
    return Client(account_sid, auth_token)


async def send_twilio_sms(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    """Send one previously-approved SMS with a registered Twilio sender."""
    credentials = dict(credentials or {})
    account_sid = str(
        credentials.get("account_sid") or os.getenv("TWILIO_ACCOUNT_SID", "")
    ).strip()
    auth_token = str(
        credentials.get("auth_token") or os.getenv("TWILIO_AUTH_TOKEN", "")
    ).strip()
    api_key = str(credentials.get("api_key") or os.getenv("TWILIO_API_KEY", "")).strip()
    api_secret = str(
        credentials.get("api_secret") or os.getenv("TWILIO_API_SECRET", "")
    ).strip()
    sender = str(
        credentials.get("sms_sender")
        or credentials.get("sms_sender_e164")
        or os.getenv("TWILIO_SMS_FROM_NUMBER", "")
    ).strip()
    sender_type = str(credentials.get("sms_sender_type") or "").strip()
    recipient = str((draft.get("target") or {}).get("phone") or "").strip()
    body = str(draft.get("body") or "").strip()

    cred_error = _twilio_credential_error(account_sid, auth_token, api_key, api_secret)
    if cred_error:
        raise ProviderConfigurationError(cred_error)
    if not sender.startswith("+"):
        raise ProviderConfigurationError("A registered Twilio SMS sender is not configured")
    if sender_type and sender_type not in {
        "twilio_registered",
        "ported",
        "toll_free_verified",
    }:
        raise ProviderConfigurationError("Twilio SMS sender registration is not verified")
    if not recipient.startswith("+"):
        raise ProviderRequestError("approved SMS target must be E.164")
    if not body or len(body) > 1_600:
        raise ProviderRequestError("approved SMS body must contain 1-1600 characters")

    def _send() -> str:
        from twilio.base.exceptions import TwilioRestException

        client = _twilio_client(account_sid, auth_token, api_key, api_secret)
        try:
            message = client.messages.create(to=recipient, from_=sender, body=body)
        except TwilioRestException as exc:
            raise ProviderRejectedError(
                f"Twilio rejected the SMS request (code {exc.code or 'unknown'})."
            ) from exc
        return str(message.sid or "")

    reference = await asyncio.wait_for(asyncio.to_thread(_send), timeout=25.0)
    if not reference:
        raise ProviderRequestError("Twilio did not return a message SID")
    return ProviderResult("twilio_sms", reference, "queued", {"to": recipient})


async def send_acs_sms(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    """Send one previously-approved SMS through Azure Communication Services."""
    credentials = dict(credentials or {})
    connection_string = str(
        credentials.get("connection_string") or os.getenv("ACS_CONNECTION_STRING", "")
    ).strip()
    sender = str(
        credentials.get("sms_sender")
        or credentials.get("sms_sender_e164")
        or os.getenv("ACS_SMS_FROM_NUMBER", "")
    ).strip()
    recipient = str((draft.get("target") or {}).get("phone") or "").strip()
    body = str(draft.get("body") or "").strip()
    if not connection_string:
        raise ProviderConfigurationError("ACS connection string is not configured")
    if not sender:
        raise ProviderConfigurationError("ACS SMS sender is not configured")
    if not recipient.startswith("+"):
        raise ProviderRequestError("approved SMS target must be E.164")
    if not body or len(body) > 1_600:
        raise ProviderRequestError("approved SMS body must contain 1-1600 characters")

    def _send() -> str:
        from azure.communication.sms import SmsClient

        client = SmsClient.from_connection_string(connection_string)
        results = client.send(
            from_=sender,
            to=[recipient],
            message=body,
            enable_delivery_report=True,
        )
        result = next(iter(results), None)
        if result is None:
            return ""
        successful = bool(getattr(result, "successful", False))
        if not successful:
            error_message = str(getattr(result, "error_message", "ACS rejected the SMS"))
            raise ProviderRejectedError(error_message[:500])
        return str(getattr(result, "message_id", "") or "")

    reference = await asyncio.wait_for(asyncio.to_thread(_send), timeout=25.0)
    if not reference:
        raise ProviderRequestError("ACS did not return a message id")
    return ProviderResult("acs_sms", reference, "queued", {"to": recipient})


async def place_acs_call(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    credentials = dict(credentials or {})
    connection_string = str(
        credentials.get("connection_string") or os.getenv("ACS_CONNECTION_STRING", "")
    )
    from_number = str(
        credentials.get("from_number") or os.getenv("ACS_FROM_NUMBER", "")
    )
    callback_url = _authenticated_callback_url(
        "/api/commands/webhooks/acs", "ORACLE_ACS_WEBHOOK_SECRET"
    )
    to_number = str((draft.get("target") or {}).get("phone") or "").strip()

    if not connection_string:
        raise ProviderConfigurationError("ACS connection string is not configured")
    if not from_number:
        raise ProviderConfigurationError("ACS from-number is not configured")
    if not to_number.startswith("+"):
        raise ProviderRequestError("approved call target must be E.164")

    def _call() -> str:
        from azure.communication.callautomation import (
            CallAutomationClient,
            CallInvite,
            PhoneNumberIdentifier,
        )
        from acs_call_handler import build_qwen_media_streaming_options

        client = CallAutomationClient.from_connection_string(connection_string)
        call_invite = CallInvite(
            target=PhoneNumberIdentifier(to_number),
            source_caller_id_number=PhoneNumberIdentifier(from_number),
        )
        result = client.create_call(
            call_invite,
            callback_url,
            media_streaming=build_qwen_media_streaming_options(),
        )
        return result.call_connection_id or ""

    reference = await asyncio.wait_for(asyncio.to_thread(_call), timeout=25.0)
    if not reference:
        raise ProviderRequestError("ACS did not return a call connection ID")
    return ProviderResult("acs", reference, "queued", {})


async def create_google_calendar_event(
    draft: Mapping[str, Any],
    *,
    access_token: Optional[str] = None,
) -> ProviderResult:
    token = access_token or os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN", "")
    if not token:
        raise ProviderConfigurationError("Google Calendar OAuth credential is not configured")
    event = dict(draft.get("event") or {})
    if not event.get("summary") or not event.get("start") or not event.get("end"):
        raise ProviderRequestError("calendar summary, start, and end are required")
    calendar_id = urllib.parse.quote(
        str(draft.get("calendar_id") or "primary"), safe=""
    )
    request = urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events?sendUpdates=all",
        data=json.dumps(event, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Oracle-CommandRouter/1.0",
        },
    )
    _, response = await asyncio.wait_for(asyncio.to_thread(_http_json, request), timeout=25.0)
    reference = str(response.get("id") or "")
    if not reference:
        raise ProviderRequestError("Google Calendar did not return an event id")
    return ProviderResult(
        "google_calendar",
        reference,
        str(response.get("status") or "confirmed"),
        {"html_link": response.get("htmlLink")},
    )


async def place_twilio_call(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    credentials = dict(credentials or {})
    account_sid = str(credentials.get("account_sid") or os.getenv("TWILIO_ACCOUNT_SID", ""))
    api_key = str(credentials.get("api_key") or os.getenv("TWILIO_API_KEY", ""))
    api_secret = str(credentials.get("api_secret") or os.getenv("TWILIO_API_SECRET", ""))
    auth_token = str(
        credentials.get("auth_token")
        or os.getenv("TWILIO_AUTH_TOKEN", "")
    )
    from_number = str(
        credentials.get("from_number") or os.getenv("TWILIO_FROM_NUMBER", "")
    )
    to_number = str((draft.get("target") or {}).get("phone") or "").strip()
    callback_base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    status_callback = (
        f"{callback_base}/api/commands/webhooks/twilio/status"
        if callback_base
        else ""
    )
    if not status_callback:
        logger.warning(
            "ORACLE_PUBLIC_BASE_URL is not set — Twilio call status callbacks will be lost"
        )
    twiml_url = os.getenv("ORACLE_TWILIO_TWIML_URL", "") or (
        f"{callback_base}/api/commands/webhooks/twilio" if callback_base else ""
    )
    account_tier = os.getenv("ORACLE_TWILIO_ACCOUNT_TIER", "").strip().lower()

    cred_error = _twilio_credential_error(account_sid, auth_token, api_key, api_secret)
    if cred_error:
        raise ProviderConfigurationError(cred_error)
    if not from_number:
        raise ProviderConfigurationError("TWILIO_FROM_NUMBER is not configured")
    if not to_number.startswith("+"):
        raise ProviderRequestError("approved call target must be E.164")
    if not twiml_url:
        raise ProviderConfigurationError("Twilio TwiML URL is not configured")
    if account_tier in {"trial", "free-trial"}:
        raise ProviderRejectedError(
            "Twilio Voice Trial permits only Twilio's predefined outbound "
            "templates and blocks Media Streams; upgrade the Twilio project "
            "before placing CRM AI calls."
        )

    def _call() -> str:
        from twilio.base.exceptions import TwilioRestException

        client = _twilio_client(account_sid, auth_token, api_key, api_secret)
        try:
            call = client.calls.create(
                to=to_number,
                from_=from_number,
                url=twiml_url,
                status_callback=status_callback or None,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                timeout=30,
            )
        except TwilioRestException as exc:
            raise ProviderRejectedError(
                f"Twilio rejected the call request (code {exc.code or 'unknown'})."
            ) from exc
        return call.sid

    reference = await asyncio.wait_for(asyncio.to_thread(_call), timeout=25.0)
    if not reference:
        raise ProviderRequestError("Twilio did not return a call SID")
    return ProviderResult("twilio", reference, "queued", {"to": to_number, "from": from_number})


async def abort_twilio_call(
    call_sid: str,
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> None:
    """Best-effort termination when durable Twilio state cannot be established."""
    credentials = dict(credentials or {})
    account_sid = str(
        credentials.get("account_sid") or os.getenv("TWILIO_ACCOUNT_SID", "")
    )
    api_key = str(credentials.get("api_key") or os.getenv("TWILIO_API_KEY", ""))
    api_secret = str(
        credentials.get("api_secret") or os.getenv("TWILIO_API_SECRET", "")
    )
    auth_token = str(
        credentials.get("auth_token") or os.getenv("TWILIO_AUTH_TOKEN", "")
    )
    cred_error = _twilio_credential_error(account_sid, auth_token, api_key, api_secret)
    if cred_error:
        logger.error("Cannot terminate unmanaged Twilio call: %s", cred_error)
        return

    def _hangup() -> None:
        client = _twilio_client(account_sid, auth_token, api_key, api_secret)
        client.calls(call_sid).update(status="completed")

    try:
        await asyncio.wait_for(asyncio.to_thread(_hangup), timeout=15.0)
    except Exception:
        logger.exception("Failed to terminate unmanaged Twilio call: sid=%s", call_sid)


def _extract_call_reference(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    for key in ("call_id", "callId", "id", "sid", "reference", "provider_call_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


async def place_custom_http_call(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    """Call any internet telephony provider through a custom REST endpoint.

    Provider payload contract:
    {
      "to": "+15551234567",
      "from": "+15555550101",
      "callback_url": "https://your-domain.example/...",
      "provider": "custom_call"
    }
    Provider response should return JSON with one of: call_id / id / sid / reference.
    """
    credentials = dict(credentials or {})
    api_url = str(
        credentials.get("api_url") or os.getenv("ORACLE_CUSTOM_CALL_API_URL", "")
    ).strip()
    auth_token = str(
        credentials.get("auth_token")
        or credentials.get("api_secret")
        or os.getenv("ORACLE_CUSTOM_CALL_AUTH_TOKEN", "")
    ).strip()
    from_number = str(
        credentials.get("from_number")
        or os.getenv("ORACLE_CUSTOM_CALL_FROM_NUMBER", "")
    ).strip()
    to_number = str((draft.get("target") or {}).get("phone") or "").strip()
    callback_url = _authenticated_callback_url(
        "/api/commands/webhooks/custom-call", "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET"
    )

    if not api_url:
        raise ProviderConfigurationError("ORACLE_CUSTOM_CALL_API_URL is not configured")
    if not to_number.startswith("+"):
        raise ProviderRequestError("approved call target must be E.164")

    payload = {
        "to": to_number,
        "from": from_number,
        "callback_url": callback_url,
        "provider": "custom_call",
        "channel": "voice",
        "draft": {
            "subject": str((draft.get("subject") or "").strip()),
            "body": str((draft.get("body") or "").strip())[:5000],
        },
    }

    headers = {"Content-Type": "application/json", "User-Agent": "Oracle-CommandRouter/1.0"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    def _call() -> str:
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        _, response = _http_json(request, timeout=30.0)
        return _extract_call_reference(response)

    reference = await asyncio.wait_for(asyncio.to_thread(_call), timeout=35.0)
    if not reference:
        raise ProviderRequestError("Custom internet call API did not return a call identifier")
    return ProviderResult(
        "custom_call",
        reference,
        "queued",
        {"to": to_number, "from": from_number},
    )
