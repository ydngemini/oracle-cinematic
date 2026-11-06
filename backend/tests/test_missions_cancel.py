"""AC08.10: a mission can be cancelled, and cancelling stops later effects.

The routes are driven against an in-memory stand-in for the two tables they
touch, so the assertions are about which rows change and which do not. They do
not prove the SQL against PostgreSQL; that check is still owed.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

import pytest
from fastapi import HTTPException

import missions_api
from missions import executor
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)
MID = "22222222-2222-2222-2222-222222222222"


class FakeDb:
    def __init__(self, status, actions=()):
        self.mission = {
            "id": MID, "objective_kind": "sphere_touched", "objective_text": "x",
            "status": status, "mode": "shadow", "simulated_at": None,
            "launched_at": None, "created_at": dt.datetime(2026, 1, 1),
            "allowed_channels": [], "auto_channels": [], "constraints": {},
        }
        self.actions = [dict(state=s) for s in actions]
        self.events = []
        self.writes = 0

    async def fetchrow(self, sql, *args):
        if sql.lstrip().startswith("SELECT * FROM missions"):
            return dict(self.mission)
        if "UPDATE missions" in sql:
            if self.mission["status"] in missions_api.TERMINAL_STATUSES:
                return None
            self.writes += 1
            self.mission["status"] = "cancelled" if "SET status = 'cancelled'" in sql else "paused"
            return dict(self.mission)
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "UPDATE mission_actions" in sql:
            hit = [a for a in self.actions if a["state"] == "planned"]
            for a in hit:
                a["state"] = "skipped"
                a["reason"] = "mission cancelled"
            return len(hit)
        if "state IN ('staged', 'approved')" in sql:
            return len([a for a in self.actions if a["state"] in ("staged", "approved")])
        if sql.startswith("SELECT status FROM missions"):
            return self.mission["status"]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "INSERT INTO mission_events" in sql:
            self.events.append(args[2])


@pytest.fixture
def db(monkeypatch):
    def install(status, actions=()):
        fake = FakeDb(status, actions)

        @contextlib.asynccontextmanager
        async def tx(_ctx):
            yield fake

        monkeypatch.setattr(missions_api, "tenant_tx", tx)
        monkeypatch.setattr(missions_api, "require_feature", lambda _f: None)
        monkeypatch.setattr("db.connection.tenant_tx", tx)
        return fake

    return install


def run(coro):
    return asyncio.run(coro)


def test_cancel_skips_only_planned_actions_and_reports_in_flight(db):
    fake = db("active", ["planned", "planned", "staged", "approved", "executed"])
    out = run(missions_api.cancel_mission(MID, CTX))
    assert out["mission"]["status"] == "cancelled"
    assert out["actions_skipped"] == 2
    assert out["commands_already_staged_or_approved"] == 2
    assert [a["state"] for a in fake.actions] == [
        "skipped", "skipped", "staged", "approved", "executed"]
    assert fake.events == ["cancelled"]


def test_cancel_is_idempotent(db):
    fake = db("cancelled", ["skipped"])
    out = run(missions_api.cancel_mission(MID, CTX))
    assert out["already_cancelled"] is True
    assert fake.writes == 0 and fake.events == []


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_cancel_refuses_a_finished_mission(db, status):
    fake = db(status, ["planned"])
    with pytest.raises(HTTPException) as err:
        run(missions_api.cancel_mission(MID, CTX))
    assert err.value.status_code == 409
    assert fake.actions[0]["state"] == "planned"


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_pause_cannot_revive_a_terminal_mission(db, status):
    fake = db(status)
    with pytest.raises(HTTPException) as err:
        run(missions_api.pause_mission(MID, CTX))
    assert err.value.status_code == 409
    assert fake.mission["status"] == status and fake.writes == 0


def test_pause_still_works_on_a_live_mission(db):
    db("active")
    assert run(missions_api.pause_mission(MID, CTX))["mission"]["status"] == "paused"


def test_cancelled_event_kind_is_allowed_by_the_schema():
    import pathlib

    sql = (pathlib.Path(__file__).resolve().parents[1]
           / "db" / "migrations" / "0100_missions.sql").read_text(encoding="utf-8")
    assert "'failed', 'cancelled')),\n    detail" in sql
    assert "'cancelled')),\n    mode" in sql


@pytest.mark.parametrize("status,expected", [
    ("active", True), ("shadow", True),
    ("cancelled", False), ("paused", False), ("completed", False),
])
def test_executor_only_stages_while_the_mission_is_running(db, status, expected):
    db(status)
    assert run(executor._still_running(CTX, MID)) is expected


def test_work_one_rechecks_status_before_staging():
    import inspect

    source = inspect.getsource(executor._work_one)
    assert "_still_running" in source
    assert source.index("_still_running") < source.index("await _stage(")
