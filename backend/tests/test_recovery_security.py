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
import rate_limit_middleware
from csrf_middleware import CSRFMiddleware
from rate_limit_middleware import (
    AUTHENTICATED_API_RATE_LIMIT,
    RateLimitMiddleware,
    _authenticated_principal,
    _get_bucket_for_path,
    _get_client_ip,
)
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


def test_authenticated_api_principal_requires_a_valid_token_and_is_opaque(monkeypatch):
    request = _Request(cookies={"oracle_session": "signed.jwt"})
    monkeypatch.setattr(
        auth,
        "decode_token",
        lambda token: {
            "sub": "agent@example.com",
            "tenant_id": "tenant-123",
        }
        if token == "signed.jwt"
        else None,
    )

    principal = _authenticated_principal(request)
    assert principal is not None
    assert principal.startswith("principal:")
    assert "agent@example.com" not in principal
    assert "tenant-123" not in principal

    def reject(_token):
        raise HTTPException(status_code=401, detail="Invalid token")

    monkeypatch.setattr(auth, "decode_token", reject)
    assert _authenticated_principal(request) is None


def test_auth_status_and_unknown_paths_do_not_share_the_general_api_bucket():
    assert _get_bucket_for_path("/auth/session") == "/auth/"
    assert _get_bucket_for_path("/api/crm/contacts") == "/api/"
    assert _get_bucket_for_path("/robots.txt") == "/other/"


def test_authenticated_general_api_gets_a_principal_quota(monkeypatch):
    observed = []

    async def check(identity, bucket, limit):
        observed.append((identity, bucket, limit))
        return True, 1

    monkeypatch.setattr(rate_limit_middleware, "_check_rate_limit_redis", check)
    monkeypatch.setattr(
        rate_limit_middleware,
        "_authenticated_principal",
        lambda _request: "principal:opaque",
    )
    middleware = RateLimitMiddleware(lambda *_args, **_kwargs: None, enabled=True)
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/crm/contacts",
        "raw_path": b"/api/crm/contacts",
        "query_string": b"",
        "headers": [],
        "client": ("198.51.100.9", 443),
        "server": ("api.neoh.example", 443),
    })

    async def call_next(_request):
        return JSONResponse({"ok": True})

    response = asyncio.run(middleware.dispatch(request, call_next))
    assert response.status_code == 200
    assert observed == [
        ("principal:opaque", "/api/authenticated", AUTHENTICATED_API_RATE_LIMIT)
    ]
    assert response.headers["X-RateLimit-Limit"] == str(AUTHENTICATED_API_RATE_LIMIT)


def test_cors_preflight_uses_its_own_bucket_not_the_endpoint_quota(monkeypatch):
    """A preflight must not spend the endpoint's quota, but must still be capped.

    An entirely unmetered method would be the cheapest way to walk the whole
    middleware chain and route resolution from a single IP for free.
    """
    observed: list[tuple[str, str, int]] = []

    async def record(identity, bucket, limit):
        observed.append((identity, bucket, limit))
        return True, 1

    monkeypatch.setattr(rate_limit_middleware, "_check_rate_limit_redis", record)
    middleware = RateLimitMiddleware(lambda *_args, **_kwargs: None, enabled=True)
    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "OPTIONS",
        "scheme": "https",
        "path": "/api/crm/contacts",
        "raw_path": b"/api/crm/contacts",
        "query_string": b"",
        "headers": [],
        "client": ("198.51.100.9", 443),
        "server": ("api.neoh.example", 443),
    })

    async def call_next(_request):
        return Response(status_code=204)

    response = asyncio.run(middleware.dispatch(request, call_next))
    assert response.status_code == 204
    assert observed == [
        ("198.51.100.9", "OPTIONS", rate_limit_middleware.PREFLIGHT_RATE_LIMIT)
    ]


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


def test_logout_returns_concrete_no_content_response_with_matching_dev_cookie_scope(monkeypatch):
    import config

    monkeypatch.setattr(config, "IS_DEV", True)
    response = auth.logout(Response())
    assert response.status_code == 204
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 2
    assert all("samesite=lax" in cookie.lower() for cookie in cookies)
    assert all("secure" not in cookie.lower() for cookie in cookies)


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
        "ORACLE_QWEN_REALTIME_ENABLED",
        "ORACLE_TWILIO_QWEN_REALTIME_ENABLED",
        "ORACLE_ACS_RESOURCE_ID",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_WORKSPACE_ID",
        "DASHSCOPE_REALTIME_URL",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
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
        # 32+ chars: config.validate_or_die() now refuses a key too short to be
        # real, and this fixture is about JWT scope, not key strength.
        "ORACLE_ENCRYPTION_MASTER_KEY": "test-only-encryption-key-32-bytes-min",
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

    qwen_without_acs_audience = _validate_config_in_subprocess(
        **base,
        ORACLE_QWEN_REALTIME_ENABLED="true",
        DASHSCOPE_API_KEY="test-dashscope-key",
        DASHSCOPE_WORKSPACE_ID="ws-test",
    )
    assert qwen_without_acs_audience.returncode != 0
    assert "ORACLE_ACS_RESOURCE_ID" in qwen_without_acs_audience.stderr

    twilio_qwen_without_twilio = _validate_config_in_subprocess(
        **base,
        ORACLE_TWILIO_QWEN_REALTIME_ENABLED="true",
        DASHSCOPE_API_KEY="test-dashscope-key",
        DASHSCOPE_WORKSPACE_ID="ws-test",
    )
    assert twilio_qwen_without_twilio.returncode != 0
    assert "TWILIO_ACCOUNT_SID" in twilio_qwen_without_twilio.stderr
    assert "TWILIO_AUTH_TOKEN" in twilio_qwen_without_twilio.stderr
    assert "TWILIO_FROM_NUMBER" in twilio_qwen_without_twilio.stderr

    twilio_qwen_ready = _validate_config_in_subprocess(
        **base,
        ORACLE_PUBLIC_BASE_URL="https://api.neoh.example",
        ORACLE_TWILIO_QWEN_REALTIME_ENABLED="true",
        DASHSCOPE_API_KEY="test-dashscope-key",
        DASHSCOPE_WORKSPACE_ID="ws-test",
        TWILIO_ACCOUNT_SID="AC" + ("a" * 32),
        TWILIO_AUTH_TOKEN="test-twilio-auth-token",
        TWILIO_FROM_NUMBER="+15555550101",
    )
    assert twilio_qwen_ready.returncode == 0, twilio_qwen_ready.stderr


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
