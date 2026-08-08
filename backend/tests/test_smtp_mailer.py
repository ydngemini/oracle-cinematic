"""SMTP transport, credential resolution and the no-plaintext rule.

SMTP is how Neoh sends mail: through a server the operator names, with no
third-party API in the path. A password reset travels this way, which is why the
connection is always encrypted and why there is no default host to fall back on.
"""

from __future__ import annotations

import smtplib
import ssl
import sys
import types

import pytest

import auth
import smtp_mailer
from smtp_mailer import SmtpConfigurationError, SmtpSendError

_ENV = (
    "ORACLE_SMTP_HOST",
    "ORACLE_SMTP_PORT",
    "ORACLE_SMTP_USERNAME",
    "ORACLE_SMTP_PASSWORD",
    "ORACLE_SMTP_FROM_EMAIL",
    "ORACLE_SMTP_FROM_NAME",
    "ORACLE_EMAIL_PROVIDER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)


class FakeSMTP:
    """Records the protocol conversation so the TLS policy can be asserted."""

    instances: list["FakeSMTP"] = []
    starttls_supported = True
    login_error: Exception | None = None
    send_error: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.implicit_tls = context is not None
        self.events: list[str] = []
        self.sent: list = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append("quit")
        return False

    def ehlo(self):
        self.events.append("ehlo")

    def has_extn(self, name):
        return type(self).starttls_supported if name == "starttls" else False

    def starttls(self, context=None):
        assert context is not None, "STARTTLS must be given a verifying SSL context"
        self.events.append("starttls")

    def login(self, username, password):
        if type(self).login_error:
            raise type(self).login_error
        self.events.append(f"login:{username}")

    def send_message(self, message):
        if type(self).send_error:
            raise type(self).send_error
        self.events.append("send")
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.starttls_supported = True
    FakeSMTP.login_error = None
    FakeSMTP.send_error = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ORACLE_SMTP_HOST", "mail.neohrs.com")
    monkeypatch.setenv("ORACLE_SMTP_USERNAME", "no-reply@neohrs.com")
    monkeypatch.setenv("ORACLE_SMTP_PASSWORD", "app-password")


def _send(**over):
    kwargs = {"recipient": "agent@example.com", "subject": "Hi", "text": "Body"}
    kwargs.update(over)
    return smtp_mailer.send(**kwargs)


# --- configuration ------------------------------------------------------------

def test_sender_falls_back_to_the_login_address(configured):
    """Authenticating as an address is the common case; do not require it twice."""
    assert smtp_mailer.resolve_settings()["sender"] == "no-reply@neohrs.com"


def test_tenant_credentials_override_the_platform_environment(configured):
    settings = smtp_mailer.resolve_settings(
        {"host": "mail.brokerage.example", "port": "2525", "from_email": "team@brokerage.example"}
    )

    assert settings["host"] == "mail.brokerage.example"
    assert settings["port"] == 2525
    assert settings["sender"] == "team@brokerage.example"


def test_missing_host_is_a_configuration_error():
    """There is deliberately no default host — guessing one would silently route
    mail through a third party."""
    with pytest.raises(SmtpConfigurationError, match="ORACLE_SMTP_HOST"):
        smtp_mailer.resolve_settings()


def test_missing_sender_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv("ORACLE_SMTP_HOST", "mail.neohrs.com")

    with pytest.raises(SmtpConfigurationError, match="ORACLE_SMTP_FROM_EMAIL"):
        smtp_mailer.resolve_settings()


def test_username_without_password_is_rejected(monkeypatch):
    """Far more likely a half-filled config than a deliberate open relay."""
    monkeypatch.setenv("ORACLE_SMTP_HOST", "mail.neohrs.com")
    monkeypatch.setenv("ORACLE_SMTP_USERNAME", "no-reply@neohrs.com")

    with pytest.raises(SmtpConfigurationError, match="password is missing"):
        smtp_mailer.resolve_settings()


@pytest.mark.parametrize("port", ["not-a-number", "0", "70000"])
def test_bad_ports_are_rejected(configured, port):
    with pytest.raises(SmtpConfigurationError):
        smtp_mailer.resolve_settings({"port": port})


def test_is_configured_does_not_raise():
    assert smtp_mailer.is_configured() is False


# --- transport security -------------------------------------------------------

def test_starttls_is_negotiated_before_login(configured, fake_smtp):
    _send()
    conversation = fake_smtp.instances[0].events

    assert conversation.index("starttls") < conversation.index("login:no-reply@neohrs.com")
    assert conversation.index("login:no-reply@neohrs.com") < conversation.index("send")


def test_refuses_to_send_when_the_server_has_no_starttls(configured, fake_smtp):
    """A downgrade would put the reset link on the wire in the clear."""
    fake_smtp.starttls_supported = False

    with pytest.raises(SmtpConfigurationError, match="STARTTLS"):
        _send()
    assert "send" not in fake_smtp.instances[0].events


def test_port_465_uses_implicit_tls_and_skips_starttls(configured, monkeypatch, fake_smtp):
    monkeypatch.setenv("ORACLE_SMTP_PORT", "465")

    _send()
    conversation = fake_smtp.instances[0].events

    assert "starttls" not in conversation
    assert fake_smtp.instances[0].implicit_tls is True
    assert "send" in conversation


# --- message shape ------------------------------------------------------------

def test_message_carries_sender_recipient_and_both_bodies(configured, fake_smtp):
    _send(html="<p>Body</p>")
    message = fake_smtp.instances[0].sent[0]

    assert message["To"] == "agent@example.com"
    assert message["From"] == "no-reply@neohrs.com"
    assert message["Subject"] == "Hi"
    assert message.get_content_type() == "multipart/alternative"


def test_display_name_is_used_when_configured(configured, monkeypatch, fake_smtp):
    monkeypatch.setenv("ORACLE_SMTP_FROM_NAME", "Neoh")

    _send()
    assert fake_smtp.instances[0].sent[0]["From"] == "Neoh <no-reply@neohrs.com>"


def test_message_id_is_anchored_to_the_sender_domain(configured, fake_smtp):
    """Otherwise smtplib invents one from the container hostname, which reads
    badly to spam filters."""
    reference = _send()

    assert reference.endswith("@neohrs.com>")


def test_invalid_recipient_is_rejected_before_connecting(configured, fake_smtp):
    with pytest.raises(SmtpConfigurationError, match="recipient"):
        _send(recipient="not-an-address")
    assert fake_smtp.instances == []


# --- failure classification ---------------------------------------------------

def test_auth_failure_is_a_configuration_error(configured, fake_smtp):
    """Almost always a wrong or expired credential — operator fixable."""
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad creds")

    with pytest.raises(SmtpConfigurationError, match="authentication"):
        _send()


def test_server_rejection_is_a_send_error(configured, fake_smtp):
    """Distinct from misconfiguration so callers can tell them apart."""
    fake_smtp.send_error = smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"nope")})

    with pytest.raises(SmtpSendError, match="SMTP send failed"):
        _send()


def test_network_failure_is_a_send_error(configured, fake_smtp):
    fake_smtp.send_error = OSError("connection reset")

    with pytest.raises(SmtpSendError):
        _send()


# --- password reset -----------------------------------------------------------

def test_password_reset_defaults_to_smtp(configured, fake_smtp):
    auth._send_reset_email("locked-out@example.com", "https://neohrs.com/?reset=tok")
    message = fake_smtp.instances[0].sent[0]

    assert message["To"] == "locked-out@example.com"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "https://neohrs.com/?reset=tok" in body


def test_password_reset_survives_an_unconfigured_smtp_server(fake_smtp):
    """/forgot must still return 202 so it can't enumerate accounts."""
    auth._send_reset_email("locked-out@example.com", "https://neohrs.com/?reset=tok")

    assert fake_smtp.instances == []
