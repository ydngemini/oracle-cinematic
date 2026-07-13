"""Narrow provider adapters for approved command jobs.

Secrets come from the environment/secret-injection layer or encrypted provider
credentials, never from a command payload.  All network calls have finite
timeouts and return provider references suitable for the audit trail.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    pass


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
) -> tuple[int, dict[str, Any]]:
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


async def send_ses_email(draft: Mapping[str, Any]) -> ProviderResult:
    sender = os.getenv("ORACLE_SES_FROM_EMAIL", "")
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

        client = boto3.client(
            "sesv2",
            region_name=os.getenv("AWS_REGION", "us-east-2"),
            config=Config(
                connect_timeout=8,
                read_timeout=15,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
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


async def place_twilio_call(
    draft: Mapping[str, Any],
    *,
    credentials: Optional[Mapping[str, Any]] = None,
) -> ProviderResult:
    credentials = dict(credentials or {})
    account_sid = str(credentials.get("account_sid") or os.getenv("TWILIO_ACCOUNT_SID", ""))
    auth_token = str(credentials.get("auth_token") or os.getenv("TWILIO_AUTH_TOKEN", ""))
    from_number = str(credentials.get("from_number") or os.getenv("TWILIO_FROM_NUMBER", ""))
    twiml_url = str(credentials.get("twiml_url") or os.getenv("ORACLE_TWILIO_TWIML_URL", ""))
    to_number = str((draft.get("target") or {}).get("phone") or "").strip()
    if not all((account_sid, auth_token, from_number, twiml_url)):
        raise ProviderConfigurationError("Twilio call credentials or TwiML URL are not configured")
    if not to_number.startswith("+"):
        raise ProviderRequestError("approved call target must be E.164")

    form = {
        "To": to_number,
        "From": from_number,
        "Url": twiml_url,
        "StatusCallback": os.getenv("ORACLE_TWILIO_STATUS_CALLBACK", ""),
        "StatusCallbackEvent": "initiated ringing answered completed",
    }
    form = {key: value for key, value in form.items() if value}
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(account_sid)}/Calls.json",
        data=urllib.parse.urlencode(form).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Oracle-CommandRouter/1.0",
        },
    )
    _, response = await asyncio.wait_for(asyncio.to_thread(_http_json, request), timeout=25.0)
    reference = str(response.get("sid") or "")
    if not reference:
        raise ProviderRequestError("Twilio did not return a call SID")
    return ProviderResult("twilio", reference, str(response.get("status") or "queued"), {})


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


def verify_twilio_signature(
    *,
    url: str,
    form: Mapping[str, Any],
    signature: str,
    auth_token: Optional[str] = None,
) -> bool:
    """Verify Twilio's HMAC-SHA1 webhook signature in constant time."""
    token = auth_token or os.getenv("TWILIO_AUTH_TOKEN", "")
    if not token or not signature or not url.startswith(("https://", "http://")):
        return False
    material = url + "".join(
        f"{key}{form[key]}" for key in sorted(form) if form[key] is not None
    )
    digest = hmac.new(token.encode("utf-8"), material.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)
