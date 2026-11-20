"""A data-subject deletion redacts the person's drafts, approvals and send
jobs and keeps the record that an action happened (ledger L17-38, migration
0182, owner decision 2026-11-20), against real PostgreSQL.

Before: the drafts (command_executions), their approvals (action_approvals)
and the execute jobs carrying a copy of the approved draft (automation_jobs)
survived a deletion with the message, the address and the person's name in
them, only unlinked — and unlinked, every agent of the brokerage could read
them. Now their content and free text are replaced by the marker
{"redacted": "data_subject_erasure", "at": ...}; ids, type, channel, state,
times, who asked and who decided and the provider's message id stay; an
unsent draft is cancelled and a pending approval revoked. Rows tied to
someone else (another client's draft, an unlinked draft to another address,
a draft linked to another client that carries this person's address) are
not touched. A restore that brings a draft's content back is redacted again.

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

MASTER = "d" * 64


def test_deletion_redacts_drafts_and_keeps_the_action_record(monkeypatch, tmp_path):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    asyncio.run(_scenario())


def _j(value):
    return json.loads(value) if isinstance(value, str) else value


async def _scenario():
    import asyncpg

    import privacy_lifecycle as pl
    import privacy_requests
    from crypto import derive_tenant_key
    from db import connection
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    x, y, kx = (str(uuid.uuid4()) for _ in range(3))
    x_email, y_email = f"x-{s}@client.test", f"y-{s}@client.test"
    x_phone = "+13025550181"
    cmd1, cmd2, cmd3, cmd4, cmd_y, cmd_other, cmd_yx = (str(uuid.uuid4()) for _ in range(7))
    ref1 = f"<msg-{s}@neoh.test>"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def ins_approval(cmd, target, body, status, *, target_type="command", target_id=None, reason=None):
        payload = {"command_id": cmd, "target": target, "draft": {"body": body}, "context": {}, "tenant_id": T}
        decided = status != "pending"
        return str(await admin.fetchval(
            "INSERT INTO action_approvals (tenant_id, action_type, risk_class, target_type, target_id, payload_hash, "
            "draft_payload, status, requested_by, decided_by, decided_at, reason, expires_at) VALUES "
            "($1,'command:EMAIL','outreach',$2,$3,repeat('c',64),$4::jsonb,$5,$6,$7,$8,$9, now() + interval '1 day') "
            "RETURNING id", T, target_type, target_id or cmd, json.dumps(payload), status, owner,
            owner if decided else None, (await admin.fetchval("SELECT now() - interval '2 hours'")) if decided else None,
            reason))

    async def ins_job(cmd, approval, payload, state, result=None, status_message=None):
        return str(await admin.fetchval(
            "INSERT INTO automation_jobs (tenant_id, job_type, state, risk_class, payload, result, idempotency_key, "
            "approval_id, created_by, status_message, next_retry_at) VALUES ($1,'command:execute',$2,'outreach',"
            "$3::jsonb,$4::jsonb,$5,$6,$7,$8, CASE WHEN $2 = 'queued' THEN now() + interval '1 hour' END) RETURNING id",
            T, state, json.dumps(payload), json.dumps(result) if result else None,
            f"command:{cmd}:execute:{approval}", approval, owner, status_message))

    async def ins_command(cmd, ctype, target, body, state, *, approval=None, job=None, provider=None,
                          ref=None, last_error=None, recon=None):
        await admin.execute(
            "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, draft, "
            "state, approval_id, job_id, idempotency_key, provider, provider_reference, provider_submitted_at, "
            "last_error, reconciliation_reason, created_by) VALUES ($1,$2,$3,'explicit','outreach',$4::jsonb,"
            "$5::jsonb,$6,$7,$8,$9,$10,$11, CASE WHEN $11::text IS NULL THEN NULL ELSE now() - interval '1 hour' END,"
            "$12,$13,$14)",
            cmd, T, ctype, json.dumps(target), json.dumps({"content": {"subject": f"Hi X {s}", "body": body},
                                                          "context": {"why": f"X {s} asked"}}),
            state, approval, job, f"ai:m-{s}:{cmd}", provider, ref, last_error, recon, owner)

    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                T, f"redact-{s}", f"Brokerage {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, email, phone) VALUES "
                "($1,$3,$4,$5,'302-555-0181'), ($2,$3,$6,$7,'302-555-0182')",
                x, y, T, f"X {s}", x_email, f"Y {s}", y_email)
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, legacy_client_id, pii_ciphertext) "
                "VALUES ($1,$2,$3,$4, pgp_sym_encrypt($5,$6))", kx, T, owner, x,
                json.dumps({"full_name": f"X {s}", "phone": x_phone}), derive_tenant_key(T, MASTER))
            await admin.execute("UPDATE clients SET contact_id=$1 WHERE id=$2", kx, x)

            xt = {"client_id": x, "email": x_email}
            # 1. sent email: approval approved, job succeeded, provider id kept.
            a1 = await ins_approval(cmd1, xt, f"hello x {s}", "approved", reason=f"fine for X {s}")
            j1 = await ins_job(cmd1, a1, {"command_id": cmd1, "command_type": "EMAIL", "target": xt,
                                          "draft": {"body": f"hello x {s}"}, "tenant_id": T}, "succeeded",
                               result={"provider": "smtp", "reference": ref1, "status": "sent",
                                       "detail": {"to": x_email}})
            await ins_command(cmd1, "EMAIL", xt, f"hello x {s}", "succeeded", approval=a1, job=j1,
                              provider="smtp", ref=ref1, recon=f"confirmed with X {s} by phone")
            # 2. a text linked by CONTACT only, waiting for approval.
            kt = {"contact_id": kx, "phone": x_phone, "state_code": "DE"}
            a2 = await ins_approval(cmd2, kt, f"text x {s}", "pending")
            await ins_command(cmd2, "SMS", kt, f"text x {s}", "awaiting_approval", approval=a2)
            # 3. an email linked to NO record, to X's address (case differs): failed.
            await ins_command(cmd3, "EMAIL", {"email": x_email.upper()}, f"to x {s}", "failed",
                              last_error=f"550 {x_email} mailbox unavailable")
            # 4. approved and queued, its job not yet run.
            a4 = await ins_approval(cmd4, xt, f"queued x {s}", "approved")
            j4 = await ins_job(cmd4, a4, {"command_id": cmd4, "command_type": "EMAIL", "target": xt,
                                          "draft": {"body": f"queued x {s}"}, "tenant_id": T}, "queued")
            await ins_command(cmd4, "EMAIL", xt, f"queued x {s}", "queued", approval=a4, job=j4)
            # 5. an approval that is not a command's, about X's contact.
            a5 = await ins_approval(cmd2, {"contact_id": kx}, f"other x {s}", "rejected",
                                    target_type="contact", target_id=kx, reason=f"no, X {s} said stop")

            # Someone else's rows: none of these may change.
            yt = {"client_id": y, "email": y_email}
            a_y = await ins_approval(cmd_y, yt, f"hello y {s}", "approved")
            j_y = await ins_job(cmd_y, a_y, {"command_id": cmd_y, "command_type": "EMAIL", "target": yt,
                                             "draft": {"body": f"hello y {s}"}, "tenant_id": T}, "succeeded")
            await ins_command(cmd_y, "EMAIL", yt, f"hello y {s}", "succeeded", approval=a_y, job=j_y,
                              provider="smtp", ref=f"<y-{s}>")
            await ins_command(cmd_other, "EMAIL", {"email": f"other-{s}@client.test"}, f"other {s}", "awaiting_approval")
            # Linked to Y, yet carrying X's address: tied to Y, never matched by address.
            await ins_command(cmd_yx, "EMAIL", {"client_id": y, "email": x_email}, f"yx {s}", "awaiting_approval")
            j_other = str(await admin.fetchval(
                "INSERT INTO automation_jobs (tenant_id, job_type, state, payload, idempotency_key, created_by) "
                "VALUES ($1,'privacy:export','succeeded',$2::jsonb,$3,$4) RETURNING id",
                T, json.dumps({"note": x_email}), f"export-{s}", owner))

        x_rows = {"command_executions": [cmd1, cmd2, cmd3, cmd4],
                  "action_approvals": [a1, a2, a4, a5], "automation_jobs": [j1, j4]}
        untouched = {"command_executions": [cmd_y, cmd_other, cmd_yx],
                     "action_approvals": [a_y], "automation_jobs": [j_y, j_other]}

        async def rows(table, ids):
            return {str(r["id"]): dict(r) for r in await admin.fetch(
                f"SELECT * FROM {table} WHERE id = ANY($1::uuid[])", ids)}

        before = {t: await rows(t, ids) for t, ids in x_rows.items()}
        before_other = {t: await rows(t, ids) for t, ids in untouched.items()}

        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        async def ask(kind, preview=False):
            return await privacy_requests.handle_subject_request(
                octx, kind=kind, email=x_email, phone=None, reason="asked", preview=preview)

        # Access returns them, the address-only draft and the jobs included.
        found = await ask("dsr_access")
        for table, ids in x_rows.items():
            assert sorted(r["id"] for r in found["records"][table]) == sorted(ids), table
        assert cmd_yx not in json.dumps(found["records"]) and cmd_other not in json.dumps(found["records"])

        done = await ask("dsr_delete")
        assert done["redacted"] == {"command_executions": 4, "action_approvals": 4, "automation_jobs": 2}
        assert "kept_unlinked" not in done and done["redaction"] == privacy_requests.REDACTION_NOTE
        stored = _j(await admin.fetchval("SELECT receipt FROM privacy_operations WHERE id=$1::uuid",
                                         done["operation_id"]))
        assert stored["redacted"] == done["redacted"]

        after = {t: await rows(t, ids) for t, ids in x_rows.items()}
        # Nothing about the person is left in any of their rows.
        blob = json.dumps(after, default=str).lower()
        for needle in (x_email.lower(), "5550181", f"x {s}".lower(), str(x), str(kx)):
            assert needle not in blob, needle

        marker = None
        for table, col in (("command_executions", "draft"), ("command_executions", "target"),
                           ("action_approvals", "draft_payload")):
            for row in after[table].values():
                value = _j(row[col])
                assert set(value) == {"redacted", "at"} and value["redacted"] == "data_subject_erasure", (table, col)
                marker = marker or value
                assert value == marker   # one shape, one time, everywhere
                assert row["client_id"] is None and row["contact_id"] is None

        # The skeleton of each command stays; unsent ones are cancelled.
        keep = ("id", "tenant_id", "command_type", "classification", "risk_class", "approval_id", "job_id",
                "idempotency_key", "provider", "provider_reference", "provider_submitted_at", "scheduled_at",
                "created_by", "created_at", "reconcile_attempts", "reconcile_checked_at")
        for rid, row in after["command_executions"].items():
            assert {k: row[k] for k in keep} == {k: before["command_executions"][rid][k] for k in keep}, rid
        ce = after["command_executions"]
        assert [ce[c]["state"] for c in (cmd1, cmd2, cmd3, cmd4)] == ["succeeded", "cancelled", "failed", "cancelled"]
        assert ce[cmd1]["provider_reference"] == ref1 and ce[cmd1]["last_error"] is None
        assert ce[cmd1]["reconciliation_reason"].startswith("Redacted:")
        assert ce[cmd2]["last_error"].startswith("Cancelled:") and ce[cmd4]["last_error"].startswith("Cancelled:")
        assert ce[cmd3]["last_error"].startswith("Redacted:")

        # Approvals: decision kept; the pending one revoked by the requester.
        keep_a = ("id", "tenant_id", "action_type", "risk_class", "target_type", "payload_hash", "requested_by",
                  "requested_at", "expires_at", "created_at")
        aa = after["action_approvals"]
        for rid, row in aa.items():
            assert {k: row[k] for k in keep_a} == {k: before["action_approvals"][rid][k] for k in keep_a}, rid
        assert [aa[a]["status"] for a in (a1, a2, a4, a5)] == ["approved", "revoked", "approved", "rejected"]
        for a in (a1, a4, a5):
            assert (aa[a]["decided_by"], aa[a]["decided_at"]) == (
                before["action_approvals"][a]["decided_by"], before["action_approvals"][a]["decided_at"])
        assert aa[a2]["decided_by"] == owner and aa[a2]["decided_at"] is not None
        assert aa[a2]["reason"].startswith("Cancelled:") and aa[a1]["reason"].startswith("Redacted:")
        assert aa[a5]["reason"].startswith("Redacted:")
        assert aa[a1]["target_id"] == cmd1 and aa[a5]["target_id"] == "redacted"

        # Jobs: the copy of the draft goes, the ids the worker reads stay.
        aj = after["automation_jobs"]
        assert _j(aj[j1]["payload"]) == {**marker, "command_id": cmd1, "command_type": "EMAIL"}
        assert _j(aj[j1]["result"]) == {**marker, "provider": "smtp", "reference": ref1, "status": "sent"}
        assert aj[j1]["state"] == "succeeded"
        assert aj[j4]["state"] == "cancelled" and aj[j4]["next_retry_at"] is None
        assert aj[j4]["status_message"].startswith("Cancelled:") and aj[j4]["result"] is None

        # Someone else's rows are byte-for-byte what they were.
        assert {t: await rows(t, ids) for t, ids in untouched.items()} == before_other

        ledger = {r["target"]: r for r in await admin.fetch(
            "SELECT target, action, rows_affected, detail FROM erasure_ledger WHERE operation_id=$1::uuid "
            "AND target IN ('command_executions','action_approvals','automation_jobs')", done["operation_id"])}
        assert {t: (r["action"], r["rows_affected"], _j(r["detail"])["unsent_cancelled"])
                for t, r in ledger.items()} == {
            "command_executions": ("anonymized", 4, 2), "action_approvals": ("anonymized", 4, 1),
            "automation_jobs": ("anonymized", 2, 1)}

        # The SQL guard: only execute jobs, only for a running deletion of this tenant.
        async with tenant_tx(octx) as conn:
            op = await pl._create_operation(conn, tenant_id=T, kind="dsr_delete", requested_by=owner,
                                            subject_kind="contact", subject_ref=f"ref-{s}", state="running")
            assert await conn.fetchval("SELECT privacy_erase_subject_rows($1,'automation_jobs',$2::uuid[])",
                                       op["id"], [j_other]) == 0
        assert (await rows("automation_jobs", [j_other]))[j_other] == before_other["automation_jobs"][j_other]
        other_t = str(uuid.uuid4())
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Other')", other_t, f"other-{s}")
        try:
            async with tenant_tx(TenantContext(agent_id=owner, tenant_id=other_t, role=Role.BROKER_OWNER)) as conn:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.fetchval("SELECT privacy_erase_subject_rows($1,'command_executions',$2::uuid[])",
                                        op["id"], [cmd_y])
        finally:
            await admin.execute("DELETE FROM tenants WHERE id=$1", other_t)
        assert (await rows("command_executions", [cmd_y]))[cmd_y] == before_other["command_executions"][cmd_y]

        # A restore that brings a draft's content back: re-applied, redacted again.
        await admin.execute(
            "UPDATE command_executions SET target=$2::jsonb, draft=$3::jsonb WHERE id=$1", cmd1,
            json.dumps(_j(before["command_executions"][cmd1]["target"])),
            json.dumps(_j(before["command_executions"][cmd1]["draft"])))
        key = done["restore_directive"]
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=False) if e.get("directive") == key]
        assert mine[0]["status"] == "resurrected" and mine[0]["rows_back"] == {"command_executions": 1}
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=True) if e.get("directive") == key]
        assert mine[0]["status"] == "reapplied" and mine[0]["redacted"]["command_executions"] == 1
        again = (await rows("command_executions", [cmd1]))[cmd1]
        assert _j(again["draft"])["redacted"] == "data_subject_erasure" and x_email not in json.dumps(_j(again["target"]))
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=False) if e.get("directive") == key]
        assert mine[0]["status"] == "still_erased"
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
