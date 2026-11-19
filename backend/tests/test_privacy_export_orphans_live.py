"""Audit 29.6: export archives no row points to are found and deleted, and an
expired archive that cannot be deleted for a day raises an alert. Real
PostgreSQL, local storage in a temporary directory. Skipped without
ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_orphaned_export_archives_are_deleted_and_overdue_ones_alert(monkeypatch, tmp_path):
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    asyncio.run(_scenario(tmp_path))


async def _scenario(media_root):
    import asyncpg

    import component_health
    import privacy_export
    from db import connection

    tenant = str(uuid.uuid4())

    def archive(op_id, owner=None, suffix=""):
        key = f"privacy/exports/{owner or tenant}/{op_id}.zip{suffix}"
        (media_root / key).parent.mkdir(parents=True, exist_ok=True)
        (media_root / key).write_bytes(b"PK archive")
        return key

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)

    async def op(state, *, pointed=True, expires="now() + interval '6 days'", touched="now()"):
        op_id = str(uuid.uuid4())
        key = archive(op_id)
        await admin.execute(
            "INSERT INTO privacy_operations (id, tenant_id, kind, requested_by, policy_version, state, artifact_key, "
            f"artifact_expires_at, result, updated_at) VALUES ($1,$2,'export','owner@x.test','v',$3,$4,{expires},"
            f"'{{}}'::jsonb,{touched})", op_id, tenant, state, key if pointed else None)
        return key

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Orphan live')",
                            tenant, f"orph-{tenant[:8]}")
        overdue_before = (await admin.fetchrow(component_health._SQL_EXPORT_CLEANUP))["overdue"]
        live = await op("succeeded")
        building = await op("running", pointed=False)
        overdue = await op("succeeded", expires="now() - interval '2 days'")
        failed = await op("failed", pointed=False)
        stuck = await op("running", pointed=False, touched="now() - interval '3 days'")
        no_row = archive(str(uuid.uuid4()))
        failed_partial = archive(failed.rsplit("/", 1)[1][:-4], suffix=".partial")
        building_partial = archive(building.rsplit("/", 1)[1][:-4], suffix=".partial")
        elsewhere = archive(str(uuid.uuid4()), owner=str(uuid.uuid4()))  # a tenant this DB does not know
        odd = f"privacy/exports/{tenant}/notes.txt"
        (media_root / odd).write_bytes(b"not an archive")

        # A platform context that fell back to the request pool has no
        # platform power and sees no rows: it must delete nothing.
        platform_pool = connection._platform_pool
        connection._platform_pool = None
        try:
            powerless = await privacy_export.sweep_orphan_exports()
        finally:
            connection._platform_pool = platform_pool
        assert powerless == {"skipped": "no_platform_power", "scanned": 10}
        assert all(p.exists() for p in media_root.rglob("*.zip*"))

        out = await privacy_export.sweep_orphan_exports()
        assert out == {"scanned": 10, "kept": 5, "orphans_deleted": 3, "orphans_failed": 0,
                       "unknown_tenant": 1, "unrecognised": 1, "truncated": False}
        for kept in (live, building, overdue, stuck, building_partial, elsewhere, odd):
            assert (media_root / kept).exists(), kept
        for gone in (failed, no_row, failed_partial):
            assert not (media_root / gone).exists(), gone

        # The expired archive its row still points to is expire_exports' job;
        # a day past expiry it is counted for the export_cleanup alert.
        assert (await admin.fetchrow(component_health._SQL_EXPORT_CLEANUP))["overdue"] == overdue_before + 1
        assert component_health._export_cleanup(1)["state"] == component_health.DEGRADED
    finally:
        await admin.execute("DELETE FROM privacy_operations WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()
