"""The property-data health components read real tables (L19-04), against
real PostgreSQL as the platform role (skipped without ORACLE_LIVE_DB_ADMIN_DSN)."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_property_components_read_harvest_sources_and_market_stats(monkeypatch):
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import component_health as ch
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = uuid.uuid4()
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Health')", tenant, f"h-{tenant.hex[:8]}")
        await admin.execute(
            """INSERT INTO harvest_sources (tenant_id, source_key, display_name, jurisdiction, adapter,
                   schedule_seconds, enabled, circuit_state, last_succeeded_at)
               VALUES ($1,'health-open','Open circuit','DE','arcgis',86400,true,'open', now()),
                      ($1,'health-ok','Fresh','DE','arcgis',86400,true,'closed', now())""", tenant)
        out = await ch._db_components()
        harvest = out["property_harvesters"]
        assert harvest["state"] == ch.DEGRADED and harvest["detail"]["open_circuits"] >= 1, harvest
        assert out["market_data"]["state"] in (ch.NOT_CONFIGURED, ch.HEALTHY, ch.STALE), out["market_data"]
        assert set(ch._DB_KEYS) <= set(out)
    finally:
        await admin.execute("DELETE FROM harvest_sources WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()
