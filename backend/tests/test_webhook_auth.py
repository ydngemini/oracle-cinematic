"""HOOK-3 (security review 2026-10-01, L17-10): the ACS and custom-call webhook
secret no longer travels in callback URLs. A header, a body signature or an
expiring signed URL authenticates; the old `?token=` form is accepted for one
release with a deprecation log line that never carries the secret."""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse

import pytest
from fastapi import HTTPException

import webhook_auth

SECRET = "acs-test-secret-0123456789"
ENV = "ORACLE_ACS_WEBHOOK_SECRET"
PATH = "/api/commands/webhooks/acs"


class _URL:
    def __init__(self, path):
        self.path = path


class _Req:
    def __init__(self, *, headers=None, query=None, body=b"{}", path=PATH):
        self.headers = headers or {}
        self.query_params = query or {}
        self._body = body
        self.url = _URL(path)

    async def body(self):
        return self._body


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(ENV, SECRET)
    monkeypatch.delenv("ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT", raising=False)
    monkeypatch.delenv("ORACLE_WEBHOOK_CALLBACK_TTL_SECONDS", raising=False)


def _verify(req, now=None):
    return asyncio.run(webhook_auth.verify_webhook(req, ENV, now=now))


def _denied(req, now=None) -> int:
    with pytest.raises(HTTPException) as exc:
        _verify(req, now)
    return exc.value.status_code


def test_header_token_is_accepted_and_a_wrong_one_is_not():
    assert _verify(_Req(headers={"x-neoh-webhook-token": SECRET})) == "header"
    assert _denied(_Req(headers={"x-neoh-webhook-token": "nope"})) == 403


def test_body_signature_binds_body_and_time():
    body = b'{"eventType":"x"}'
    now = time.time()
    good = f"t={int(now)},v1={webhook_auth.body_signature(SECRET, int(now), body)}"
    assert _verify(_Req(headers={"x-neoh-signature": good}, body=body), now) == "signature"
    # Another body under the same signature, or a signature replayed 6 minutes later.
    assert _denied(_Req(headers={"x-neoh-signature": good}, body=b'{"eventType":"y"}'), now) == 403
    assert _denied(_Req(headers={"x-neoh-signature": good}, body=body), now + 360) == 403


def test_callback_urls_carry_an_expiring_signature_not_the_secret(monkeypatch):
    import acs_call_handler
    import command_providers

    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.neoh.example")
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_WEBHOOK_SECRET", "custom-test-secret-0123456789")
    acs = acs_call_handler._get_callback_url()
    custom = command_providers._authenticated_callback_url(
        "/api/commands/webhooks/custom-call", "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET")
    for url, secret in ((acs, SECRET), (custom, "custom-test-secret-0123456789")):
        assert secret not in url and urllib.parse.quote(secret) not in url
        assert "token=" not in url
    parts = urllib.parse.urlsplit(acs)
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.path == PATH and set(query) == {"exp", "sig"}
    assert _verify(_Req(query=query, path=parts.path)) == "signed_url"
    # Not valid for another route, and not after it expires.
    assert _denied(_Req(query=query, path="/api/commands/webhooks/custom-call")) == 403
    assert _denied(_Req(query=query, path=parts.path), now=int(query["exp"]) + 1) == 403
    assert _denied(_Req(query={**query, "sig": "0" * 64}, path=parts.path)) == 403


def test_old_query_token_works_for_one_release_and_logs_without_the_secret(caplog, monkeypatch):
    with caplog.at_level(logging.WARNING, logger="oracle.webhook_auth"):
        assert _verify(_Req(query={"token": SECRET})) == "query_token_deprecated"
    assert "DEPRECATED webhook credential" in caplog.text and ENV in caplog.text
    assert SECRET not in caplog.text
    monkeypatch.setenv("ORACLE_WEBHOOK_QUERY_TOKEN_COMPAT", "0")
    assert _denied(_Req(query={"token": SECRET})) == 403


def test_nothing_or_no_configured_secret_is_refused(monkeypatch):
    assert _denied(_Req()) == 403
    assert _denied(_Req(query={"token": "guess"})) == 403
    monkeypatch.setenv(ENV, "")
    assert _denied(_Req(headers={"x-neoh-webhook-token": SECRET})) == 503


def test_the_webhook_routes_use_it():
    import inspect

    import commands_api

    for route in (commands_api.acs_webhook, commands_api.custom_call_webhook):
        assert "await _require_webhook_secret(request" in inspect.getsource(route)
    assert "webhook_auth.verify_webhook" in inspect.getsource(commands_api._require_webhook_secret)
