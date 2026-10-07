"""Error responses keep the security headers, and malformed ids are 4xx.

Found by the OWASP ZAP authenticated active scan against staging (2026-10-06):
three GET routes answered a non-UUID id with an unhandled 500 — and because
Starlette renders unhandled errors outside user middleware, those 500s carried
no HSTS, CSP or nosniff.
"""

from __future__ import annotations

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client():
    app = FastAPI()
    app.middleware("http")(server._security_headers)
    app.exception_handler(asyncpg.exceptions.InvalidTextRepresentationError)(server._invalid_input_syntax)
    app.exception_handler(ValueError)(server._value_error)

    @app.get("/asyncpg-uuid")
    async def asyncpg_uuid():
        # Exactly what asyncpg raises binding "client_id" to a $1::uuid param.
        raise ValueError("invalid UUID 'client_id': length must be between 32..36 characters, got 9")

    @app.get("/asyncpg-wrapped-uuid")
    async def asyncpg_wrapped_uuid():
        # What actually reached FastAPI on staging: asyncpg's client-side
        # DataError (InterfaceError, ValueError) wrapping the codec's message.
        from asyncpg.exceptions import _base

        raise _base.DataError("invalid input for query argument $1: 'client_id' "
                              "(invalid UUID 'client_id': length must be between 32..36 characters, got 9)")

    @app.get("/other-value-error")
    async def other_value_error():
        raise ValueError("an internal bug")

    @app.get("/bad-uuid")
    async def bad_uuid():
        raise asyncpg.exceptions.InvalidTextRepresentationError(
            'invalid input syntax for type uuid: "client_id"')

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    return TestClient(app, raise_server_exceptions=False)


def test_a_malformed_id_is_422_not_500(client):
    r = client.get("/bad-uuid")
    assert r.status_code == 422
    assert "expected format" in r.json()["detail"]
    assert "client_id" not in r.text, "the database's message is not echoed"


def test_an_unhandled_error_keeps_every_security_header(client):
    r = client.get("/boom")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text
    for header in server._SECURITY_HEADERS:
        assert header in r.headers, header


def test_the_api_csp_states_the_directives_with_no_fallback():
    csp = server._SECURITY_HEADERS["Content-Security-Policy"]
    for directive in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"):
        assert directive in csp


def test_an_empty_plan_filter_is_no_filter():
    import inspect

    import sales_api

    src = inspect.getsource(sales_api.list_enrollments)
    assert 'plan_id = _uuid(plan_id, "plan_id") if plan_id else None' in src


def test_asyncpgs_client_side_uuid_error_is_422(client):
    r = client.get("/asyncpg-uuid")
    assert r.status_code == 422 and "client_id" not in r.text


def test_any_other_value_error_is_a_500_with_headers(client):
    r = client.get("/other-value-error")
    assert r.status_code == 500 and "internal bug" not in r.text
    assert "Strict-Transport-Security" in r.headers


def test_asyncpgs_wrapped_bind_error_is_422(client):
    r = client.get("/asyncpg-wrapped-uuid")
    assert r.status_code == 422 and "client_id" not in r.text
