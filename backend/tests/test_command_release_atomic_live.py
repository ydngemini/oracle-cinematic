"""Releasing an approved command is one transaction (external audit 24.1),
against real PostgreSQL with FORCE RLS.

release_command committed the approval, then the job, then the command's move
to 'queued' — three transactions. A failure after the first left an approved
action with no job that nobody could approve again; a worker claiming the job
before the third commit found the command still 'awaiting_approval' and
refused it. Reject had the same split. Now each commits together or not at
all, and the reconciliation sweep repairs commands stranded before the fix.

Nothing is sent: no worker runs, and the jobs created are only inspected.
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


def test_release_and_reject_are_atomic_and_stranded_commands_are_repaired(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg
    from fastapi import HTTPException

    import commands_api
    import outreach_lifecycle
    from commands_api import CommandCreate, CommandType
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    client = str(uuid.uuid4())
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

    async def stage(key: str) -> dict:
        body = CommandCreate(command_type=CommandType.EMAIL,
                             target={"email": f"x-{s}@client.test", "client_id": client},
                             draft={"subject": "Hello", "body": "Following up."},
                             idempotency_key=f"atomic-{s}-{key}")
        created = await commands_api.create_command(body, ctx=octx)
        return await commands_api._get_command(octx, created["command"]["id"])

    async def state(cid) -> tuple:
        return tuple(await admin.fetchrow(
            "SELECT ce.state, aa.status, (SELECT count(*) FROM automation_jobs j WHERE j.tenant_id = ce.tenant_id "
            "AND j.job_type = 'command:execute' AND j.payload->>'command_id' = ce.id::text) AS jobs "
            "FROM command_executions ce JOIN action_approvals aa ON aa.id = ce.approval_id WHERE ce.id=$1",
            uuid.UUID(str(cid))))

    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name, postal_address) "
                                "VALUES ($1,$2,$3,'1 Main St, Dover, DE 19901')", T, f"atomic-{s}", f"B {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name, email) VALUES ($1,$2,$3,$4)",
                                client, T, f"X {s}", f"x-{s}@client.test")

        # 1. A failure after the approval decision rolls the approval back too.
        row = await stage("fail")
        real_advance = outreach_lifecycle.advance_first_response

        async def boom(*_a, **_k):
            raise RuntimeError("crash after the job insert")

        monkeypatch.setattr(outreach_lifecycle, "advance_first_response", boom)
        with pytest.raises(RuntimeError):
            await commands_api.release_command(octx, row, reason="approved in test")
        assert await state(row["id"]) == ("awaiting_approval", "pending", 0)

        # 2. A worker cannot see the job before the command is 'queued': inside
        #    the release, after the job insert, another connection sees nothing.
        seen = {}

        async def peek(conn, **kwargs):
            seen["jobs_visible"] = await admin.fetchval(
                "SELECT count(*) FROM automation_jobs WHERE tenant_id=$1 AND job_type='command:execute' "
                "AND payload->>'command_id' = $2", uuid.UUID(T), str(row["id"]))
            return await real_advance(conn, **kwargs)

        monkeypatch.setattr(outreach_lifecycle, "advance_first_response", peek)
        released = await commands_api.release_command(octx, row, reason="approved in test")
        assert seen["jobs_visible"] == 0
        assert released["command"]["state"] == "queued"
        assert await state(row["id"]) == ("queued", "approved", 1)
        monkeypatch.setattr(outreach_lifecycle, "advance_first_response", real_advance)
        with pytest.raises(HTTPException) as again:          # a second click is a clean 409
            await commands_api.release_command(octx, row, reason="approved in test")
        assert again.value.status_code == 409

        # 3. Reject is atomic too.
        rej = await stage("reject")
        monkeypatch.setattr(outreach_lifecycle, "advance_first_response", boom)
        with pytest.raises(RuntimeError):
            await commands_api.withdraw_command(octx, rej, reason="rejected in test")
        assert await state(rej["id"]) == ("awaiting_approval", "pending", 0)
        monkeypatch.setattr(outreach_lifecycle, "advance_first_response", real_advance)

        # 4. An expired approval: a 409, the 'expired' decision is kept, nothing
        #    queued, and the command stays staged (editable), as it was before.
        late = await stage("late")
        await admin.execute("UPDATE action_approvals SET expires_at = now() - interval '1 minute' "
                            "WHERE id = (SELECT approval_id FROM command_executions WHERE id=$1)", late["id"])
        with pytest.raises(HTTPException) as gone:
            await commands_api.release_command(octx, late, reason="approved in test")
        assert gone.value.status_code == 409
        assert await state(late["id"]) == ("awaiting_approval", "expired", 0)

        # 5. Commands stranded by the old three-transaction release are repaired.
        stranded = {k: await stage(k) for k in ("approved", "approved_expired", "rejected", "dead_job")}
        for k, status, expiry in (("approved", "approved", 1440),
                                  ("approved_expired", "approved", -1),
                                  ("rejected", "rejected", 1440),
                                  ("dead_job", "approved", 1440)):
            await admin.execute(
                "UPDATE action_approvals SET status=$2, decided_by=$3, decided_at=now() - interval '10 minutes', "
                "expires_at = now() + make_interval(mins => $4) WHERE id = (SELECT approval_id FROM command_executions WHERE id=$1)",
                stranded[k]["id"], status, owner, expiry)
        await admin.execute("UPDATE action_approvals SET decided_at = now() - interval '10 minutes' "
                            "WHERE id = (SELECT approval_id FROM command_executions WHERE id=$1)", late["id"])
        # The old release's job committed and the worker dead-lettered it
        # ("not executable from awaiting_approval"): the sweep must not attach it.
        dead_cmd = stranded["dead_job"]
        dead_job = await admin.fetchval(
            "INSERT INTO automation_jobs (tenant_id, job_type, queue_name, state, risk_class, payload, priority, "
            "max_attempts, attempt_count, idempotency_key, approval_id, created_by, completed_at) "
            "VALUES ($1,'command:execute','default','dead_letter','outreach',$2::jsonb,10,5,5,$3,$4,$5,now()) "
            "RETURNING id", uuid.UUID(T), json.dumps({"command_id": str(dead_cmd["id"])}),
            f"command:{dead_cmd['id']}:execute", dead_cmd["approval_id"], owner)
        counts = await commands_api.reconcile_stranded_releases(T)
        assert counts == {"queued": 2, "cancelled": 2, "held_not_entitled": 0}
        assert await state(stranded["approved"]["id"]) == ("queued", "approved", 1)
        assert await state(stranded["approved_expired"]["id"]) == ("cancelled", "approved", 0)
        assert await state(stranded["rejected"]["id"]) == ("cancelled", "rejected", 0)
        assert await state(late["id"]) == ("awaiting_approval", "expired", 0)     # still editable
        new_job = await admin.fetchrow(
            "SELECT j.id, j.state FROM command_executions ce JOIN automation_jobs j ON j.id = ce.job_id "
            "WHERE ce.id=$1", dead_cmd["id"])
        assert new_job["id"] != dead_job and new_job["state"] == "queued"
        assert await commands_api.reconcile_stranded_releases(T) == {"queued": 0, "cancelled": 0,
                                                                     "held_not_entitled": 0}

        # 6. A job built for an approval an edit replaced never runs, and never retries.
        class Reporter:
            job = {"tenant_id": T, "approval_id": str(uuid.uuid4()), "created_by": owner}

            async def progress(self, *_a, **_k):
                raise AssertionError("must stop before any work")

        with pytest.raises(RuntimeError) as superseded:
            await commands_api._execute_command_job(
                {"command_type": "EMAIL", "command_id": str(stranded["approved"]["id"]), "target": {}, "draft": {}},
                Reporter())
        assert getattr(superseded.value, "job_terminal", False) is True
        assert await state(stranded["approved"]["id"]) == ("queued", "approved", 1)
        assert getattr(superseded.value, "job_error_code", "") == "APPROVAL_SUPERSEDED"

        # 7. Reject still withdraws a command whose approval expired at the click.
        withdrawn = await commands_api.withdraw_command(octx, late, reason="rejected in test")
        assert withdrawn["command"]["state"] == "cancelled"
        assert await state(late["id"]) == ("cancelled", "expired", 0)
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
