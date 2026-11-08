"""AC08.10 against real PostgreSQL with FORCE RLS, as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN). The unit tests fake the tables, so
these are what prove the statuses, states and journal kinds the schema accepts.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def _ctx(tenant: str):
    from tenancy import Role, TenantContext

    return TenantContext(agent_id="agent@live.test", tenant_id=tenant, role=Role.AGENT)


def _run(scenario):
    async def wrapper():
        import asyncpg

        from db import connection

        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=3)
        tenants: list[str] = []
        try:
            await scenario(admin, tenants)
        finally:
            await connection.close_pool()
            for tenant in tenants:
                await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
            await admin.close()

    asyncio.run(wrapper())


async def _brokerage(admin, tenants: list[str]) -> str:
    tenant = str(uuid.uuid4())
    tenants.append(tenant)
    await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Mission test')",
                        tenant, f"mission-{tenant[:8]}")
    return tenant


async def _mission(admin, tenant: str, status: str, states: tuple[str, ...] = ()) -> str:
    mission = await admin.fetchval(
        "INSERT INTO missions (tenant_id, objective_kind, objective_text, status) "
        "VALUES ($1,'sphere_touched','Stay in touch',$2) RETURNING id::text", tenant, status)
    for state in states:
        await admin.execute(
            "INSERT INTO mission_actions (tenant_id, mission_id, channel, state) "
            "VALUES ($1,$2::uuid,'email',$3)", tenant, mission, state)
    return mission


@pytest.fixture(autouse=True)
def feature_on(monkeypatch):
    import missions_api

    monkeypatch.setattr(missions_api, "require_feature", lambda _feature: None)


def test_cancel_writes_what_the_schema_accepts_and_journals_once():
    import missions_api

    async def scenario(admin, tenants):
        tenant = await _brokerage(admin, tenants)
        mission = await _mission(admin, tenant, "active",
                                 ("planned", "planned", "staged", "approved", "executed"))

        out = await missions_api.cancel_mission(mission, _ctx(tenant))
        again = await missions_api.cancel_mission(mission, _ctx(tenant))

        assert out["mission"]["status"] == "cancelled"
        assert out["actions_skipped"] == 2
        assert out["commands_already_staged_or_approved"] == 2
        assert again["already_cancelled"] is True and again["actions_skipped"] == 0
        states = await admin.fetch(
            "SELECT state, blocked_reason FROM mission_actions WHERE mission_id=$1::uuid", mission)
        assert sorted(r["state"] for r in states) == [
            "approved", "executed", "skipped", "skipped", "staged"]
        assert {r["blocked_reason"] for r in states if r["state"] == "skipped"} == {"mission cancelled"}
        kinds = await admin.fetch(
            "SELECT kind FROM mission_events WHERE mission_id=$1::uuid", mission)
        assert [r["kind"] for r in kinds] == ["cancelled"]

    _run(scenario)


def test_another_brokerage_cannot_see_or_cancel_the_mission():
    import missions_api

    async def scenario(admin, tenants):
        mine = await _brokerage(admin, tenants)
        theirs = await _brokerage(admin, tenants)
        mission = await _mission(admin, mine, "active", ("planned",))

        with pytest.raises(HTTPException) as exc:
            await missions_api.cancel_mission(mission, _ctx(theirs))

        assert exc.value.status_code == 404
        assert await admin.fetchval(
            "SELECT status FROM missions WHERE id=$1::uuid", mission) == "active"
        assert await admin.fetchval(
            "SELECT state FROM mission_actions WHERE mission_id=$1::uuid", mission) == "planned"

    _run(scenario)


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_nothing_moves_a_finished_mission(status):
    import missions_api

    async def scenario(admin, tenants):
        tenant = await _brokerage(admin, tenants)
        mission = await _mission(admin, tenant, status)
        body = missions_api.LaunchRequest(mode="shadow")

        for attempt in (missions_api.pause_mission(mission, _ctx(tenant)),
                        missions_api.launch_mission(mission, body, _ctx(tenant))):
            with pytest.raises(HTTPException) as exc:
                await attempt
            assert exc.value.status_code == 409
        assert await admin.fetchval(
            "SELECT status FROM missions WHERE id=$1::uuid", mission) == status

    _run(scenario)


def test_a_cancel_landing_inside_launch_is_not_undone(monkeypatch):
    """Launch reads the mission, then writes it. A cancel between the two used
    to be overwritten, putting a cancelled mission back to work."""
    import missions_api

    real_load = missions_api._load

    async def scenario(admin, tenants):
        tenant = await _brokerage(admin, tenants)
        mission = await _mission(admin, tenant, "simulated")

        async def load_then_cancel(ctx, mission_id):
            row = await real_load(ctx, mission_id)
            await admin.execute("UPDATE missions SET status='cancelled' WHERE id=$1::uuid", mission)
            return row

        monkeypatch.setattr(missions_api, "_load", load_then_cancel)
        with pytest.raises(HTTPException) as exc:
            await missions_api.launch_mission(
                mission, missions_api.LaunchRequest(mode="shadow"), _ctx(tenant))

        assert exc.value.status_code == 409
        assert await admin.fetchval(
            "SELECT status FROM missions WHERE id=$1::uuid", mission) == "cancelled"

    _run(scenario)
