"""Transactional email on Azure Communication Services.

Email was the one channel with no Azure path at all — every sender went through
SES — so password reset silently no-op'd on an Azure deployment. These cover the
provider seam and the ACS sender, including the default that decides which cloud
an unconfigured environment mails through.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import auth
import command_providers
import commands_api
from command_providers import (
    ProviderConfigurationError,
    ProviderRejectedError,
    ProviderRequestError,
    send_acs_email,
)

CONNECTION_STRING = "endpoint=https://neoh-acs.communication.azure.com/;accesskey=fake"


class _Poller:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class _EmailClient:
    """Stand-in for azure.communication.email.EmailClient."""

    sent: list[dict] = []
    result = {"id": "acs-message-1", "status": "Succeeded"}

    @classmethod
    def from_connection_string(cls, connection_string):
        cls.connection_string = connection_string
        return cls()

    def begin_send(self, message):
        type(self).sent.append(message)
        return _Poller(type(self).result)


@pytest.fixture(autouse=True)
def _fake_acs_sdk(monkeypatch):
    """Install a fake azure.communication.email module for the lazy import."""
    _EmailClient.sent = []
    _EmailClient.result = {"id": "acs-message-1", "status": "Succeeded"}
    module = types.ModuleType("azure.communication.email")
    module.EmailClient = _EmailClient
    monkeypatch.setitem(sys.modules, "azure.communication.email", module)
    monkeypatch.setenv("ACS_CONNECTION_STRING", CONNECTION_STRING)
    monkeypatch.setenv("ORACLE_ACS_FROM_EMAIL", "no-reply@neohrs.com")
    # A developer with SMTP configured locally must not change what these assert.
    monkeypatch.delenv("ORACLE_EMAIL_PROVIDER", raising=False)
    yield


def _draft(**over):
    draft = {
        "target": {"email": "agent@example.com"},
        "subject": "Your showing is confirmed",
        "body": "See you at 3pm.",
    }
    draft.update(over)
    return draft


# --- provider selection -------------------------------------------------------

def test_email_provider_defaults_to_smtp(monkeypatch):
    """An unset ORACLE_EMAIL_PROVIDER must not route mail through AWS. SMTP is
    the platform sender under per-agent Google OAuth; ACS is opt-in."""
    monkeypatch.delenv("ORACLE_EMAIL_PROVIDER", raising=False)
    sender, credential_key = commands_api._resolve_email_provider()

    assert sender is command_providers.send_smtp_email
    assert credential_key == "smtp"


def test_acs_remains_selectable(monkeypatch):
    monkeypatch.setenv("ORACLE_EMAIL_PROVIDER", "acs")
    sender, credential_key = commands_api._resolve_email_provider()

    assert sender is command_providers.send_acs_email
    assert credential_key == "acs"


def test_ses_remains_selectable(monkeypatch):
    monkeypatch.setenv("ORACLE_EMAIL_PROVIDER", "ses")
    sender, credential_key = commands_api._resolve_email_provider()

    assert sender is command_providers.send_ses_email
    assert credential_key == "ses"


def test_unknown_email_provider_fails_loudly(monkeypatch):
    monkeypatch.setenv("ORACLE_EMAIL_PROVIDER", "sendgrid")

    with pytest.raises(ProviderConfigurationError, match="ORACLE_EMAIL_PROVIDER"):
        commands_api._resolve_email_provider()


# --- the ACS sender -----------------------------------------------------------

def test_sends_approved_draft_and_returns_message_id():
    result = asyncio.run(send_acs_email(_draft()))

    assert result.provider == "acs_email"
    assert result.reference == "acs-message-1"
    assert result.status == "submitted"
    assert result.detail == {"recipient": "agent@example.com"}

    (message,) = _EmailClient.sent
    assert message["senderAddress"] == "no-reply@neohrs.com"
    assert message["recipients"] == {"to": [{"address": "agent@example.com"}]}
    assert message["content"]["subject"] == "Your showing is confirmed"
    assert message["content"]["plainText"] == "See you at 3pm."


def test_per_tenant_credentials_override_environment():
    asyncio.run(
        send_acs_email(
            _draft(),
            credentials={
                "connection_string": "endpoint=https://tenant.communication.azure.com/;accesskey=k",
                "from_email": "team@brokerage.example",
            },
        )
    )

    assert _EmailClient.connection_string.startswith("endpoint=https://tenant.")
    assert _EmailClient.sent[0]["senderAddress"] == "team@brokerage.example"


def test_missing_connection_string_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)

    with pytest.raises(ProviderConfigurationError, match="connection string"):
        asyncio.run(send_acs_email(_draft()))


def test_missing_sender_identity_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("ORACLE_ACS_FROM_EMAIL", raising=False)

    with pytest.raises(ProviderConfigurationError, match="ORACLE_ACS_FROM_EMAIL"):
        asyncio.run(send_acs_email(_draft()))


@pytest.mark.parametrize(
    "draft, expected",
    [
        (_draft(target={"email": "not-an-email"}), "target is invalid"),
        (_draft(target={}), "target is invalid"),
        (_draft(subject=""), "subject and body are required"),
        (_draft(body="   "), "subject and body are required"),
    ],
)
def test_rejects_malformed_drafts(draft, expected):
    with pytest.raises(ProviderRequestError, match=expected):
        asyncio.run(send_acs_email(draft))


def test_downstream_rejection_surfaces_as_rejected():
    """The SDK accepting the call is not the same as ACS accepting the message."""
    _EmailClient.result = {"id": "acs-2", "status": "Failed"}

    with pytest.raises(ProviderRejectedError, match="Failed"):
        asyncio.run(send_acs_email(_draft()))


def test_missing_message_id_is_a_request_error():
    _EmailClient.result = {"status": "Succeeded"}

    with pytest.raises(ProviderRequestError, match="message id"):
        asyncio.run(send_acs_email(_draft()))


# --- password reset -----------------------------------------------------------

def test_password_reset_email_goes_through_acs_when_selected(monkeypatch):
    monkeypatch.setenv("ORACLE_EMAIL_PROVIDER", "acs")

    auth._send_reset_email("locked-out@example.com", "https://neohrs.com/?reset=tok")

    (message,) = _EmailClient.sent
    assert message["recipients"] == {"to": [{"address": "locked-out@example.com"}]}
    assert "https://neohrs.com/?reset=tok" in message["content"]["html"]
    assert "https://neohrs.com/?reset=tok" in message["content"]["plainText"]


def test_password_reset_stays_best_effort_when_email_is_unconfigured(monkeypatch):
    """/forgot must still return 202 so it can't be used to enumerate accounts."""
    monkeypatch.setenv("ORACLE_EMAIL_PROVIDER", "acs")
    monkeypatch.delenv("ACS_CONNECTION_STRING", raising=False)

    auth._send_reset_email("locked-out@example.com", "https://neohrs.com/?reset=tok")

    assert _EmailClient.sent == []


def test_password_reset_rejects_an_unknown_provider_without_raising(monkeypatch):
    monkeypatch.setenv("ORACLE_EMAIL_PROVIDER", "carrier-pigeon")

    auth._send_reset_email("locked-out@example.com", "https://neohrs.com/?reset=tok")

    assert _EmailClient.sent == []
