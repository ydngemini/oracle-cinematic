"""Deleting a photo or 3D space removes its stored files, not just the row.

The route used to delete only the property_media row, leaving the object (and
a space's companion files) in the bucket with nothing pointing at it. Files are
now removed first; if any removal fails, nothing is deleted and the caller can
retry.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException

import media_api
import object_storage
import space_assets
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="agent@x.test", tenant_id="00000000-0000-0000-0000-0000000000aa", role=Role.AGENT)


def _setup(monkeypatch, row, *, configured=True, failed=0):
    state = {"deleted_rows": 0, "deleted_keys": []}

    class Conn:
        async def fetchrow(self, sql, _media_id):
            if sql.startswith("SELECT"):
                return row
            if row is None:
                return None
            state["deleted_rows"] += 1
            return {"id": row["id"]}

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    def delete_objects(keys):
        keys = list(keys)
        state["deleted_keys"] = keys
        return {"deleted": len(keys) - failed, "failed": failed}

    monkeypatch.setattr(media_api, "tenant_tx", tx)
    monkeypatch.setattr(object_storage, "is_configured", lambda: configured)
    monkeypatch.setattr(space_assets, "delete_objects", delete_objects)
    return state


def test_photo_file_is_removed_before_its_row(monkeypatch):
    mid = uuid4()
    state = _setup(monkeypatch, {"id": mid, "kind": "photo", "s3_key": "media/t/p.jpg"})
    out = asyncio.run(media_api.delete_media(mid, CTX))
    assert out["deleted"] is True
    assert state["deleted_keys"] == ["media/t/p.jpg"] and state["deleted_rows"] == 1


def test_space_removes_its_companion_files(monkeypatch):
    mid = uuid4()
    state = _setup(monkeypatch, {"id": mid, "kind": "splat", "s3_key": "splats/t/space.sog"})
    asyncio.run(media_api.delete_media(mid, CTX))
    assert "splats/t/space.sog" in state["deleted_keys"]
    assert len(state["deleted_keys"]) > 1      # manifest / companions too
    assert state["deleted_rows"] == 1


def test_failed_file_removal_deletes_nothing(monkeypatch):
    mid = uuid4()
    state = _setup(monkeypatch, {"id": mid, "kind": "photo", "s3_key": "media/t/p.jpg"}, failed=1)
    with pytest.raises(HTTPException) as err:
        asyncio.run(media_api.delete_media(mid, CTX))
    assert err.value.status_code == 503 and state["deleted_rows"] == 0


def test_blob_backed_photo_needs_no_storage_call(monkeypatch):
    mid = uuid4()
    state = _setup(monkeypatch, {"id": mid, "kind": "photo", "s3_key": None})
    asyncio.run(media_api.delete_media(mid, CTX))
    assert state["deleted_keys"] == [] and state["deleted_rows"] == 1


def test_unknown_media_is_404(monkeypatch):
    _setup(monkeypatch, None)
    with pytest.raises(HTTPException) as err:
        asyncio.run(media_api.delete_media(uuid4(), CTX))
    assert err.value.status_code == 404
