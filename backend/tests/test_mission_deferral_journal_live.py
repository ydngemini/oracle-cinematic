"""A deferred mission step's journal row is stored (0159, L08-02), against
real PostgreSQL as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN). Before 0159 the insert was refused by
mission_events_kind_check and executor._journal swallowed the error."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_a_deferral_is_journalled():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    from db import connection
    from missions import executor
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant, mission = uuid.uuid4(), uuid.uuid4()
    ctx = TenantContext(agent_id="mission-owner@defer.test", tenant_id=str(tenant), role=Role.BROKER_OWNER)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Defer')", tenant, f"defer-{tenant.hex[:8]}")
        await admin.execute(
            "INSERT INTO missions (id, tenant_id, objective_kind, objective_text, created_by) "
            "VALUES ($1,$2,'sphere_touched','Follow up','x')",
            mission, tenant)
        await executor._journal(ctx, mission, "action_deferred",
                                {"action_id": str(uuid.uuid4()), "until": "2026-10-16T09:00:00+00:00"})
        kinds = [r["kind"] for r in await admin.fetch(
            "SELECT kind FROM mission_events WHERE mission_id=$1", mission)]
        assert kinds == ["action_deferred"], kinds
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("mission_events", "missions"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
