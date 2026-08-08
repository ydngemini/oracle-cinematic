import asyncio

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import pytest

from cors_config import DEFAULT_CORS_ORIGINS, get_allowed_origins


def _cors_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_allowed_origins(""),
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization"],
        allow_credentials=True,
    )
    return app


def _cors_wrapped_auth_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def reject_without_auth(request: Request, call_next):
        if not request.headers.get("Authorization"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/protected")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    # Registration order matters: CORS must wrap auth/security middleware.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_allowed_origins(""),
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization"],
        allow_credentials=True,
    )
    return app


def _run_request(app: FastAPI, method: str, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async def _request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers or {})

    return asyncio.run(_request())


def test_defaults_include_vite_development_and_preview_origins(monkeypatch) -> None:
    monkeypatch.delenv("ORACLE_CORS_ORIGINS", raising=False)

    origins = get_allowed_origins()

    assert origins == list(DEFAULT_CORS_ORIGINS)
    assert "http://localhost:4173" in origins
    assert "http://127.0.0.1:4173" in origins
    assert "http://localhost:5173" in origins


def test_configured_origins_are_trimmed_and_deduplicated() -> None:
    assert get_allowed_origins(
        "https://app.neoh.example, https://app.neoh.example,https://admin.neoh.example"
    ) == ["https://app.neoh.example", "https://admin.neoh.example"]


def test_wildcard_origin_is_rejected_for_credentialed_requests() -> None:
    with pytest.raises(RuntimeError, match="exact origins"):
        get_allowed_origins("*")


def test_vite_preview_origin_receives_credentialed_cors_headers() -> None:
    app = _cors_test_app()
    origin = "http://localhost:4173"

    preflight = _run_request(
        app,
        "OPTIONS",
        "/protected",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    response = _run_request(app, "GET", "/protected", headers={"Origin": origin})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unknown_origin_does_not_receive_an_allow_origin_header() -> None:
    response = _run_request(
        _cors_test_app(),
        "GET",
        "/protected",
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_auth_rejection_still_receives_cors_headers() -> None:
    origin = "http://localhost:4173"
    response = _run_request(
        _cors_wrapped_auth_app(),
        "GET",
        "/protected",
        headers={"Origin": origin},
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
