"""Regression coverage for the Azure recovery trust boundaries."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

import auth
import commands_api
import csrf_middleware
from csrf_middleware import CSRFMiddleware
from rate_limit_middleware import _get_client_ip
from tenancy import _request_authorization


class _Request:
    def __init__(self, *, cookies=None, headers=None, host="10.0.0.4"):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = SimpleNamespace(host=host)


def test_session_cookie_becomes_bearer_authorization():
    request = _Request(cookies={"oracle_session": "signed.jwt"})
    assert _request_authorization(request, None) == "Bearer signed.jwt"
    assert _request_authorization(request, "Bearer explicit") == "Bearer explicit"


def test_forwarded_for_is_ignored_unless_trusted(monkeypatch):
    request = _Request(headers={"X-Forwarded-For": "198.51.100.9, 203.0.113.7"})
    monkeypatch.setenv("ORACLE_ENV", "dev")
    monkeypatch.delenv("ORACLE_TRUST_PROXY_HEADERS", raising=False)
    assert _get_client_ip(request) == "10.0.0.4"
    monkeypatch.setenv("ORACLE_TRUST_PROXY_HEADERS", "true")
    assert _get_client_ip(request) == "203.0.113.7"


def test_managed_proxy_defaults_to_rightmost_public_client(monkeypatch):
    request = _Request(
        headers={"X-Forwarded-For": "198.51.100.9, 10.1.2.3, 172.16.4.5"}
    )
    monkeypatch.setenv("ORACLE_ENV", "prod")
    monkeypatch.delenv("ORACLE_TRUST_PROXY_HEADERS", raising=False)
    assert _get_client_ip(request) == "198.51.100.9"

    monkeypatch.setenv("ORACLE_TRUST_PROXY_HEADERS", "false")
    assert _get_client_ip(request) == "10.0.0.4"


def test_csrf_blocks_json_mutations_without_double_submit_token():
    middleware = CSRFMiddleware(lambda *_args, **_kwargs: None, enabled=True)

    def request(*headers: tuple[bytes, bytes]) -> Request:
        return Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/generate-tour",
            "raw_path": b"/api/generate-tour",
            "query_string": b"",
            "headers": list(headers),
            "client": ("198.51.100.9", 443),
            "server": ("api.neoh.example", 443),
        })

    async def call_next(_request):
        return JSONResponse({"ok": True})

    rejected = asyncio.run(middleware.dispatch(request(), call_next))
    assert rejected.status_code == 403

    accepted = asyncio.run(
        middleware.dispatch(
            request(
                (b"cookie", b"csrf_token=same-token"),
                (b"x-csrf-token", b"same-token"),
            ),
            call_next,
        )
    )
    assert accepted.status_code == 200


def test_csrf_bootstrap_preserves_endpoint_cookie(monkeypatch):
    issued_tokens = iter(("bootstrap-token", "competing-token"))
    monkeypatch.setattr(
        csrf_middleware,
        "_generate_csrf_token",
        lambda: next(issued_tokens),
    )
    middleware = CSRFMiddleware(lambda *_args, **_kwargs: None, enabled=True)
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/auth/csrf",
        "raw_path": b"/auth/csrf",
        "query_string": b"",
        "headers": [],
        "client": ("198.51.100.9", 443),
        "server": ("api.neoh.example", 443),
    })

    async def call_next(_request):
        response = JSONResponse({"csrf_token": "bootstrap-token"})
        assert csrf_middleware.issue_csrf_cookie(response) == "bootstrap-token"
        return response

    response = asyncio.run(middleware.dispatch(request, call_next))
    set_cookie = response.headers.getlist("set-cookie")
    assert len(set_cookie) == 1
    assert "csrf_token=bootstrap-token" in set_cookie[0]
    assert "competing-token" not in set_cookie[0]


def test_csrf_bootstrap_reuses_valid_browser_cookie():
    token = "a" * 43
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/auth/csrf",
        "raw_path": b"/auth/csrf",
        "query_string": b"",
        "headers": [(b"cookie", f"csrf_token={token}".encode("ascii"))],
        "client": ("198.51.100.9", 443),
        "server": ("api.neoh.example", 443),
    })
    response = JSONResponse({})

    assert csrf_middleware.get_or_issue_csrf_cookie(request, response) == token
    assert response.headers.getlist("set-cookie") == []


def test_logout_returns_concrete_no_content_response():
    response = auth.logout(Response())
    assert response.status_code == 204
    assert len(response.headers.getlist("set-cookie")) == 2


def test_shared_redis_initializer_returns_connected_client(monkeypatch):
    import rate_limit_middleware

    class _Redis:
        async def ping(self):
            return True

    client = _Redis()
    monkeypatch.setenv("REDIS_URL", "rediss://example.invalid:10000/0")
    monkeypatch.setattr(rate_limit_middleware, "_redis_client", None)

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *_args, **_kwargs: client)
    assert asyncio.run(rate_limit_middleware._init_redis()) is client


_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _validate_config_in_subprocess(**settings: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in (
        "ORACLE_ENV",
        "ORACLE_DOMAIN",
        "ORACLE_BASE_URL",
        "ORACLE_PUBLIC_BASE_URL",
        "ORACLE_SECRET_KEY",
        "ORACLE_ENCRYPTION_MASTER_KEY",
        "ORACLE_JWT_ISSUER",
        "ORACLE_JWT_AUDIENCE",
        "ORACLE_JWT_TENANT_ISSUER",
        "ORACLE_JWT_TENANT_AUDIENCE",
        "ORACLE_ENABLE_WEBHOOKS",
        "ORACLE_ACS_WEBHOOK_SECRET",
        "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET",
    ):
        environment.pop(name, None)
    environment.update({"PYTHONPATH": str(_BACKEND_DIR), **settings})
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import config; config.validate_or_die(); import auth; "
                "assert auth._JWT_ISSUER and auth._JWT_AUDIENCE"
            ),
        ],
        cwd=_BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_production_config_resolves_jwt_scope_and_gates_webhook_secrets():
    base = {
        "ORACLE_ENV": "prod",
        "ORACLE_DOMAIN": "https://api.neoh.example",
        "ORACLE_SECRET_KEY": "test-only-secret-key-with-at-least-32-bytes",
        "ORACLE_ENCRYPTION_MASTER_KEY": "test-only-encryption-key",
    }
    webhooks_disabled = _validate_config_in_subprocess(**base)
    assert webhooks_disabled.returncode == 0, webhooks_disabled.stderr

    tenant_issuer_fallback = _validate_config_in_subprocess(
        ORACLE_ENV="prod",
        ORACLE_JWT_TENANT_ISSUER="https://login.example/tenant/v2.0",
        ORACLE_SECRET_KEY=base["ORACLE_SECRET_KEY"],
        ORACLE_ENCRYPTION_MASTER_KEY=base["ORACLE_ENCRYPTION_MASTER_KEY"],
    )
    assert tenant_issuer_fallback.returncode == 0, tenant_issuer_fallback.stderr

    webhooks_enabled = _validate_config_in_subprocess(
        **base, ORACLE_ENABLE_WEBHOOKS="true"
    )
    assert webhooks_enabled.returncode != 0
    assert "ORACLE_ACS_WEBHOOK_SECRET" in webhooks_enabled.stderr
    assert "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET" in webhooks_enabled.stderr


class _WebhookRequest:
    def __init__(self, payload, token=""):
        self._payload = payload
        self.query_params = {"token": token} if token else {}

    async def json(self):
        return self._payload


def _configure_webhooks(monkeypatch) -> None:
    monkeypatch.setenv("ORACLE_ACS_WEBHOOK_SECRET", "acs-test-secret")
    monkeypatch.setenv("ORACLE_CUSTOM_CALL_WEBHOOK_SECRET", "custom-test-secret")


def test_acs_webhook_rejects_forged_events(monkeypatch):
    _configure_webhooks(monkeypatch)
    event = {
        "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
        "data": {"validationCode": "validation-code"},
    }
    with pytest.raises(HTTPException) as forged:
        asyncio.run(commands_api.acs_webhook(_WebhookRequest(event)))
    assert forged.value.status_code == 403
    accepted = asyncio.run(
        commands_api.acs_webhook(_WebhookRequest(event, "acs-test-secret"))
    )
    assert accepted == {"validationResponse": "validation-code"}


def test_custom_call_rejects_forgery_and_client_selected_reply_url(monkeypatch):
    _configure_webhooks(monkeypatch)
    with pytest.raises(HTTPException) as forged:
        asyncio.run(commands_api.custom_call_webhook(_WebhookRequest({})))
    assert forged.value.status_code == 403
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(commands_api.custom_call_webhook(_WebhookRequest(
            {"event": "speech", "reply_url": "http://127.0.0.1/internal"},
            "custom-test-secret",
        )))
    assert blocked.value.status_code == 422
    assert blocked.value.detail == "reply_url is not supported."


def test_custom_call_status_callback_is_accepted_inline(monkeypatch):
    _configure_webhooks(monkeypatch)
    response = asyncio.run(commands_api.custom_call_webhook(
        _WebhookRequest({}, "custom-test-secret")
    ))
    assert response == {"accepted": True}
