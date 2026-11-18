"""An outbound command goes only to the phone or email on the record it is
about (external audit 26.4), against real PostgreSQL as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Before: staging checked the phone's format and the record id's permission, but
never that the phone (or email) was that client's or contact's; the worker
sent to whatever the target held. A text could go to a stranger under a
client's record, agent authority and timeline.

Nothing is sent: the gate and every sender are patched to fail if reached.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_destination_is_the_one_on_the_record(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import commands_api
    import contact_truth
    from commands_api import CommandCreate, CommandEdit, CommandType
    from db import connection
    from tenancy import Role, TenantContext

    if not os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-master-key-" + secrets.token_hex(16))

    async def _never(*_a, **_k):
        raise AssertionError("an execution reached the outreach gate or a sender")

    monkeypatch.setattr(commands_api, "guard_outreach", _never)
    monkeypatch.setattr(commands_api, "_resolve_email_provider", _never)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    owner = f"owner-{tag}@dest.test"
    ctx = TenantContext(agent_id=owner, tenant_id=tenant, role=Role.BROKER_OWNER)
    client, bare, contact = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def cmd(kind: CommandType, key: str, **target) -> CommandCreate:
        draft = {"subject": "Hello", "body": "Following up."} if kind is CommandType.EMAIL else {"body": "Hi"}
        if kind is not CommandType.EMAIL:
            target.setdefault("state_code", "DE")
        return CommandCreate(command_type=kind, target={k: str(v) for k, v in target.items()}, draft=draft,
                             idempotency_key=f"dest-{tag}-{key}")

    async def status_of(coro) -> int:
        try:
            await coro
        except HTTPException as exc:
            return exc.status_code
        return 200

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name, postal_address) VALUES ($1,$2,'Destinations',"
                            "'1 Main St, Dover, DE 19901')", tenant, f"dest-{tag}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash, is_active) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y',true)", tenant, owner)
        # The client's phone is stored as typed; the contact keeps only keyed hashes.
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, phone, email) "
                            "VALUES ($1,$2,'Pat Seller','(302) 555-0100','Pat@Example.TEST')", client, tenant)
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'No Details')",
                            bare, tenant)
        await admin.execute(
            "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state, "
            "phone_lookup_hash, email_lookup_hash) VALUES ($1,$2,$3,'matrix','pending_encryption',$4,$5)",
            contact, tenant, owner, contact_truth.lookup_hash(tenant, "phone", "+13025550199"),
            contact_truth.lookup_hash(tenant, "email", "kim@example.test"))

        # ── staging ────────────────────────────────────────────────────────
        ok = await commands_api.create_command(cmd(CommandType.SMS, "ok", phone="+13025550100", client_id=client),
                                               ctx=ctx)
        assert ok["created"]
        # a number that is not the client's (before 26.4: staged, and sent)
        assert await status_of(commands_api.create_command(
            cmd(CommandType.SMS, "stranger", phone="+13025550111", client_id=client), ctx=ctx)) == 422
        assert await status_of(commands_api.create_command(
            cmd(CommandType.CALL, "stranger-call", phone="+13025550111", client_id=client), ctx=ctx)) == 422
        # a record with no phone on file vouches for none
        assert await status_of(commands_api.create_command(
            cmd(CommandType.SMS, "bare", phone="+13025550100", client_id=bare), ctx=ctx)) == 422
        # a contact: matched by its keyed hash
        assert (await commands_api.create_command(
            cmd(CommandType.SMS, "contact", phone="+13025550199", contact_id=contact), ctx=ctx))["created"]
        assert await status_of(commands_api.create_command(
            cmd(CommandType.SMS, "contact-no", phone="+13025550100", contact_id=contact), ctx=ctx)) == 422
        # both named: either record's own number will do (the contact's here)
        assert (await commands_api.create_command(
            cmd(CommandType.SMS, "both", phone="+13025550199", client_id=client, contact_id=contact),
            ctx=ctx))["created"]
        # email: normalised (case), and an address that is not theirs is refused
        mail = await commands_api.create_command(
            cmd(CommandType.EMAIL, "mail", email="pat@example.test", client_id=client), ctx=ctx)
        assert mail["created"]
        assert await status_of(commands_api.create_command(
            cmd(CommandType.EMAIL, "mail-no", email="someone@else.test", client_id=client), ctx=ctx)) == 422
        assert (await commands_api.create_command(
            cmd(CommandType.EMAIL, "mail-contact", email="KIM@example.test", contact_id=contact), ctx=ctx))["created"]
        # an email about no record is not a CRM identity: left to the gate
        assert (await commands_api.create_command(
            cmd(CommandType.EMAIL, "mail-free", email="anyone@else.test"), ctx=ctx))["created"]
        # editing the draft to another number is refused the same way
        stranger_edit = CommandEdit(target={"phone": "+13025550111", "state_code": "DE", "client_id": str(client)},
                                    draft={"body": "Changed"})
        assert await status_of(commands_api.edit_command(ok["command"]["id"], stranger_edit, ctx=ctx)) == 422

        # ── execution: the record changed after approval ───────────────────
        target = commands_api._decode(ok["command"]["target"])
        assert await commands_api._destination_still_on_record(tenant, CommandType.SMS, target) is None
        await admin.execute("UPDATE clients SET phone='+13025550122' WHERE id=$1", client)
        assert "not the one on this client's" in (
            await commands_api._destination_still_on_record(tenant, CommandType.SMS, target))
        await admin.execute("UPDATE command_executions SET state='queued' WHERE id=$1",
                            uuid.UUID(ok["command"]["id"]))

        class _Reporter:
            job = {"tenant_id": tenant, "created_by": owner}

            async def progress(self, *_a, **_k):
                return None

        payload = {"command_id": ok["command"]["id"], "command_type": "SMS", "target": target,
                   "draft": {"body": "Hi"}, "tenant_id": tenant}
        with pytest.raises(RuntimeError, match="Not sent: That phone number is not the one"):
            await commands_api._execute_command_job(payload, _Reporter())
        after = await admin.fetchrow("SELECT state, provider_reference FROM command_executions WHERE id=$1",
                                     uuid.UUID(ok["command"]["id"]))
        assert after["state"] == "failed" and after["provider_reference"] is None
        # the record's email removed: the approved email is not sent either
        await admin.execute("UPDATE clients SET email=NULL WHERE id=$1", client)
        assert "No email address is on" in await commands_api._destination_still_on_record(
            tenant, CommandType.EMAIL, commands_api._decode(mail["command"]["target"]))
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
                await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
