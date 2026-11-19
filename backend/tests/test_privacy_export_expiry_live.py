"""An expired export forgets its archive only after storage confirmed the
delete (audit 29.6), on real PostgreSQL. Skipped without
ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_expiry_clears_the_archive_only_after_a_confirmed_delete(monkeypatch, tmp_path):
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    asyncio.run(_scenario(monkeypatch, tmp_path))


async def _scenario(monkeypatch, media_root):
    import asyncpg

    import object_storage
    import privacy_export
    from db import connection

    tenant = str(uuid.uuid4())
    key = f"privacy/exports/{tenant}/op.zip"
    (media_root / key).parent.mkdir(parents=True, exist_ok=True)
    (media_root / key).write_bytes(b"PK archive")
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Expiry live')", tenant, f"exp-{tenant[:8]}")
        op = await admin.fetchval(
            "INSERT INTO privacy_operations (tenant_id, kind, requested_by, policy_version, state, artifact_key, "
            "artifact_expires_at, result) VALUES ($1,'export','owner@x.test','v','succeeded',$2, now() - interval '1 hour', "
            "'{}'::jsonb) RETURNING id", tenant, key)

        async def row():
            r = await admin.fetchrow("SELECT artifact_key, result FROM privacy_operations WHERE id=$1", op)
            return r["artifact_key"], json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]

        # Storage not configured: nothing deleted, so the row keeps its key.
        monkeypatch.setattr(object_storage, "is_configured", lambda: False)
        await privacy_export.expire_exports()
        artifact, result = await row()
        assert artifact == key and result["artifact_delete_pending"] == "storage_not_configured"
        assert (media_root / key).exists()

        # A delete that fails: still kept, reason recorded.
        monkeypatch.setattr(object_storage, "is_configured", lambda: True)

        def refuse(_key):
            raise object_storage.StorageError("denied")

        monkeypatch.setattr(object_storage, "delete_object", refuse)
        await privacy_export.expire_exports()
        artifact, result = await row()
        assert artifact == key and result["artifact_delete_pending"] == "StorageError"

        # A confirmed delete: the file is gone and the row forgets it.
        monkeypatch.undo()
        monkeypatch.setattr(object_storage, "BACKEND", "local")
        monkeypatch.setattr(object_storage, "MEDIA_ROOT", media_root)
        await privacy_export.expire_exports()
        artifact, result = await row()
        assert artifact is None and result.get("artifact") == "expired_and_deleted"
        assert "artifact_delete_pending" not in result and not (media_root / key).exists()
    finally:
        await admin.execute("DELETE FROM privacy_operations WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()
