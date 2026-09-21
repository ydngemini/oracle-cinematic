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


async def send_smtp_email(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
    reply_to: Optional[str] = None,
) -> ProviderResult:
    """Send one previously-approved email over SMTP.

    This is the bring-your-own-mail-server path: `credentials` comes from the
    tenant's encrypted provider vault, so a brokerage can send through their own
    server (or their own Google account) without the platform holding it. With
    no tenant credential it falls back to the platform SMTP environment.
    `reply_to` is the drafting agent's own address (user_profiles.public_email)
    — the send still goes out through the one platform/tenant relay, but
    replies land with the agent who worked the lead, not the relay mailbox."""
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
                reply_to=reply_to,
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
