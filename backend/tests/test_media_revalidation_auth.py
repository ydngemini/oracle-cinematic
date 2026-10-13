"""A 304 on /api/media/{id} is an authorization answer (prompt 16 T16.14): the
service worker revalidates a prefetched splat this way, so a viewer who can no
longer see the row gets 404, never "still yours"."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException

import media_api
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="agent-a", tenant_id="00000000-0000-0000-0000-0000000000aa", role=Role.AGENT)


def _db(monkeypatch, visible):
    reads = []

    class Conn:
        async def fetchval(self, sql, *args):
            reads.append(sql)
            return 1 if visible else None

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    monkeypatch.setattr(media_api, "tenant_tx", tx)
    return reads


def test_a_visible_row_revalidates_with_304_and_no_bytes(monkeypatch):
    media_id = uuid4()
    reads = _db(monkeypatch, visible=True)
    res = asyncio.run(media_api.serve_media(media_id, ctx=CTX, if_none_match=f'"{media_id}"'))
    assert res.status_code == 304
    assert reads == ["SELECT 1 FROM property_media WHERE id = $1"], "a PK read, never the bytes"


def test_a_row_the_caller_cannot_see_is_404_not_304(monkeypatch):
    media_id = uuid4()
    _db(monkeypatch, visible=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(media_api.serve_media(media_id, ctx=CTX, if_none_match=f'"{media_id}"'))
    assert exc.value.status_code == 404
