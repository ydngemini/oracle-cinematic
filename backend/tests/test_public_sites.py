"""Published sites are served publicly; nothing else is (prompt 14 T14.05)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import public_sites_api as ps


def _request(etag=None):
    headers = [(b"if-none-match", etag.encode())] if etag else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def _with_row(monkeypatch, row):
    seen = {}

    class Conn:
        async def fetchrow(self, sql, *args):
            seen["sql"], seen["args"] = sql, args
            return row

    @asynccontextmanager
    async def tx(ctx):
        seen["ctx"] = ctx
        yield Conn()

    monkeypatch.setattr(ps, "tenant_tx", tx)
    return seen


ROW = {"name": "Wilmington Homes", "template_key": "editorial", "published_at": None, "revision": 3,
       "brand_theme": '{"accent": "#fff"}', "content": {"headline": "Hi"}, "content_sha256": "a" * 64}


def test_a_published_site_returns_only_public_fields(monkeypatch):
    seen = _with_row(monkeypatch, ROW)
    response = Response()
    body = asyncio.run(ps.public_site("northstar", "wilmington-homes", _request(), response))
    assert body == {"name": "Wilmington Homes", "template_key": "editorial", "revision": 3, "published_at": None,
                    "brand_theme": {"accent": "#fff"}, "content": {"headline": "Hi"}}
    assert response.headers["cache-control"] == "no-cache" and response.headers["etag"] == f'"{"a" * 64}"'
    assert "s.status = 'published'" in seen["sql"] and "r.id = s.published_revision_id" in seen["sql"]
    assert seen["args"] == ("northstar", "wilmington-homes")


def test_an_unchanged_page_revalidates_with_304(monkeypatch):
    _with_row(monkeypatch, ROW)
    result = asyncio.run(ps.public_site("northstar", "wilmington-homes", _request(f'"{"a" * 64}"'), Response()))
    assert result.status_code == 304


@pytest.mark.parametrize("tenant, slug", [("northstar", "nope"), ("../etc", "x"), ("a", "b"), ("North", "site")])
def test_unpublished_or_malformed_addresses_are_404(monkeypatch, tenant, slug):
    _with_row(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ps.public_site(tenant, slug, _request(), Response()))
    assert exc.value.status_code == 404


def test_publish_returns_the_served_address():
    assert ps.public_site_path("northstar", "wilmington-homes") == "/sites/northstar/wilmington-homes"
