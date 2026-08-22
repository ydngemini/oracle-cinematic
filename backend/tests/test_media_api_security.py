import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from fastapi import HTTPException

import media_api
from tenancy import Role, TenantContext, require_context


CTX = TenantContext(
    agent_id="agent-1",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


def test_media_bytes_require_neoh_authentication():
    route = next(route for route in media_api.router.routes if route.path == "/api/media/{media_id}")
    assert any(dependency.call is require_context for dependency in route.dependant.dependencies)
    with pytest.raises(HTTPException) as exc:
        require_context(None)
    assert exc.value.status_code == 401


def test_media_bytes_are_loaded_through_tenant_scoped_metadata(monkeypatch):
    class Conn:
        async def fetchrow(self, query, media_id):
            assert "FROM property_media AS pm" in query
            assert "JOIN media_blobs AS mb" in query
            assert media_id == UUID("22222222-2222-4222-8222-222222222222")
            return {"content_type": "image/png", "bytes": b"private-image"}

    received = []

    @asynccontextmanager
    async def fake_tenant_tx(ctx):
        received.append(ctx)
        yield Conn()

    monkeypatch.setattr(media_api, "tenant_tx", fake_tenant_tx)
    response = asyncio.run(
        media_api.serve_media(
            UUID("22222222-2222-4222-8222-222222222222"),
            ctx=CTX,
            if_none_match=None,
        )
    )

    assert received == [CTX]
    assert response.body == b"private-image"
    # `private` is the security-relevant half: the requesting user's browser may
    # cache a tenant-scoped image, but no shared proxy or CDN may. It replaced
    # `no-store`, which made every thumbnail re-read the whole file.
    assert "private" in response.headers["cache-control"]
    assert "public" not in response.headers["cache-control"]
    assert response.headers["etag"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_media_persist_rejects_unbounded_file_count():
    files = [object()] * (media_api.MAX_FILES_PER_UPLOAD + 1)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            media_api._persist(
                object(),
                tenant_id=CTX.tenant_id,
                lead_id=None,
                listing_id=None,
                files=files,
            )
        )

    assert exc.value.status_code == 413


def test_contract_generation_rate_limit_is_per_actor(monkeypatch):
    media_api._contract_generation_timestamps.clear()
    monkeypatch.setattr(media_api, "_CONTRACT_RATE_LIMIT", 2)
    monkeypatch.setattr(media_api.time, "monotonic", lambda: 100.0)

    media_api._check_contract_generation_rate(CTX)
    media_api._check_contract_generation_rate(CTX)
    with pytest.raises(HTTPException) as exc:
        media_api._check_contract_generation_rate(CTX)

    assert exc.value.status_code == 429


def test_contract_data_failure_does_not_expose_internal_error(monkeypatch):
    from ml_forge import synthetic_lawyer

    media_api._contract_generation_timestamps.clear()

    async def fail(*_args, **_kwargs):
        raise RuntimeError("database host and internal details")

    monkeypatch.setattr(synthetic_lawyer, "generate_assignment_contract_for_client", fail)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            media_api.generate_assignment_contract(
                UUID("22222222-2222-4222-8222-222222222222"),
                expiration_seconds=3600,
                ctx=CTX,
            )
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "Contract data service unavailable."
    assert "database host" not in exc.value.detail


def test_repeat_view_is_answered_without_rereading_the_bytes(monkeypatch):
    """A matching If-None-Match must 304 before touching the database.

    Media is the heaviest thing this API serves and a property card renders
    eight of them at once. Without a validator every re-render was a full read
    of every file, which is what made the database the image server.
    """
    import asyncio
    from uuid import UUID

    import media_api

    media_id = UUID("22222222-2222-4222-8222-222222222222")

    def exploding_tx(_ctx):
        raise AssertionError("a 304 must not query for the bytes")

    monkeypatch.setattr(media_api, "tenant_tx", exploding_tx)

    response = asyncio.run(
        media_api.serve_media(media_id, ctx=CTX, if_none_match=f'"{media_id}"')
    )

    assert response.status_code == 304
    assert response.headers["etag"] == f'"{media_id}"'


def test_a_stale_validator_still_serves_the_bytes(monkeypatch):
    """Only the matching id short-circuits — anything else reads normally."""
    import asyncio
    from contextlib import asynccontextmanager
    from uuid import UUID

    import media_api

    class Conn:
        async def fetchrow(self, *_a, **_k):
            return {
                "kind": "photo", "s3_key": None, "media_content_type": "image/png",
                "content_type": "image/png", "bytes": b"real-bytes",
            }

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield Conn()

    monkeypatch.setattr(media_api, "tenant_tx", fake_tx)

    response = asyncio.run(
        media_api.serve_media(
            UUID("22222222-2222-4222-8222-222222222222"),
            ctx=CTX,
            if_none_match='"11111111-1111-4111-8111-111111111111"',
        )
    )

    assert response.status_code == 200
    assert response.body == b"real-bytes"
