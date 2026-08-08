"""One TLS-enforcing SMTP send primitive.

Two callers need to send mail over SMTP and they have different shapes: the
approved-command provider is async and carries per-tenant credentials, while the
password-reset path in auth.py is synchronous and platform-level. Both go
through send() here so the transport-security policy lives in exactly one place.

Policy: the connection is always encrypted. Port 465 is implicit TLS; every
other port must negotiate STARTTLS and the send is abandoned if the server does
not offer it. There is no plaintext fallback — a downgrade would put a password
reset link on the wire in the clear.

Configuration resolves per-call credentials first, then the platform environment,
so a tenant can bring their own mail server without touching the deployment.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any, Mapping, Optional

# No default host on purpose. Guessing a provider is how a deployment ends up
# silently routing mail through someone else's service; ORACLE_SMTP_HOST must be
# named explicitly, and resolve_settings() raises if it is not.
DEFAULT_PORT = 587
IMPLICIT_TLS_PORT = 465
DEFAULT_TIMEOUT = 20.0


class SmtpConfigurationError(RuntimeError):
    """The mail server, credentials or sender identity are not usable."""


class SmtpSendError(RuntimeError):
    """The server accepted the connection but refused the message."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def resolve_settings(credentials: Optional[Mapping[str, Any]] = None) -> dict:
    """Merge per-tenant credentials over the platform environment."""
    supplied = dict(credentials or {})

    host = _clean(supplied.get("host")) or _clean(os.getenv("ORACLE_SMTP_HOST"))
    raw_port = _clean(supplied.get("port")) or _clean(os.getenv("ORACLE_SMTP_PORT"))
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError as exc:
        raise SmtpConfigurationError(f"SMTP port {raw_port!r} is not a number") from exc
    if not 1 <= port <= 65535:
        raise SmtpConfigurationError(f"SMTP port {port} is out of range")

    username = _clean(supplied.get("username")) or _clean(os.getenv("ORACLE_SMTP_USERNAME"))
    password = _clean(supplied.get("password")) or _clean(os.getenv("ORACLE_SMTP_PASSWORD"))
    # Falling back to the login address keeps the common Gmail case config-free:
    # the account you authenticate as is the account you send from.
    sender = (
        _clean(supplied.get("from_email"))
        or _clean(os.getenv("ORACLE_SMTP_FROM_EMAIL"))
        or username
    )
    sender_name = _clean(supplied.get("from_name")) or _clean(os.getenv("ORACLE_SMTP_FROM_NAME"))

    if not host:
        raise SmtpConfigurationError("ORACLE_SMTP_HOST is not configured")
    if not sender or "@" not in sender:
        raise SmtpConfigurationError("ORACLE_SMTP_FROM_EMAIL is not configured")
    # Anonymous relays exist, but a blank password with a username set is far
    # more likely to be a half-filled config than an intentional open relay.
    if username and not password:
        raise SmtpConfigurationError("SMTP username is set but the password is missing")

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "sender": sender,
        "sender_name": sender_name,
    }


def is_configured(credentials: Optional[Mapping[str, Any]] = None) -> bool:
    """Whether a send could succeed, without attempting one."""
    try:
        resolve_settings(credentials)
        return True
    except SmtpConfigurationError:
        return False


def build_message(
    *,
    sender: str,
    sender_name: str,
    recipient: str,
    subject: str,
    text: str,
    html: Optional[str] = None,
) -> EmailMessage:
    if not recipient or "@" not in parseaddr(recipient)[1]:
        raise SmtpConfigurationError("recipient address is invalid")

    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = recipient
    message["Subject"] = subject
    # A stable domain-anchored Message-ID reads better to spam filters than the
    # local hostname smtplib would otherwise invent inside a container.
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


def send(
    *,
    recipient: str,
    subject: str,
    text: str,
    html: Optional[str] = None,
    credentials: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Send one message. Returns its Message-ID.

    Raises SmtpConfigurationError for anything the operator must fix and
    SmtpSendError for a server-side rejection, so callers can tell "not set up
    yet" apart from "set up but refused"."""
    settings = resolve_settings(credentials)
    message = build_message(
        sender=settings["sender"],
        sender_name=settings["sender_name"],
        recipient=recipient,
        subject=subject,
        text=text,
        html=html,
    )

    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    try:
        if settings["port"] == IMPLICIT_TLS_PORT:
            client = smtplib.SMTP_SSL(
                settings["host"], settings["port"], timeout=timeout, context=context
            )
        else:
            client = smtplib.SMTP(settings["host"], settings["port"], timeout=timeout)
        with client:
            if settings["port"] != IMPLICIT_TLS_PORT:
                client.ehlo()
                if not client.has_extn("starttls"):
                    raise SmtpConfigurationError(
                        f"{settings['host']}:{settings['port']} does not offer STARTTLS; "
                        "refusing to send credentials or a reset link in the clear"
                    )
                client.starttls(context=context)
                client.ehlo()
            if settings["username"]:
                client.login(settings["username"], settings["password"])
            client.send_message(message)
    except SmtpConfigurationError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        # Overwhelmingly this is a Gmail account without an app password.
        raise SmtpConfigurationError(f"SMTP authentication rejected: {exc}") from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise SmtpSendError(f"SMTP send failed: {exc}") from exc

    return str(message["Message-ID"])
