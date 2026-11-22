"""Closing and reopening a brokerage reach Stripe through a durable job and
report only what Stripe confirmed (external audit 22.1), against real
PostgreSQL.

The job row commits with the closure (and with the withdrawal), is claimable
while the brokerage is closing (privacy: jobs are), derives the wanted renewal
from tenants.lifecycle_state when it runs, confirms with a read-back, and on
its final attempt leaves `divergent` on the closure operation, which the
external_cleanup alert counts until a later confirmation settles it.

Stripe is a stub that records calls. Skipped unless ORACLE_LIVE_DB_ADMIN_DSN
is set (see test_privacy_lifecycle_live).
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


class _Stripe:
    def __init__(self):
        self.cancel, self.up, self.calls = False, False, []

    def modify(self, sub, *, cancel_at_period_end, max_network_retries):
        self.calls.append(("modify", sub, cancel_at_period_end))
        if not self.up:
            raise TimeoutError("api.stripe.com timed out")
        self.cancel = cancel_at_period_end
        return {"id": sub, "cancel_at_period_end": cancel_at_period_end}

    def retrieve(self, sub, *, max_network_retries):
        if not self.up:
            raise TimeoutError("api.stripe.com timed out")
        return {"id": sub, "cancel_at_period_end": self.cancel}


class _Reporter:
    def __init__(self, job: dict):
        self.job = job

    async def progress(self, *_a, **_k):
        return None


def test_closure_billing_against_real_postgres(monkeypatch):
    import recovery_mode
    import stripe

    fake = _Stripe()
    monkeypatch.setattr(recovery_mode, "guard", lambda *_a, **_k: None)
    monkeypatch.setattr(stripe, "api_key", "sk_test_" + "stub")
    monkeypatch.setattr(stripe.Subscription, "modify", fake.modify)
    monkeypatch.setattr(stripe.Subscription, "retrieve", fake.retrieve)
    asyncio.run(_scenario(fake, monkeypatch))


async def _scenario(fake, monkeypatch):
    import asyncpg

    import component_health
    import privacy_lifecycle as pl
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner, name, sub = f"owner-{s}@live.test", f"Brokerage {s}", f"sub_live_{s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def jobs():
        return [dict(r) for r in await admin.fetch(
            "SELECT * FROM automation_jobs WHERE tenant_id=$1 AND job_type='privacy:billing_renewal' "
            "ORDER BY created_at", T)]

    async def run(job, attempt):
        job = {**job, "attempt_count": attempt}
        return await pl._billing_renewal_job(json.loads(job["payload"]), _Reporter(job))

    async def billing_of(op_id):
        return json.loads(await admin.fetchval(
            "SELECT progress->'billing' FROM privacy_operations WHERE id=$1", op_id))

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"bill-{s}", name)
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        await admin.execute("INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, "
                            "status) VALUES ($1,$2,$3,'active')", T, f"cus_{s}", sub)
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
        # L17-58: an agent each closure signs out and whose numbers it switches
        # off, and one already signed out with numbers already off before any
        # closure, which no withdrawal may switch back on.
        live, idle = f"agent-{s}@live.test", f"idle-{s}@live.test"
        agent = {}
        for who, on in ((live, True), (idle, False)):
            uid = await admin.fetchval(
                "INSERT INTO users (tenant_id, agent_id, role, email, password_hash, is_active) "
                "VALUES ($1,$2,'agent',$2,'scrypt$x$y',$3) RETURNING id::text", T, who, on)
            tr = await admin.fetchval(
                "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, agent_forward_e164, active, "
                "provider, twilio_account_sid, provider_account_id) "
                "VALUES ($1,$2,$3,'+13025550188',$4,'twilio',$5,$6) RETURNING id::text",
                T, who, "+1302" + str(secrets.randbelow(10**7)).zfill(7), on, "AC" + "0" * 32, "AC" + "0" * 32)
            mr = await admin.fetchval("INSERT INTO messaging_routes (tenant_id, agent_id, active) "
                                      "VALUES ($1,$2,$3) RETURNING id::text", T, who, on)
            agent[who] = {"user": uid, "telephony": tr, "messaging": mr}

        async def switched_on(who):
            a = agent[who]
            return (await admin.fetchval("SELECT is_active FROM users WHERE id=$1::uuid", a["user"]),
                    await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1::uuid", a["telephony"]),
                    await admin.fetchval("SELECT active FROM messaging_routes WHERE id=$1::uuid", a["messaging"]))

        async def op_row(op_id):
            row = await admin.fetchrow("SELECT state, result, error, completed_at FROM privacy_operations "
                                       "WHERE id=$1", op_id)
            return {**dict(row), "result": json.loads(row["result"])}

        # ── Stripe down at closure: closing, honest "pending", job committed
        closing = await pl.request_closure(octx, confirm_name=name, reason="closing")
        assert closing["state"] == "closing" and closing["billing"] == "pending"
        [close_job] = await jobs()
        assert close_job["state"] == "queued" and close_job["max_attempts"] == pl.BILLING_MAX_ATTEMPTS
        assert close_job["idempotency_key"] == f"privacy:billing:{closing['operation_id']}:close"
        # claimable although the brokerage is closing (privacy: jobs are)
        assert await admin.fetchval(
            "SELECT 1 FROM automation_jobs WHERE id=$1 AND (job_type LIKE 'privacy:%' OR NOT EXISTS ("
            "SELECT 1 FROM tenants t WHERE t.id = automation_jobs.tenant_id AND t.lifecycle_state <> 'active'))",
            close_job["id"])
        assert (await billing_of(closing["operation_id"]))["state"] == "pending"
        assert (await pl.tenant_lifecycle(octx))["billing_renewal"]["state"] == "pending"

        # ── the job, Stripe back: set, read back, confirmed ──────────────
        fake.up = True
        assert (await run(close_job, 2))["state"] == "confirmed"
        assert fake.cancel is True
        lifecycle = await pl.tenant_lifecycle(octx)
        assert lifecycle["billing_renewal"]["state"] == "confirmed"
        assert lifecycle["billing_renewal"]["desired"] == "stop_renewal"

        # ── withdraw with Stripe down; the CLOSE job runs again afterwards ─
        fake.up = False
        assert await switched_on(live) == (False, False, False)
        reopened = await pl.withdraw_closure(octx, reason="changed mind")
        assert reopened["state"] == "active" and reopened["billing"] == "pending"
        assert reopened["restored_users"] == 1 and reopened["restored_routes"] == 2
        assert await switched_on(live) == (True, True, True)
        assert await switched_on(idle) == (False, False, False)
        assert len(await jobs()) == 2
        fake.up = True
        assert (await run(close_job, 3))["desired"] == "renew"    # derived from 'active' now
        assert fake.cancel is False
        assert (await billing_of(closing["operation_id"]))["state"] == "confirmed"

        # ── final attempt with Stripe down: divergent + the alert ─────────
        fake.up = False
        again = await pl.request_closure(octx, confirm_name=name, reason="closing for good")
        third = (await jobs())[-1]
        out = await run(third, third["max_attempts"])
        assert out["state"] == "divergent" and out["_terminal_state"] == "partial"
        assert (await billing_of(again["operation_id"]))["state"] == "divergent"
        health = (await component_health._db_components())["external_cleanup"]
        assert health["state"] == component_health.DEGRADED and health["detail"]["billing_divergent"] >= 1

        # ── a later confirmation settles it ───────────────────────────────
        fake.up = True
        assert (await run(third, 1))["state"] == "confirmed" and fake.cancel is True
        assert (await billing_of(again["operation_id"]))["state"] == "confirmed"
        assert await admin.fetchval(
            "SELECT count(*) FROM privacy_operations WHERE tenant_id=$1 AND kind='closure' "
            "AND progress->'billing'->>'state' = 'divergent'", T) == 0

        # ── review of 22.1: withdraw and close again before anything confirms
        # The older closure's 'pending' can never be confirmed (every later
        # job writes the newest closure): it is superseded at once, and only
        # each brokerage's latest closure counts towards the alert.
        fake.up = False
        await pl.withdraw_closure(octx, reason="changed mind again")          # again: {renew, pending}
        assert (await billing_of(again["operation_id"]))["state"] == "pending"
        third_close = await pl.request_closure(octx, confirm_name=name, reason="closing, final answer")
        assert (await billing_of(again["operation_id"]))["state"] == "superseded"
        assert (await billing_of(third_close["operation_id"]))["state"] == "pending"

        async def stuck():
            return (await component_health._db_components())["external_cleanup"]["detail"]["stuck_pending"]

        base = await stuck()
        await admin.execute(
            "UPDATE privacy_operations SET progress = jsonb_set(progress, '{billing,state}', '\"pending\"'), "
            "updated_at = now() - interval '7 hours' WHERE id=$1", again["operation_id"])
        assert await stuck() == base                       # an older closure is history
        await admin.execute("UPDATE privacy_operations SET updated_at = now() - interval '7 hours' WHERE id=$1",
                            third_close["operation_id"])
        assert await stuck() == base + 1                   # the latest one, stuck, does page

        # ── review of 22.1: a closure blocked by a legal hold, then withdrawn
        # No running closure operation: the record goes to the latest one
        # (the blocked closure being withdrawn), which tenant_lifecycle reads.
        # L17-58: an older closure the pre-fix withdrawal left 'blocked_legal_hold'
        # (its freeze list names the idle agent) is history, never restored from.
        stale = await admin.fetchval(
            "INSERT INTO privacy_operations (tenant_id, kind, state, subject_kind, subject_ref, requested_by, "
            "requested_at, progress, error, policy_version) VALUES ($1,'closure','blocked_legal_hold','tenant',"
            "$2,$3, now() - interval '1 day', $4::jsonb, 'legal hold earlier', 'test') RETURNING id",
            T, T, owner, json.dumps({"freeze": {"signed_out_user_ids": [agent[idle]["user"]], "routes": {
                "telephony_routes": [agent[idle]["telephony"]], "messaging_routes": [agent[idle]["messaging"]]}}}))
        hold = await admin.fetchval("INSERT INTO legal_holds (tenant_id, scope, reason, placed_by) "
                                    "VALUES ($1,'tenant','litigation','counsel') RETURNING id", T)
        assert await pl.begin_erasure(T, requested_by="test") is None
        assert await admin.fetchval("SELECT state FROM privacy_operations WHERE id=$1",
                                    third_close["operation_id"]) == "blocked_legal_hold"
        assert await switched_on(live) == (False, False, False)
        fake.up = True
        withdrawn = await pl.withdraw_closure(octx, reason="settled")
        assert withdrawn["billing"] == "confirmed" and fake.cancel is False
        # Everything the blocked closure froze is back, nothing else is.
        assert withdrawn["restored_users"] == 1 and withdrawn["restored_routes"] == 2
        assert await switched_on(live) == (True, True, True)
        assert await switched_on(idle) == (False, False, False)
        # Finished as a withdrawn closure, the hold still on record and not lifted.
        blocked = await op_row(third_close["operation_id"])
        assert blocked["state"] == "cancelled" and blocked["completed_at"] is not None
        assert blocked["result"]["withdrawn_by"] == owner
        assert blocked["result"]["withdrawn_from"] == "blocked_legal_hold"
        assert blocked["error"] == f"legal hold {hold}"
        assert await admin.fetchval("SELECT released_at FROM legal_holds WHERE id=$1", hold) is None
        assert (await op_row(stale))["state"] == "blocked_legal_hold"
        assert "withdrawn_by" not in (await op_row(stale))["result"]
        assert await billing_of(third_close["operation_id"]) == {
            **await billing_of(third_close["operation_id"]), "desired": "renew", "state": "confirmed"}
        lifecycle = await pl.tenant_lifecycle(octx)
        assert lifecycle["lifecycle_state"] == "active" and lifecycle["legal_hold"] is True
        assert lifecycle["billing_renewal"]["desired"] == "renew"
        assert lifecycle["billing_renewal"]["state"] == "confirmed"
        assert (await jobs())[-1]["idempotency_key"] == f"privacy:billing:{third_close['operation_id']}:withdraw"
        # Withdrawing twice: the second is refused and changes nothing.
        with pytest.raises(pl.LifecycleError, match="grace period"):
            await pl.withdraw_closure(octx, reason="again")
        assert (await op_row(third_close["operation_id"]))["result"]["withdraw_reason"] == "settled"

        # ── L17-58: the scheduler's erasure and the withdrawal at the same time,
        # the hold still standing. Both lock the tenants row first. A third
        # connection holds that lock while the two queue behind it in a chosen
        # order (PostgreSQL grants a row lock to its waiters in turn), so each
        # order is run, not left to chance: the erasure first blocks the
        # closure and the withdrawal then restores from the blocked one; the
        # withdrawal first reopens and the erasure is refused. The end is the
        # same either way.
        async def waiting_on_tenant_lock(n):
            for _ in range(400):
                if await admin.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND query LIKE '%FROM tenants WHERE id=$1 FOR UPDATE%'") >= n:
                    return
                await asyncio.sleep(0.025)
            raise AssertionError(f"{n} callers never queued on the tenants row")

        async def erase():
            try:
                return await pl.begin_erasure(T, requested_by="retention-scheduler")
            except pl.LifecycleError as exc:
                return str(exc)

        for first in ("erase", "withdraw"):
            closure = await pl.request_closure(octx, confirm_name=name, reason=f"closing ({first} first)")
            assert await switched_on(live) == (False, False, False)
            locker = await asyncpg.connect(DSN)
            try:
                lock = locker.transaction()
                await lock.start()
                await locker.execute("SELECT 1 FROM tenants WHERE id=$1 FOR UPDATE", T)
                racers = {}
                for who in ((first, "withdraw" if first == "erase" else "erase")):
                    racers[who] = asyncio.ensure_future(
                        erase() if who == "erase" else pl.withdraw_closure(octx, reason="raced"))
                    await waiting_on_tenant_lock(len(racers))
                await lock.rollback()
                erased, withdrawn = await racers["erase"], await racers["withdraw"]
            finally:
                await locker.close()
            if first == "erase":
                assert erased is None                      # blocked by the hold, then withdrawn
            else:
                assert erased.startswith("tenant is active"), erased
            assert withdrawn["restored_users"] == 1 and withdrawn["restored_routes"] == 2
            assert await admin.fetchval("SELECT lifecycle_state FROM tenants WHERE id=$1", T) == "active"
            assert await switched_on(live) == (True, True, True)
            assert await switched_on(idle) == (False, False, False)
            raced = await op_row(closure["operation_id"])
            assert raced["state"] == "cancelled", raced
            assert (raced["result"].get("withdrawn_from") == "blocked_legal_hold") == (first == "erase")
            assert await admin.fetchval("SELECT count(*) FROM privacy_operations WHERE tenant_id=$1 "
                                        "AND kind='erasure'", T) == 0
            assert await admin.fetchval("SELECT released_at FROM legal_holds WHERE id=$1", hold) is None

        # ── review of PR #185: members the owner switched off during the
        # closure stay off, with their numbers. Three agents, frozen by this
        # closure; each reinstated in Team, then:
        #   K (membership row) suspended, the audit write failing after commit:
        #     only the membership marks it;
        #   N (no membership row) suspended: only the audit event marks it;
        #   P (membership row) suspended and reinstated again, the second audit
        #     write failing: the audit's last word says suspended, but P is on,
        #     so P and P's numbers come back.
        # And S (membership row), suspended in Team BEFORE this closure, then
        # put back on by plain SQL (no newer Team event), so this closure
        # froze S: the only suspension S has on record is older than the
        # closure, and the lookup is bounded to events since it
        # (_SWITCHED_OFF_SINCE_SQL, a.created_at > requested_at), so S and
        # S's numbers come back.
        import audit_ledger
        import brokerage_onboarding as bo

        team = {}
        names = {"K": "Kim Keller", "N": None, "P": "Pat Park", "S": "Sam Stone"}
        for tag, membership in (("K", True), ("N", False), ("P", True), ("S", True)):
            who = f"{tag.lower()}-{s}@live.test"
            uid = await admin.fetchval(
                "INSERT INTO users (tenant_id, agent_id, role, email, password_hash, full_name) "
                "VALUES ($1,$2,'agent',$2,'scrypt$x$y',$3) RETURNING id", T, who, names[tag])
            if membership:
                await admin.execute("INSERT INTO team_memberships (tenant_id, user_id, team_name, status) "
                                    "VALUES ($1,$2,'Team','active')", T, uid)
            tr = await admin.fetchval(
                "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, agent_forward_e164, active, "
                "provider, twilio_account_sid, provider_account_id) "
                "VALUES ($1,$2,$3,'+13025550199',true,'twilio',$4,$5) RETURNING id::text",
                T, who, "+1302" + str(secrets.randbelow(10**7)).zfill(7), "AC" + "0" * 32, "AC" + "0" * 32)
            mr = await admin.fetchval("INSERT INTO messaging_routes (tenant_id, agent_id, active) "
                                      "VALUES ($1,$2,true) RETURNING id::text", T, who)
            team[tag] = {"user": uid, "telephony": tr, "messaging": mr, "agent": who}
        why = bo.MemberStatusChange(reason="owner decision")
        await bo.suspend_member(team["S"]["user"], why, ctx=octx)
        await admin.execute("UPDATE users SET is_active=true, updated_at=now() WHERE id=$1", team["S"]["user"])
        await admin.execute("UPDATE team_memberships SET status='active' WHERE user_id=$1", team["S"]["user"])
        kept = await pl.request_closure(octx, confirm_name=name, reason="closing (team changes)")
        s_events = await admin.fetch(
            "SELECT a.action, a.created_at < o.requested_at AS before FROM audit_ledger a, privacy_operations o "
            "WHERE a.target_id = $1 AND o.id = $2", team["S"]["user"], kept["operation_id"])
        assert [(e["action"], e["before"]) for e in s_events] == [("team.member.suspended", True)], s_events

        async def audit_fails(*_a, **_k):
            raise RuntimeError("audit store unavailable")

        for tag in ("K", "N", "P"):
            await bo._reinstate(octx, team[tag]["user"], why)
        with monkeypatch.context() as m:
            m.setattr(audit_ledger.ledger, "record", audit_fails)
            await bo.suspend_member(team["K"]["user"], why, ctx=octx)
        await bo.suspend_member(team["N"]["user"], why, ctx=octx)
        await bo.suspend_member(team["P"]["user"], why, ctx=octx)
        with monkeypatch.context() as m:
            m.setattr(audit_ledger.ledger, "record", audit_fails)
            await bo._reinstate(octx, team["P"]["user"], why)

        async def state_of(tag):
            t = team[tag]
            return (await admin.fetchval("SELECT is_active FROM users WHERE id=$1", t["user"]),
                    await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1::uuid", t["telephony"]),
                    await admin.fetchval("SELECT active FROM messaging_routes WHERE id=$1::uuid", t["messaging"]))

        assert await state_of("K") == (False, False, False) == await state_of("N")
        assert await state_of("P") == (True, False, False)
        assert await state_of("S") == (False, False, False)              # frozen by this closure
        back = await pl.withdraw_closure(octx, reason="keep the brokerage")
        assert await state_of("K") == (False, False, False)
        assert await state_of("N") == (False, False, False)
        assert await state_of("P") == (True, True, True)
        assert await state_of("S") == (True, True, True)                 # its old suspension is not "since"
        assert await switched_on(live) == (True, True, True)
        assert await switched_on(idle) == (False, False, False)
        assert sorted((u["id"], u["reason"]) for u in back["skipped_users"]) == sorted(
            (str(team[t]["user"]), "suspended") for t in ("K", "N"))
        # named for the owner as Team names them (N has no full name: email),
        # but the operation's record keeps ids and reasons only
        assert sorted(u["name"] for u in back["skipped_users"]) == sorted(["Kim Keller", team["N"]["agent"]])
        assert sorted(back["skipped_routes"]) == sorted(
            team[t][r] for t in ("K", "N") for r in ("telephony", "messaging"))
        recorded = await op_row(kept["operation_id"])
        assert recorded["state"] == "cancelled"
        assert sorted(recorded["result"]["skipped_routes"]) == sorted(back["skipped_routes"])
        assert sorted(recorded["result"]["skipped_users"], key=lambda u: u["id"]) == sorted(
            ({"id": str(team[t]["user"]), "reason": "suspended"} for t in ("K", "N")), key=lambda u: u["id"])

        # ── L17-58: once an erasure has started there is nothing to withdraw ─
        await admin.execute("UPDATE legal_holds SET released_at=now(), released_by='counsel' WHERE id=$1", hold)
        last = await pl.request_closure(octx, confirm_name=name, reason="closing, erase")
        erasure = await pl.begin_erasure(T, requested_by="test")
        assert erasure and (await op_row(last["operation_id"]))["state"] == "succeeded"
        with pytest.raises(pl.LifecycleError, match="grace period"):
            await pl.withdraw_closure(octx, reason="too late")
        assert await admin.fetchval("SELECT lifecycle_state FROM tenants WHERE id=$1", T) == "erasing"
        assert await switched_on(live) == (False, False, False)
        assert (await op_row(last["operation_id"]))["state"] == "succeeded"
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in await admin.fetch(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                    "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
