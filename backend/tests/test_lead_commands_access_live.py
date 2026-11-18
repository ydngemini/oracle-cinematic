"""An outbound command that names only a lead follows that lead's record
(external audit 26.3, migration 0179), against real PostgreSQL with FORCE RLS
as the application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Before: _require_target_editable checked a client_id and a contact_id only,
the 0142 trigger linked nothing for a lead-only target (so the policies saw a
pool draft), and the execution recheck returned True when no client and no
contact were named. Any agent could stage, and have sent, a text about a
colleague's private lead.

Actors: agent A, agent B (owns a client whose house is a lead), agent C (B's
client shared with C to view). Nothing is sent: the gate and every sender are
patched to fail the test if reached.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_lead_only_commands_follow_the_lead(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import commands_api
    from commands_api import CommandCreate, CommandEdit, CommandType
    from db import connection
    from tenancy import Role, TenantContext

    async def _never(*_a, **_k):
        raise AssertionError("an execution reached the outreach gate or a sender")

    monkeypatch.setattr(commands_api, "guard_outreach", _never)
    monkeypatch.setattr(commands_api, "_resolve_email_provider", _never)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c")}
    agent = {k: f"{k}-{tag}@leads.test" for k in users}
    client = {"a": uuid.uuid4(), "b": uuid.uuid4(), "b2": uuid.uuid4()}
    lead = {"a": uuid.uuid4(), "b": uuid.uuid4(), "pool": uuid.uuid4(), "shared": uuid.uuid4(),
            "foreign": uuid.uuid4(), "doomed": uuid.uuid4()}

    def ctx(who: str) -> TenantContext:
        return TenantContext(agent_id=agent[who], tenant_id=tenant,
                             role=Role.BROKER_OWNER if who == "owner" else Role.AGENT)

    def sms(key: str, **target) -> CommandCreate:
        return CommandCreate(command_type=CommandType.SMS, draft={"body": "Hi there"},
                             target={"phone": "+13025550179", "state_code": "DE",
                                     **{k: str(v) for k, v in target.items()}},
                             idempotency_key=f"leads-{tag}-{key}")

    async def status_of(coro) -> int:
        try:
            await coro
        except HTTPException as exc:
            return exc.status_code
        return 200

    async def staged_before_26_4(body: CommandCreate, who: str) -> str:
        """A draft staged before destinations were bound to the record (26.4):
        a pool lead has no record holding a phone, so it is refused today."""
        real = commands_api._require_destination_on_record

        async def _unchecked(*_a, **_k):
            return None

        monkeypatch.setattr(commands_api, "_require_destination_on_record", _unchecked)
        try:
            return (await commands_api.create_command(body, ctx=ctx(who)))["command"]["id"]
        finally:
            monkeypatch.setattr(commands_api, "_require_destination_on_record", real)

    def row_for(cid):
        return admin.fetchrow("SELECT * FROM command_executions WHERE id=$1", uuid.UUID(cid))

    try:
        for t, slug in ((tenant, "leads"), (other_tenant, "leads-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name, postal_address) VALUES ($1,$2,'Lead drafts',"
                                "'1 Main St, Dover, DE 19901')", t, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)", uid, tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for k, owner in (("a", agent["a"]), ("b", agent["b"]), ("b2", agent["b"])):
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id, phone) "
                                "VALUES ($1,$2,$3,$4,'+13025550179')", client[k], tenant, f"Seller {k}", owner)
        for k, seller in (("a", client["a"]), ("b", client["b"]), ("pool", None), ("shared", client["b2"]),
                          ("doomed", None)):
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5)", lead[k], tenant, f"P-{tag}-{k}", seller, f"{k} St")
        # a lead of ANOTHER brokerage (C3.5: cross-brokerage denial is tested)
        await admin.execute(
            "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, address) "
            "VALUES ($1,$2,$3,'DE',50,'Elsewhere St')", lead["foreign"], other_tenant, f"P-{tag}-foreign")
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'view',$4)", tenant, client["b"], users["c"], agent["b"])
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'edit',$4)", tenant, client["b2"], users["c"], agent["b"])

        # ── staging: own lead and a pool lead stage; a colleague's private
        # lead is 404, a view-only one 403 (before 0179 every one was 200) ──
        own = (await commands_api.create_command(sms("own", lead_id=lead["a"]), ctx=ctx("a")))["command"]["id"]
        # a pool lead (no client): nothing on record vouches for the number (26.4)
        assert await status_of(commands_api.create_command(sms("pool-now", lead_id=lead["pool"]), ctx=ctx("a"))) == 422
        pool = await staged_before_26_4(sms("pool", lead_id=lead["pool"]), "a")
        shared = (await commands_api.create_command(sms("shared", lead_id=lead["shared"]), ctx=ctx("c")))["command"]["id"]
        assert await status_of(commands_api.create_command(sms("sneak-a", lead_id=lead["b"]), ctx=ctx("a"))) == 404
        assert await status_of(commands_api.create_command(sms("sneak-c", lead_id=lead["b"]), ctx=ctx("c"))) == 403
        # a lead with a client that is not its own: refused, whatever the caller may see
        assert await status_of(commands_api.create_command(
            sms("mismatch", lead_id=lead["b"], client_id=client["b2"]), ctx=ctx("b"))) == 422
        # a lead that does not exist, or is another brokerage's, is refused
        assert await status_of(commands_api.create_command(
            sms("ghost", lead_id=uuid.uuid4()), ctx=ctx("a"))) == 404
        assert await status_of(commands_api.create_command(
            sms("foreign", lead_id=lead["foreign"]), ctx=ctx("a"))) == 404
        # an id in another form is stored canonically, and links as such
        odd = (await commands_api.create_command(sms("odd", lead_id=lead["a"].hex.upper()), ctx=ctx("a")))["command"]
        assert odd["target"]["lead_id"] == str(lead["a"])
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1",
                                    uuid.UUID(odd["id"])) == client["a"]
        # the canonical form of the right client is not a mismatch
        same = sms("same", lead_id=lead["b"], client_id=str(client["b"]).upper())
        assert await status_of(commands_api.create_command(same, ctx=ctx("b"))) == 200
        doomed = await staged_before_26_4(sms("doomed", lead_id=lead["doomed"]), "a")

        # The trigger links a lead-only draft (and its approval) to the lead's client.
        for cid, want in ((own, client["a"]), (pool, None)):
            row = await admin.fetchrow(
                "SELECT ce.client_id, aa.client_id AS a_client FROM command_executions ce "
                "JOIN action_approvals aa ON aa.id = ce.approval_id WHERE ce.id=$1", uuid.UUID(cid))
            assert (row["client_id"], row["a_client"]) == (want, want)

        # ── read: B cannot see A's lead draft; the pool one is everyone's ──
        listed = {x["id"] for x in (await commands_api.commands(state_filter=None, limit=200, ctx=ctx("b")))["commands"]}
        assert own not in listed and pool in listed
        assert await status_of(commands_api.command_detail(own, ctx=ctx("b"))) == 404
        # retargeting A's own draft at B's lead: 404
        retarget = CommandEdit(target={"phone": "+13025550179", "state_code": "DE", "lead_id": str(lead["b"])},
                               draft={"body": "Changed"})
        assert await status_of(commands_api.edit_command(own, retarget, ctx=ctx("a"))) == 404
        # The database, without the API: an unknown client named beside a
        # private lead still links the lead's client (refused for C); a lead of
        # a different client than the one named is refused for anyone; a
        # non-canonical id links like the canonical one.
        def raw(key, target):
            return ("INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
                    "draft, idempotency_key, created_by) VALUES ($1,'SMS','explicit_sms','outreach',$2::jsonb,"
                    "'{}'::jsonb,$3,$4)", tenant, target, f"leads-{tag}-{key}")
        import json as _json

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("c")) as conn:
                await conn.execute(*raw("unknown", _json.dumps({"client_id": str(uuid.uuid4()),
                                                                  "lead_id": str(lead["b"])})), agent["c"])
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.tenant_tx(ctx("owner")) as conn:
                await conn.execute(*raw("mixed", _json.dumps({"client_id": str(client["a"]),
                                                                "lead_id": str(lead["b"])})), agent["owner"])
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute(*raw("hex", _json.dumps({"lead_id": lead["b"].hex})), agent["a"])
        # (another brokerage's lead links nothing in this one: no record here to protect)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("c")) as conn:
                await conn.execute(
                    "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
                    "draft, idempotency_key, created_by) VALUES ($1,'SMS','explicit_sms','outreach',$2::jsonb,"
                    "'{}'::jsonb,$3,$4)", tenant, f'{{"lead_id": "{lead["b"]}", "phone": "+13025550179"}}',
                    f"leads-{tag}-db", agent["c"])

        # ── execution re-checks the requester against the lead today ──────
        assert await commands_api._requester_may_contact(tenant, await row_for(own)) is True
        assert await commands_api._requester_may_contact(tenant, await row_for(pool)) is True
        # the lead's client is reassigned to B after A staged: not sent
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client["a"], agent["b"])
        assert await commands_api._requester_may_contact(tenant, await row_for(own)) is False
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client["a"], agent["a"])
        # the pool lead is given to B's client after A staged (the draft's link
        # is from staging time): the lead is re-read, not sent
        await admin.execute("UPDATE leads SET seller_client_id=$2 WHERE id=$1", lead["pool"], client["b"])
        assert await commands_api._requester_may_contact(tenant, await row_for(pool)) is False
        # the lead the draft was about is deleted: not sent
        await admin.execute("DELETE FROM leads WHERE id=$1", lead["doomed"])
        assert await commands_api._requester_may_contact(tenant, await row_for(doomed)) is False
        # C staged on a lead of a client shared with C to edit; the share is
        # revoked before the send: not sent
        assert await commands_api._requester_may_contact(tenant, await row_for(shared)) is True
        await admin.execute("DELETE FROM record_shares WHERE tenant_id=$1 AND resource_id=$2", tenant, client["b2"])
        assert await commands_api._requester_may_contact(tenant, await row_for(shared)) is False

        # The worker stops before the gate and records why.
        await admin.execute("UPDATE command_executions SET state='queued' WHERE id=$1", uuid.UUID(pool))
        stored = await row_for(pool)

        class _Reporter:
            job = {"tenant_id": tenant, "created_by": agent["a"]}

            async def progress(self, *_a, **_k):
                return None

        payload = {"command_id": pool, "command_type": "SMS", "target": commands_api._decode(stored["target"]),
                   "draft": {"body": "Hi there"}, "tenant_id": tenant}
        with pytest.raises(RuntimeError, match="Not sent"):
            await commands_api._execute_command_job(payload, _Reporter())
        after = await row_for(pool)
        assert after["state"] == "failed" and after["provider_reference"] is None

        # The 0179 backfill re-links a lead-only draft written before it (also
        # one whose target carries "client_id": null), and re-deriving an old
        # row whose lead has since moved to another client neither aborts the
        # backfill nor blocks deleting its contact (only a WRITE of a mismatched
        # target is refused). Bounded to these rows, in one transaction.
        import pathlib
        import re as _re

        sql = pathlib.Path(__file__).resolve().parents[1].joinpath(
            "db/migrations/0179_lead_commands_follow_the_record.sql").read_text()
        backfill = _re.search(r"(UPDATE command_executions SET client_id = NULL.*?);", sql, _re.S).group(1)
        old, moved, contact = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await admin.execute(
            "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
            "VALUES ($1,$2,$3,'matrix','pending_encryption')", contact, tenant, agent["b"])
        moved_target = _json.dumps({"client_id": str(client["b"]), "contact_id": str(contact),
                                    "lead_id": str(lead["b"]), "phone": "+13025550179"})
        async with connection.tenant_tx(ctx("b")) as conn:      # staged while the lead was B's client's
            await conn.execute(*raw("moved", moved_target), agent["b"])
            moved = await conn.fetchval("SELECT id FROM command_executions WHERE idempotency_key=$1",
                                        f"leads-{tag}-moved")
        await admin.execute("UPDATE leads SET seller_client_id=$2 WHERE id=$1", lead["b"], client["b2"])
        async with admin.transaction():
            await admin.execute("ALTER TABLE command_executions DISABLE TRIGGER trg_command_executions_link")
            await admin.execute(
                "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, "
                "draft, idempotency_key, created_by) VALUES ($1,$2,'SMS','explicit_sms','outreach',$3::jsonb,"
                "'{}'::jsonb,$4,$5)", old, tenant, _json.dumps({"client_id": None, "lead_id": str(lead["a"])}),
                f"leads-{tag}-old", agent["a"])
            await admin.execute("ALTER TABLE command_executions ENABLE TRIGGER trg_command_executions_link")
            assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1", old) is None
            await admin.execute(backfill + " AND id = ANY($1::uuid[])", [old, moved])
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1", old) == client["a"]
        assert await admin.fetchval("SELECT client_id FROM command_executions WHERE id=$1", moved) == client["b"]
        await admin.execute("DELETE FROM agent_contacts WHERE id=$1", contact)       # FK SET NULL re-derives
        assert await admin.fetchval("SELECT contact_id FROM command_executions WHERE id=$1", moved) is None
        # ...and the send is still refused for the requester if they cannot reach the lead's client now
        assert await commands_api._requester_may_contact(tenant, await row_for(str(moved))) is True  # B owns both
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client["b2"], agent["a"])
        assert await commands_api._requester_may_contact(tenant, await row_for(str(moved))) is False
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
