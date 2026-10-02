"""Failure must not weaken security (resilience 2026-10-02): with every limiter
store down a request is refused (503, never let through, never a misleading
429), and probes stay reachable so operators can see the outage."""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import rate_limit_middleware as rl


def _app():
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/crm/clients", ok), Route("/live", ok), Route("/health/workers", ok)])
    app.add_middleware(rl.RateLimitMiddleware, enabled=True)
    return app


@pytest.fixture
def stores_down(monkeypatch):
    import config
    from db import connection

    monkeypatch.setattr(config, "IS_DEV", False)
    monkeypatch.setattr(connection, "get_pool", lambda: None)   # PostgreSQL gone
    monkeypatch.setattr(rl, "_redis_retry_at", 10**12)           # Valkey breaker open
    yield


def _get(path):
    import httpx

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
            return await c.get(path)

    return asyncio.run(go())


def test_no_store_means_refused_with_503_not_admitted(stores_down):
    r = _get("/api/crm/clients")
    assert r.status_code == 503
    assert r.json()["code"] == "SERVICE_UNAVAILABLE"
    assert 5 <= int(r.headers["Retry-After"]) <= 10


@pytest.mark.parametrize("path", ["/live", "/health/workers"])
def test_probes_stay_reachable_during_the_outage(stores_down, path):
    assert _get(path).status_code == 200
