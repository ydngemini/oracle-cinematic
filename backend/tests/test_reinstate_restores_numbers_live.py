"""Reinstating a member in Team switches back on the numbers a closure
withdrawal left off because of them (L17-59, owner instruction 2026-11-27,
'phone numbers back on'), against real PostgreSQL: RLS, the partial unique
indexes and the closure operation's real result.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_reinstating_a_left_off_member_brings_their_numbers_back(monkeypatch):
    import recovery_mode

    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    asyncio.run(_scenario())


def _did():
    return "+1302" + str(secrets.randbelow(10**7)).zfill(7)


async def _scenario():
    import asyncpg

    import brokerage_onboarding as bo
    import privacy_lifecycle as pl
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
    owner, name = f"owner-{s}@live.test", f"Brokerage {s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    sid = "AC" + "0" * 32

    async def add_route(tenant, agent, active=True, did=None):
        return await admin.fetchval(
            "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, agent_forward_e164, active, provider, "
            "twilio_account_sid, provider_account_id) VALUES ($1,$2,$3,'+13025550188',$4,'twilio',$5,$6) "
            "RETURNING id::text",
            tenant, agent, did or _did(), active, sid, sid)

    try:
        for tid, slug in ((T, f"rrn-{s}"), (OTHER, f"rrn-other-{s}")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", tid, slug, name)
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        # the owner has a number of their own, so offboarding to them parks
        # the departing agent's (_hand_over_routes)
        await add_route(T, owner)
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
        why = bo.MemberStatusChange(reason="owner decision")

        # K: reinstated, numbers back. M: never reinstated. O: offboarded
        # after the withdrawal (number parked). C: its call number reused by
        # another brokerage meanwhile. Agent ids stored in mixed case: the
        # match is case-insensitive.
        team = {}
        for tag in ("K", "M", "O", "C"):
            who = f"{tag}-{s}@Live.test"
            uid = await admin.fetchval(
                "INSERT INTO users (tenant_id, agent_id, role, email, password_hash, full_name) "
                "VALUES ($1,$2,'agent',$3,'scrypt$x$y',$4) RETURNING id", T, who, who.lower(), f"Agent {tag}")
            await admin.execute("INSERT INTO team_memberships (tenant_id, user_id, team_name, status) "
                                "VALUES ($1,$2,'Team','active')", T, uid)
            tr = await add_route(T, who)
            mr = await admin.fetchval("INSERT INTO messaging_routes (tenant_id, agent_id, active) "
                                      "VALUES ($1,$2,true) RETURNING id::text", T, who.upper())
            team[tag] = {"user": uid, "telephony": tr, "messaging": mr, "agent": who}

        async def state_of(tag):
            t = team[tag]
            return (await admin.fetchval("SELECT is_active FROM users WHERE id=$1", t["user"]),
                    await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1::uuid", t["telephony"]),
                    await admin.fetchval("SELECT active FROM messaging_routes WHERE id=$1::uuid", t["messaging"]))

        def routes(*tags):
            return sorted(team[t][r] for t in tags for r in ("telephony", "messaging"))

        # ── a closure; during it the owner reinstates and suspends all four,
        # so the withdrawal leaves them and their numbers off (L17-58)
        closure = await pl.request_closure(octx, confirm_name=name, reason="closing")
        for tag in team:
            await bo._reinstate(octx, team[tag]["user"], why)       # nothing to restore yet
            await bo.suspend_member(team[tag]["user"], why, ctx=octx)
        back = await pl.withdraw_closure(octx, reason="keep the brokerage")
        assert sorted(back["skipped_routes"]) == routes("K", "M", "O", "C")
        recorded = json.loads(await admin.fetchval(
            "SELECT result FROM privacy_operations WHERE id=$1", closure["operation_id"]))
        assert sorted(recorded["skipped_routes"]) == routes("K", "M", "O", "C")
        for tag in team:
            assert await state_of(tag) == (False, False, False), tag

        # O offboarded to the owner: O's number is parked (owner has one)
        done = await pl.offboard_agent(octx, departing_agent_id=team["O"]["agent"],
                                       successor_agent_id=owner, reason="left", preview=False)
        assert done["telephony"] == "parked_successor_has_own_route", done
        # C's inbound number taken by another brokerage while C's route is off
        c_did = await admin.fetchval("SELECT inbound_did FROM telephony_routes WHERE id=$1::uuid",
                                     team["C"]["telephony"])
        other_route = await add_route(OTHER, f"x-{s}@live.test", did=c_did)

        # ── K reinstated: K's two numbers back on; M's and the parked O's not
        out = await bo.reinstate_member(team["K"]["user"], why, ctx=octx)
        assert sorted(out["restored_routes"]) == routes("K") and out["routes_not_restored"] == []
        assert await state_of("K") == (True, True, True)
        assert await state_of("M") == (False, False, False)
        assert await state_of("O") == (False, False, False)
        assert await state_of("C") == (False, False, False)
        audit = json.loads(await admin.fetchval(
            "SELECT metadata FROM audit_ledger WHERE tenant_id=$1 AND target_id=$2 "
            "AND action='team.member.reinstated' ORDER BY created_at DESC LIMIT 1", T, team["K"]["user"]))
        assert audit["restored_routes"] == 2 and audit["routes_not_restored"] == 0
        assert not any(r in json.dumps(audit) for r in routes("K"))         # counts only

        # ── O reinstated: the sign-in comes back, the parked number does not
        out = await bo.reinstate_member(team["O"]["user"], why, ctx=octx)
        assert out["restored_routes"] == []
        assert sorted((r["id"], r["kind"], r["reason"]) for r in out["routes_not_restored"]) == sorted(
            [(team["O"]["telephony"], "call", "offboarded"), (team["O"]["messaging"], "text", "offboarded")])
        assert await state_of("O") == (True, False, False)

        # ── C reinstated: the reused call number is refused by
        # uq_telephony_routes_active_did (held in another brokerage, invisible
        # here); C, and C's text route, come back all the same
        out = await bo.reinstate_member(team["C"]["user"], why, ctx=octx)
        assert out["restored_routes"] == [team["C"]["messaging"]]
        assert out["routes_not_restored"] == [
            {"id": team["C"]["telephony"], "kind": "call", "reason": "number_in_use"}]
        assert await state_of("C") == (True, False, True)
        assert await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1::uuid", other_route)

        # ── a plain suspension leaves the numbers on: nothing to restore
        await bo.suspend_member(team["K"]["user"], why, ctx=octx)
        assert await state_of("K") == (False, True, True)
        out = await bo.reinstate_member(team["K"]["user"], why, ctx=octx)
        assert out["restored_routes"] == [] and out["routes_not_restored"] == []
        assert await state_of("K") == (True, True, True)

        # ── while a new closure runs, reinstating M restores none of M's
        # numbers (the brokerage is closing); they stay off after it
        again = await pl.request_closure(octx, confirm_name=name, reason="closing again")
        out = await bo.reinstate_member(team["M"]["user"], why, ctx=octx)
        assert out["restored_routes"] == []
        assert sorted((r["id"], r["reason"]) for r in out["routes_not_restored"]) == sorted(
            (rid, "brokerage_not_active") for rid in routes("M"))
        assert await state_of("M") == (True, False, False)
        await pl.withdraw_closure(octx, reason="keep it")
        assert again["operation_id"]
        assert await state_of("M") == (True, False, False)
        assert await state_of("O") == (True, False, False)              # parked stays parked
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for tid in (T, OTHER):
                for table in await admin.fetch(
                        "SELECT c.table_name FROM information_schema.columns c "
                        "JOIN information_schema.tables t USING (table_schema, table_name) "
                        "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                        "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                    await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', tid)
                await admin.execute("DELETE FROM tenants WHERE id=$1", tid)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
