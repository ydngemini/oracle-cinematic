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

import ipaddress
import os
import recovery_mode
import smtplib
import socket
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


class SmtpUncertainError(SmtpSendError):
    """The connection failed after the message may have been handed over.

    Neither "sent" nor "not sent" is known; a blind retry could deliver it
    twice. Callers that record side effects must reconcile, not resend."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _reject_internal_host(host: str) -> str:
    """Refuse a tenant-supplied SMTP host that resolves inside the network.

    The host field on a stored SMTP credential is free text an agent or
    broker enters themselves (PUT /api/sales/providers/smtp), and both the
    validate probe and the real send connect to it. Without this, either is
    an SSRF/port-scan primitive against the deployment's own network — a
    "connection refused" vs. "timed out" vs. "does not offer STARTTLS"
    response distinguishes open from closed ports on hosts the caller has no
    business reaching, including the cloud metadata address.
    """
    # Returns the vetted address, and the connection is made to THAT address
    # (connect() pins it): resolving once here and again inside smtplib left a
    # DNS-rebinding window between the check and the connect (review SSRF-1).
    # The test is `is_global`, which also excludes shared/CGNAT space
    # (100.64/10) that the old private/loopback/link-local list let through, and
    # an unresolvable name is refused rather than waved on.
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SmtpConfigurationError(f"SMTP host {host!r} does not resolve") from exc
    vetted: Optional[str] = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global or ip.is_multicast:
            raise SmtpConfigurationError(
                f"SMTP host {host!r} resolves to a non-public address; "
                "a tenant mail server must be reachable on the public internet"
            )
        vetted = vetted or str(ip)
    if vetted is None:
        raise SmtpConfigurationError(f"SMTP host {host!r} does not resolve")
    return vetted


def resolve_settings(credentials: Optional[Mapping[str, Any]] = None) -> dict:
    """Merge per-tenant credentials over the platform environment."""
    supplied = dict(credentials or {})

    tenant_host = _clean(supplied.get("host"))
    host = tenant_host or _clean(os.getenv("ORACLE_SMTP_HOST"))
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
    # The operator's own ORACLE_SMTP_HOST is trusted; only a host an agent or
    # broker typed into the SMTP credential form is checked.
    pinned_ip = _reject_internal_host(tenant_host) if tenant_host else None
    if not sender or "@" not in sender:
        raise SmtpConfigurationError("ORACLE_SMTP_FROM_EMAIL is not configured")
    # Anonymous relays exist, but a blank password with a username set is far
    # more likely to be a half-filled config than an intentional open relay.
    if username and not password:
        raise SmtpConfigurationError("SMTP username is set but the password is missing")

    return {
        "host": host,
        "pinned_ip": pinned_ip,
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
    reply_to: Optional[str] = None,
) -> EmailMessage:
    if not recipient or "@" not in parseaddr(recipient)[1]:
        raise SmtpConfigurationError("recipient address is invalid")

    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = recipient
    message["Subject"] = subject
    # Mail still leaves through the one platform relay, but a reply-to lets it
    # read as coming from the agent working the lead rather than the platform.
    reply_to = _clean(reply_to)
    if reply_to and "@" in parseaddr(reply_to)[1]:
        message["Reply-To"] = reply_to
    # A stable domain-anchored Message-ID reads better to spam filters than the
    # local hostname smtplib would otherwise invent inside a container.
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


class _PinnedSMTP(smtplib.SMTP):
    """TCP to the vetted IP; EHLO/STARTTLS/certificate checks still use the
    hostname (smtplib keeps it in self._host and passes it as server_hostname)."""

    def __init__(self, *args, pinned_ip: str, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(self._pinned_ip, port, timeout)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, *args, pinned_ip: str, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(self._pinned_ip, port, timeout)


def connect(settings: Mapping[str, Any], *, timeout: float = DEFAULT_TIMEOUT) -> smtplib.SMTP:
    """Open, secure and authenticate one session; the caller closes it.

    Every path that talks to the relay — a real send and the credential
    probe in sales_api — goes through here, so the TLS policy is decided once.
    """
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    pinned = settings.get("pinned_ip")
    if settings["port"] == IMPLICIT_TLS_PORT:
        client = (
            _PinnedSMTP_SSL(settings["host"], settings["port"], timeout=timeout,
                            context=context, pinned_ip=pinned)
            if pinned else
            smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=timeout, context=context)
        )
    else:
        client = (
            _PinnedSMTP(settings["host"], settings["port"], timeout=timeout, pinned_ip=pinned)
            if pinned else
            smtplib.SMTP(settings["host"], settings["port"], timeout=timeout)
        )
    try:
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
    except BaseException:
        client.close()
        raise
    return client


def send(
    *,
    recipient: str,
    subject: str,
    text: str,
    html: Optional[str] = None,
    reply_to: Optional[str] = None,
    credentials: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Send one message. Returns its Message-ID.

    Raises SmtpConfigurationError for anything the operator must fix and
    SmtpSendError for a server-side rejection, so callers can tell "not set up
    yet" apart from "set up but refused"."""
    recovery_mode.guard(f"send email to {recipient}")
    settings = resolve_settings(credentials)
    message = build_message(
        sender=settings["sender"],
        sender_name=settings["sender_name"],
        recipient=recipient,
        subject=subject,
        text=text,
        html=html,
        reply_to=reply_to,
    )

    # Phase 1 — connect, STARTTLS, login. Nothing has been handed over yet,
    # so every failure here is a definite "not sent".
    try:
        client = connect(settings, timeout=timeout)
    except SmtpConfigurationError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        # Overwhelmingly this is a Gmail account without an app password.
        raise SmtpConfigurationError(f"SMTP authentication rejected: {exc}") from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise SmtpSendError(f"SMTP connection failed: {exc}") from exc

    # Phase 2 — MAIL / RCPT / DATA. An explicit refusal from the server is a
    # definite "not sent". Losing the connection (timeout waiting for the 250
    # after DATA, reset, disconnect) is not: the server may have queued it.
    try:
        client.send_message(message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError, smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError) as exc:
        client.close()
        raise SmtpSendError(f"SMTP server refused the message: {exc}") from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        client.close()
        raise SmtpUncertainError(f"SMTP connection lost while sending: {type(exc).__name__}") from exc

    # Phase 3 — the server accepted it. A failed QUIT no longer turns a
    # delivered message into an error (it used to, and the job resent it).
    try:
        client.__exit__(None, None, None)  # QUIT + close, as `with` would
    except (smtplib.SMTPException, OSError, ssl.SSLError):
        client.close()
    return str(message["Message-ID"])
