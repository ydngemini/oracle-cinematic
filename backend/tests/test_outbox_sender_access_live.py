"""A queued CRM email leaves only on the sender's CURRENT authority (external
audit 26.2), against real PostgreSQL with FORCE RLS as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

email_outbox.deliver() rechecked billing, suppression and the footer, then
sent as the stored created_by without asking whether that person could still
contact the client. This drives commands_api.requester_may_contact_record (the
check deliver() now makes) through each way access is lost after queueing:
reassignment, an edit share revoked, a view-only share, and the sender's
deactivation.

L09-12 (owner instruction 2026-11-20): the command worker's check used to
read a non-member requester as "an agent with that id", so a deactivated
person's queued sends still reached their own former clients. Now every
requester must be an active member, except a system pipeline staging on its
own authority under the reserved "system:" marker (speed-to-lead for a pool
contact), judged by its own rule (pool records only). The second test drives
_requester_may_contact and the worker with real staged commands; nothing is
sent (the gate and every sender are patched to fail the test if reached).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_outbox_sender_must_still_reach_the_client():
    assert asyncio.run(_scenario()) == {
        "own client": True,
        "colleague's private client": False,
        "after reassignment to a colleague": False,
        "edit share": True,
        "edit share revoked": False,
        "view-only share": False,
        "deactivated, own client (members only)": False,
        # L09-12: was True — the worker read them as "an agent with that id".
        "deactivated, own client (command-worker semantics)": False,
        "unknown sender (members only)": False,
        "broker owner": True,
        "sender stored in another case": True,
        "sender stored as users.id": True,
        "queue-time: member": True,
        "queue-time: member, another case": True,
        "queue-time: no users row (operator/demo)": False,
    }


async def _scenario() -> dict[str, bool]:
    import asyncpg

    import crm
    from commands_api import requester_may_contact_record
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c")}
    agent = {k: f"{k}-{tag}@outbox.test" for k in users}
    client_a, client_b = uuid.uuid4(), uuid.uuid4()
    out: dict[str, bool] = {}

    async def may(who: str, client, **kw) -> bool:
        return await requester_may_contact_record(tenant, agent.get(who, who), str(client), None, **kw)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Outbox access')",
                            tenant, f"outbox-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, tenant, agent[who], "broker_owner" if who == "owner" else "agent", f"Agent {who}")
        for cid, who in ((client_a, "a"), (client_b, "b")):
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, email) VALUES ($1,$2,$3,$4,$5)",
                cid, tenant, f"Client {who}", agent[who], f"{who}-{tag}@client.test")

        out["own client"] = await may("a", client_a)
        # find_member matches case-insensitively and by users.id (review of #87).
        out["sender stored in another case"] = await requester_may_contact_record(
            tenant, agent["a"].upper(), str(client_a), None)
        out["sender stored as users.id"] = await requester_may_contact_record(
            tenant, str(users["a"]), str(client_a), None)

        def as_ctx(agent_id: str) -> TenantContext:
            return TenantContext(agent_id=agent_id, tenant_id=tenant, role=Role.AGENT)

        out["queue-time: member"] = await crm.sender_is_active_member(as_ctx(agent["b"]))
        out["queue-time: member, another case"] = await crm.sender_is_active_member(as_ctx(agent["b"].upper()))
        out["queue-time: no users row (operator/demo)"] = await crm.sender_is_active_member(
            as_ctx(f"operator-{tag}@outbox.test"))
        out["colleague's private client"] = await may("a", client_b)

        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client_a, agent["b"])
        out["after reassignment to a colleague"] = await may("a", client_a)
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client_a, agent["a"])

        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'edit',$4)", tenant, client_b, users["c"], agent["b"])
        out["edit share"] = await may("c", client_b)
        await admin.execute("DELETE FROM record_shares WHERE resource_id=$1 AND grantee_user_id=$2",
                            client_b, users["c"])
        out["edit share revoked"] = await may("c", client_b)
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'view',$4)", tenant, client_b, users["c"], agent["b"])
        out["view-only share"] = await may("c", client_b)

        await admin.execute("UPDATE users SET is_active=false WHERE id=$1", users["a"])
        out["deactivated, own client (members only)"] = await may("a", client_a)
        out["deactivated, own client (command-worker semantics)"] = await may("a", client_a, allow_pipeline=True)
        out["unknown sender (members only)"] = await may(f"ghost-{tag}@outbox.test", client_a)
        out["broker owner"] = await may("owner", client_b)
        return out
    finally:
        await admin.execute("DELETE FROM record_shares WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM clients WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM users WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()


def test_the_command_worker_sends_only_for_members_and_marked_pipelines(monkeypatch):
    assert asyncio.run(_worker_scenario(monkeypatch)) == {
        "member, own client": (True, []),
        "member, email about no record": (True, []),
        "pipeline (system:speed-to-lead), pool client": (True, []),
        "legacy speed-to-lead-worker row, every origin signal": (True, []),
        "legacy name, a person's idempotency key": (False, ["not_a_member"]),
        "unknown system: name": (False, ["not_a_member"]),
        "deactivated, own former client": (False, ["not_a_member"]),
        "deactivated, email about no record": (False, ["not_a_member"]),
        "pipeline, the pool client was assigned since": (False, ["no_record_access"]),
        "pipeline name carried by an account": (False, ["not_a_member"]),
        "worker: deactivated requester": "refused: not an active member (terminal)",
        "worker: pipeline requester": "passed the requester gate",
        "register system:… address": 409,
        "invite system:… address": "refused",
    }


async def _worker_scenario(monkeypatch) -> dict:
    import secrets

    import asyncpg
    from fastapi import HTTPException, Response
    from pydantic import ValidationError

    import auth
    import brokerage_onboarding
    import commands_api
    from commands_api import CommandCreate, CommandType
    from db import connection
    from tenancy import Role, TenantContext

    async def _never(*_a, **_k):
        raise AssertionError("an execution reached the outreach gate or a sender")

    monkeypatch.setattr(commands_api, "guard_outreach", _never)
    monkeypatch.setattr(commands_api, "_resolve_email_provider", _never)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a")}
    agent = {k: f"{k}-{tag}@worker.test" for k in users}
    client_a, client_pool = uuid.uuid4(), uuid.uuid4()
    out: dict = {}
    stray: list[str] = []

    def ctx(agent_id: str, role: Role = Role.AGENT) -> TenantContext:
        return TenantContext(agent_id=agent_id, tenant_id=tenant, role=role)

    def email(client, key: str, *, source: str = "", to: str = "") -> CommandCreate:
        target = {"email": to or f"{key}-{tag}@client.test"}
        if client is not None:
            target["client_id"] = str(client)
        return CommandCreate(command_type=CommandType.EMAIL, target=target,
                             draft={"subject": "Hello", "body": "Following up."},
                             idempotency_key=key, context={"source": source} if source else {})

    async def stage(as_agent: str, body: CommandCreate) -> str:
        return (await commands_api.create_command(body, ctx=ctx(as_agent)))["command"]["id"]

    async def check(cid: str):
        row = await admin.fetchrow("SELECT * FROM command_executions WHERE id=$1", uuid.UUID(cid))
        reasons: list[str] = []
        return await commands_api._requester_may_contact(tenant, row, reasons=reasons), reasons

    async def run_worker(cid: str):
        await admin.execute("UPDATE command_executions SET state='queued' WHERE id=$1", uuid.UUID(cid))
        row = await admin.fetchrow("SELECT * FROM command_executions WHERE id=$1", uuid.UUID(cid))

        class _Reporter:
            job = {"tenant_id": tenant, "created_by": agent["owner"]}

            async def progress(self, *_a, **_k):
                return None

        payload = {"command_id": cid, "command_type": "EMAIL", "target": commands_api._decode(row["target"]),
                   "draft": {"subject": "Hello", "body": "Following up."}, "tenant_id": tenant}
        return await commands_api._execute_command_job(payload, _Reporter())

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name, postal_address) "
                            "VALUES ($1,$2,'Worker access','1 Main St, Dover, DE 19901')", tenant, f"worker-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, tenant, agent[who], "broker_owner" if who == "owner" else "agent", f"Agent {who}")
        for cid, assignee, key in ((client_a, agent["a"], "a"), (client_pool, None, "pool")):
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, email) VALUES ($1,$2,$3,$4,$5)",
                cid, tenant, f"Client {key}", assignee, f"{key}-{tag}@client.test")

        # Staged through the real API, as each requester stages today.
        own = await stage(agent["a"], email(client_a, f"own-{tag}", to=f"a-{tag}@client.test"))
        loose = await stage(agent["a"], email(None, f"loose-{tag}"))
        piped = await stage("system:speed-to-lead", email(
            client_pool, f"speed-to-lead:{tag}", source="speed-to-lead", to=f"pool-{tag}@client.test"))
        # what speed_to_lead.py staged an unassigned contact as before L09-12
        legacy = await stage("speed-to-lead-worker", email(
            client_pool, f"speed-to-lead:{tag}-legacy", source="speed-to-lead", to=f"pool-{tag}@client.test"))
        lookalike = await stage("speed-to-lead-worker", email(
            client_pool, f"parse:{tag}", source="speed-to-lead", to=f"pool-{tag}@client.test"))
        stranger = await stage("system:other", email(client_pool, f"other-{tag}", to=f"pool-{tag}@client.test"))

        out["member, own client"] = await check(own)
        out["member, email about no record"] = await check(loose)
        out["pipeline (system:speed-to-lead), pool client"] = await check(piped)
        out["legacy speed-to-lead-worker row, every origin signal"] = await check(legacy)
        out["legacy name, a person's idempotency key"] = await check(lookalike)
        out["unknown system: name"] = await check(stranger)

        # Agent A is suspended (brokerage_onboarding.suspend_member's write).
        await admin.execute("UPDATE users SET is_active=false, session_epoch=session_epoch+1 WHERE id=$1",
                            users["a"])
        out["deactivated, own former client"] = await check(own)
        out["deactivated, email about no record"] = await check(loose)

        try:
            await run_worker(own)
            out["worker: deactivated requester"] = "sent?"
        except commands_api.RequesterMayNotContact as exc:
            after = await admin.fetchrow("SELECT state, last_error, provider_reference FROM command_executions "
                                         "WHERE id=$1", uuid.UUID(own))
            ok = (after["state"] == "failed" and "not an active member of this brokerage" in after["last_error"]
                  and after["provider_reference"] is None and exc.job_terminal)
            out["worker: deactivated requester"] = ("refused: not an active member (terminal)" if ok
                                                    else f"refused, but recorded {dict(after)}")

        class _PastTheGate(RuntimeError):
            pass

        async def _past(*_a, **_k):
            raise _PastTheGate("the requester gate let it through")

        monkeypatch.setattr(commands_api, "_destination_still_on_record", _past)
        try:
            await run_worker(piped)
            out["worker: pipeline requester"] = "returned?"
        except _PastTheGate:
            out["worker: pipeline requester"] = "passed the requester gate"
        except commands_api.RequesterMayNotContact as exc:
            out["worker: pipeline requester"] = f"refused: {exc}"

        # The pool client is given to agent A's colleague (the owner) since.
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client_pool, agent["owner"])
        out["pipeline, the pool client was assigned since"] = await check(legacy)
        await admin.execute("UPDATE clients SET assignee_id=NULL WHERE id=$1", client_pool)
        # An account (even an inactive one) carrying the pipeline's name: not the pipeline.
        await admin.execute(
            "INSERT INTO users (tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
            "VALUES ($1,'system:speed-to-lead','agent',$2,'scrypt$x$y','Lookalike',false)",
            tenant, f"lookalike-{tag}@worker.test")
        out["pipeline name carried by an account"] = await check(piped)

        try:
            made = await auth.register(auth.RegisterRequest(
                email=f"system:speed-to-lead-{tag}@worker.test", password=secrets.token_urlsafe(24),
                full_name="Not a pipeline", company="Nope"), Response())
            out["register system:… address"] = 201
            stray.append(str(made.tenant_id))      # a regression made a brokerage: removed below
        except HTTPException as exc:
            out["register system:… address"] = exc.status_code
        try:
            brokerage_onboarding.InviteCreate(emails=[f"System:Speed-To-Lead-{tag}@worker.test"])
            out["invite system:… address"] = "accepted"
        except ValidationError:
            out["invite system:… address"] = "refused"
        return out
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for t in (tenant, *stray):
                for row in tables:
                    cast = "uuid" if row["data_type"] == "uuid" else "text"
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", t)
                await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(t))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
