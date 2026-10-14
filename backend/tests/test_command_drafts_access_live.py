"""Staged drafts and their approvals follow their client or contact (0142).
Against real PostgreSQL with FORCE RLS, as the application role (skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03 for command_executions + action_approvals (L03-06), owner decision
D-2026-10-13-01; the sibling of test_communications_access_matrix_live.py
(0140). Actors: agent A, agent B, agent C (B's client and contact shared with
C to VIEW), the broker owner, a platform admin, an agent of another brokerage
and a brokerage-scope pipeline.

Nothing here is sent: no draft is approved into the queue. The one execution
path exercised (_execute_command_job for a requester who lost access) must stop
before the compliance gate, and the gate and every sender are patched to fail
the test if it is reached.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

TABLES = ("command_executions", "action_approvals")


def test_drafts_follow_the_record(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


def test_drafts_policy_composition_cannot_widen():
    asyncio.run(_policy_composition())


async def _policy_composition():
    """One permissive tenant wall + exactly the four restrictive 0142 policies
    per table (a new permissive policy would OR around them, F03.01), and the
    link trigger on both."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", list(TABLES))
        triggers = await admin.fetch(
            "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND t.tgfoid = 'app_link_draft_target'::regproc")
    finally:
        await admin.close()
    for t in TABLES:
        mine = [r for r in rows if r["tablename"] == t]
        permissive = [r for r in mine if r["permissive"] == "PERMISSIVE"]
        assert len(permissive) == 1 and permissive[0]["cmd"] == "ALL", (t, permissive)
        assert {(r["policyname"], r["cmd"]) for r in mine if r["permissive"] == "RESTRICTIVE"} == {
            (f"{t}_linked_read", "SELECT"), (f"{t}_linked_insert", "INSERT"),
            (f"{t}_linked_update", "UPDATE"), (f"{t}_linked_delete", "DELETE")}, t
    assert {(r["relname"], r["tgname"]) for r in triggers} == {
        ("command_executions", "trg_command_executions_link"), ("action_approvals", "trg_action_approvals_link")}


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import approval_service
    import commands_api
    import record_access
    from commands_api import ApprovalDecision, CommandCreate, CommandEdit, CommandType
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    async def _never(*_a, **_k):
        raise AssertionError("an execution reached the outreach gate or a sender")

    monkeypatch.setattr(commands_api, "guard_outreach", _never)
    monkeypatch.setattr(commands_api, "_resolve_email_provider", _never)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@drafts.test" for k in users}
    people = {k: {"client": uuid.uuid4(), "contact": uuid.uuid4()} for k in ("a", "b", "pool", "gone")}
    assignee = {"a": agent["a"], "b": agent["b"], "pool": None, "gone": agent["a"]}
    foreign_client = uuid.uuid4()  # a client of the OTHER brokerage

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="drafts-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"pipeline:{tag}", tenant_id=tenant, role=Role.AGENT))
    actors = {"a": ctx("a"), "b": ctx("b"), "c": ctx("c"), "owner": ctx("owner"), "d": ctx("d"),
              "admin": admin_ctx, "pipeline": pipeline}

    def email(who_for: str, key: str) -> CommandCreate:
        target = {"email": f"{who_for}-{tag}@client.test"}
        if who_for in people:
            target["client_id"] = str(people[who_for]["client"])
        return CommandCreate(command_type=CommandType.EMAIL, target=target,
                             draft={"subject": "Hello", "body": "Following up."},
                             idempotency_key=f"drafts-{tag}-{key}")

    def sms(who_for: str, key: str) -> CommandCreate:
        return CommandCreate(command_type=CommandType.SMS, draft={"body": "Hi there"},
                             target={"phone": "+13025550142", "state_code": "DE",
                                     "client_id": str(people[who_for]["client"]),
                                     "contact_id": str(people[who_for]["contact"])},
                             idempotency_key=f"drafts-{tag}-{key}")

    async def status_of(coro) -> int:
        try:
            await coro
        except HTTPException as exc:
            return exc.status_code
        return 200

    try:
        for t, slug in ((tenant, "drafts"), (other_tenant, "drafts-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Drafts matrix')",
                                t, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other_tenant if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for k, p in people.items():
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'matrix','pending_encryption')", p["contact"], tenant, assignee[k])
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, contact_id) VALUES ($1,$2,$3,$4,$5)",
                p["client"], tenant, f"Drafts {k}", assignee[k], p["contact"])
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'Elsewhere')",
                            foreign_client, other_tenant)
        for kind in ("client", "contact"):
            await admin.execute(
                "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
                "granted_by) VALUES ($1,$2,$3,$4,'view',$5)", tenant, kind, people["b"][kind], users["c"], agent["b"])

        # ── staging through the real API, each agent on a record they own ──
        drafts: dict[str, str] = {}
        drafts["a"] = (await commands_api.create_command(email("a", "a"), ctx=ctx("a")))["command"]["id"]
        drafts["b"] = (await commands_api.create_command(sms("b", "b"), ctx=ctx("b")))["command"]["id"]
        drafts["pool"] = (await commands_api.create_command(email("pool", "pool"), ctx=ctx("a")))["command"]["id"]
        drafts["gone"] = (await commands_api.create_command(email("gone", "gone"), ctx=ctx("a")))["command"]["id"]
        drafts["none"] = (await commands_api.create_command(email("nobody", "none"), ctx=ctx("owner")))["command"]["id"]

        # The trigger links both tables from the target, and nobody else does.
        for k, cid in drafts.items():
            row = await admin.fetchrow(
                "SELECT ce.client_id, ce.contact_id, aa.client_id AS a_client, aa.contact_id AS a_contact "
                "FROM command_executions ce JOIN action_approvals aa ON aa.id = ce.approval_id WHERE ce.id=$1",
                uuid.UUID(cid))
            want_client = people[k]["client"] if k in people else None
            want_contact = people[k]["contact"] if k == "b" else None
            assert (row["client_id"], row["contact_id"]) == (want_client, want_contact), k
            assert (row["a_client"], row["a_contact"]) == (want_client, want_contact), k

        # The previous release inserts without the columns: still linked, and
        # the caller's record scope is back to 'actor' after the trigger ran.
        old_release = uuid.uuid4()
        async with connection.tenant_tx(ctx("a")) as conn:
            await conn.execute(
                "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, "
                "draft, idempotency_key, created_by) VALUES ($1,$2,'EMAIL','explicit_email','outreach',$3::jsonb,"
                "'{}'::jsonb,$4,$5)", old_release, tenant,
                f'{{"client_id": "{str(people["a"]["client"]).upper()}", "email": "x@client.test"}}',
                f"drafts-{tag}-old", agent["a"])
            assert await conn.fetchval("SELECT current_setting('app.record_scope', true)") == "actor"
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1",
                                    old_release) == people["a"]["client"]
        # ...a writer cannot pass a link that disagrees with the target...
        async with connection.tenant_tx(admin_ctx) as conn:
            await conn.execute("UPDATE command_executions SET client_id = NULL WHERE id=$1", old_release)
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1",
                                    old_release) == people["a"]["client"]
        # ...and an id from another brokerage links nothing.
        foreign = uuid.uuid4()
        async with connection.tenant_tx(admin_ctx) as conn:
            await conn.execute(
                "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, "
                "draft, idempotency_key, created_by) VALUES ($1,$2,'EMAIL','explicit_email','outreach',$3::jsonb,"
                "'{}'::jsonb,$4,'drafts-admin')", foreign, tenant,
                f'{{"client_id": "{foreign_client}", "email": "x@client.test"}}', f"drafts-{tag}-foreign")
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1", foreign) is None
        await admin.execute("DELETE FROM command_executions WHERE id = ANY($1::uuid[])", [old_release, foreign])

        # ── read: who sees which draft ─────────────────────────────────────
        mine = set(drafts.values())
        expected = {
            "a": {"a", "pool", "gone", "none"}, "b": {"b", "pool", "none"}, "c": {"b", "pool", "none"},
            "owner": set(drafts), "admin": set(drafts), "pipeline": set(drafts), "d": set(),
        }
        approval_of = {k: await admin.fetchval("SELECT approval_id FROM command_executions WHERE id=$1",
                                               uuid.UUID(cid)) for k, cid in drafts.items()}
        for who, c in actors.items():
            listed = {x["id"] for x in (await commands_api.commands(state_filter=None, limit=200, ctx=c))["commands"]}
            assert listed & mine == {drafts[k] for k in expected[who]}, who
            async with connection.tenant_tx(c) as conn:
                # Approval rows by id follow the same record as their draft.
                seen = {r["id"] for r in await conn.fetch(
                    "SELECT id FROM action_approvals WHERE id = ANY($1::uuid[])", list(approval_of.values()))}
                assert seen == {approval_of[k] for k in expected[who]}, who
                # The read-back the API uses agrees with the policy, view and edit.
                for k, cid in drafts.items():
                    level = await record_access.linked_access_level(conn, "command_executions", cid)
                    if who == "d":
                        assert level is None
                        continue
                    want = None if k not in expected[who] else ("view" if (who, k) == ("c", "b") else "edit")
                    assert level == want, (who, k, level)
        # An agent's approval list is their own requests, and only while they
        # can see the record; the owner's is everyone's.
        assert {a["target_id"] for a in await approval_service.list_approvals(ctx("a"))} == {
            drafts["a"], drafts["pool"], drafts["gone"]}
        assert {a["target_id"] for a in await approval_service.list_approvals(ctx("c"))} == set()
        assert {a["target_id"] for a in await approval_service.list_approvals(ctx("owner"))} >= mine

        # ── the API: 404 invisible, 403 view-only or not yours to decide ───
        reason = ApprovalDecision(reason="checked by the drafts matrix")
        edit_b = CommandEdit(target={"phone": "+13025550142", "state_code": "DE",
                                     "client_id": str(people["b"]["client"])}, draft={"body": "Changed"})
        assert await status_of(commands_api.command_detail(drafts["b"], ctx=ctx("a"))) == 404
        assert await status_of(commands_api.command_detail(drafts["b"], ctx=ctx("d"))) == 404
        assert await status_of(commands_api.command_detail(drafts["b"], ctx=ctx("c"))) == 200
        for who, code in (("a", 404), ("c", 403)):
            assert await status_of(commands_api.edit_command(drafts["b"], edit_b, ctx=ctx(who))) == code, who
            assert await status_of(commands_api.approve_command(drafts["b"], reason, ctx=ctx(who))) == code, who
            assert await status_of(commands_api.reject_command(drafts["b"], reason, ctx=ctx(who))) == code, who
        # A colleague's draft about a POOL client: readable, not B's to decide.
        assert await status_of(commands_api.command_detail(drafts["pool"], ctx=ctx("b"))) == 200
        edit_pool = CommandEdit(target={"email": "pool@client.test", "client_id": str(people["pool"]["client"])},
                                draft={"subject": "Mine now", "body": "Edited by B."})
        assert await status_of(commands_api.edit_command(drafts["pool"], edit_pool, ctx=ctx("b"))) == 403
        assert await status_of(commands_api.approve_command(drafts["pool"], reason, ctx=ctx("b"))) == 403
        assert await status_of(commands_api.reject_command(drafts["pool"], reason, ctx=ctx("b"))) == 403
        assert await admin.fetchval("SELECT state FROM command_executions WHERE id=$1",
                                    uuid.UUID(drafts["pool"])) == "awaiting_approval"
        # Staging needs the record editable: invisible 404, view share 403.
        assert await status_of(commands_api.create_command(sms("b", "sneak-a"), ctx=ctx("a"))) == 404
        assert await status_of(commands_api.create_command(sms("b", "sneak-c"), ctx=ctx("c"))) == 403
        # Retargeting your own draft at a colleague's client: 404.
        retarget = CommandEdit(target={"email": "b@client.test", "client_id": str(people["b"]["client"])},
                               draft={"subject": "Hello", "body": "Following up."})
        assert await status_of(commands_api.edit_command(drafts["a"], retarget, ctx=ctx("a"))) == 404
        # The database refuses the same rows without the API.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("c")) as conn:
                await conn.execute(
                    "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
                    "draft, idempotency_key, created_by) VALUES ($1,'EMAIL','explicit_email','outreach',$2::jsonb,"
                    "'{}'::jsonb,$3,$4)", tenant, f'{{"client_id": "{people["b"]["client"]}"}}',
                    f"drafts-{tag}-db", agent["c"])
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await conn.execute("UPDATE command_executions SET state='cancelled' WHERE id=$1",
                                      uuid.UUID(drafts["b"])) == "UPDATE 0"
        # The requester may reject their own draft (nothing is sent).
        rejected = await commands_api.reject_command(drafts["b"], reason, ctx=ctx("b"))
        assert rejected["command"]["state"] == "cancelled"

        # ── execution re-checks the requester (C3.4) ───────────────────────
        def row_for(cid):
            return admin.fetchrow("SELECT * FROM command_executions WHERE id=$1", uuid.UUID(cid))

        assert await commands_api._requester_may_contact(tenant, await row_for(drafts["pool"])) is True
        assert await commands_api._requester_may_contact(tenant, await row_for(drafts["none"])) is True
        # The owner gives the pool client to B after A's draft was staged.
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", people["pool"]["client"], agent["b"])
        assert await commands_api._requester_may_contact(tenant, await row_for(drafts["pool"])) is False
        # A system requester that is not a member is checked as an agent: pool only.
        await admin.execute(
            "UPDATE action_approvals SET requested_by='speed-to-lead' WHERE id=(SELECT approval_id FROM "
            "command_executions WHERE id=$1)", uuid.UUID(drafts["gone"]))
        assert await commands_api._requester_may_contact(tenant, await row_for(drafts["gone"])) is False
        await admin.execute("UPDATE clients SET assignee_id=NULL WHERE id=$1", people["gone"]["client"])
        assert await commands_api._requester_may_contact(tenant, await row_for(drafts["gone"])) is True
        # The worker stops before the gate and records why.
        await admin.execute("UPDATE command_executions SET state='queued' WHERE id=$1", uuid.UUID(drafts["pool"]))
        stored = await row_for(drafts["pool"])

        class _Reporter:
            job = {"tenant_id": tenant, "created_by": agent["a"]}

            async def progress(self, *_a, **_k):
                return None

        payload = {"command_id": drafts["pool"], "command_type": "EMAIL",
                   "target": commands_api._decode(stored["target"]),
                   "draft": {"subject": "Hello", "body": "Following up."}, "tenant_id": tenant}
        with pytest.raises(RuntimeError, match="Not sent"):
            await commands_api._execute_command_job(payload, _Reporter())
        after = await row_for(drafts["pool"])
        assert after["state"] == "failed" and after["last_error"].startswith("Not sent"), dict(after)
        assert after["provider_reference"] is None

        # ── a deleted client unlinks its drafts (FK SET NULL vs the trigger) ─
        await admin.execute("DELETE FROM clients WHERE id=$1", people["gone"]["client"])
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1",
                                    uuid.UUID(drafts["gone"])) is None
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                for t in (tenant, other_tenant):
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", t)
            for t in (tenant, other_tenant):
                await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(t))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
