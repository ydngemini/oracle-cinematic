"""A data-subject deletion redacts the person's drafts, approvals, send jobs
and every copy of them the database keeps, and keeps the record that an
action happened (ledger L17-38, migration 0182, owner decision 2026-11-20),
against real PostgreSQL.

Before: the drafts (command_executions), their approvals (action_approvals),
the execute jobs carrying a copy of the approved draft (automation_jobs, and
their attempts' error text) and the AI decision traces holding the whole
draft survived a deletion with the message, the address and the person's
name in them, only unlinked — and unlinked, every agent of the brokerage
could read the drafts. Now their content and free text are replaced by the
marker {"redacted": "data_subject_erasure", "at": ...}; ids, type, channel,
state, times, who asked and who decided and the provider's message id stay;
an unsent draft is cancelled, a pending approval revoked, a waiting job
cancelled — including one queued after the request's discovery — and the
receipt says, from counts, what was sent, cancelled or in flight. Rows tied
to someone else (another client's draft, an unlinked draft to another
address, a draft linked to another client that carries this person's
address, another brokerage's rows passed by id) are not touched; a legal
hold refuses; the app role still cannot rewrite a trace; the worker stops a
cancelled job for good; the SMS log repair leaves redacted commands alone; a
restore that brings a draft's content back is redacted again.

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
    asyncio.run(_scenario(monkeypatch))


def _j(value):
    return json.loads(value) if isinstance(value, str) else value


async def _scenario(monkeypatch):
    import asyncpg

    import commands_api
    import messaging_data
    import privacy_lifecycle as pl
    import privacy_requests
    from crypto import derive_tenant_key
    from db import connection
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T, O = str(uuid.uuid4()), str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    x, y, kx = (str(uuid.uuid4()) for _ in range(3))
    x_email, y_email = f"x-{s}@client.test", f"y-{s}@client.test"
    x_phone = "+13025550181"
    cmd1, cmd2, cmd3, cmd4, cmd5, cmd6, cmd_y, cmd_other, cmd_yx, cmd_o = (str(uuid.uuid4()) for _ in range(10))
    ref1, ref5 = f"<msg-{s}@neoh.test>", f"telnyx-{s}"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    async def ins_approval(cmd, target, body, status, *, target_type="command", target_id=None, reason=None,
                           tenant=None):
        tenant = tenant or T
        payload = {"command_id": cmd, "target": target, "draft": {"body": body}, "context": {}, "tenant_id": tenant}
        decided = status != "pending"
        return str(await admin.fetchval(
            "INSERT INTO action_approvals (tenant_id, action_type, risk_class, target_type, target_id, payload_hash, "
            "draft_payload, status, requested_by, decided_by, decided_at, reason, expires_at) VALUES "
            "($1,'command:EMAIL','outreach',$2,$3,repeat('c',64),$4::jsonb,$5,$6,$7,$8,$9, now() + interval '1 day') "
            "RETURNING id", tenant, target_type, target_id or cmd, json.dumps(payload), status, owner,
            owner if decided else None, (await admin.fetchval("SELECT now() - interval '2 hours'")) if decided else None,
            reason))

    async def ins_job(cmd, approval, payload, state, result=None, *, tenant=None, key=""):
        return str(await admin.fetchval(
            "INSERT INTO automation_jobs (tenant_id, job_type, state, risk_class, payload, result, idempotency_key, "
            "approval_id, created_by, next_retry_at) VALUES ($1,'command:execute',$2,'outreach',"
            "$3::jsonb,$4::jsonb,$5,$6,$7, CASE WHEN $2 = 'queued' THEN now() + interval '1 hour' END) RETURNING id",
            tenant or T, state, json.dumps(payload), json.dumps(result) if result else None,
            f"command:{cmd}:execute:{approval}{key}", approval, owner))

    async def ins_command(cmd, ctype, target, body, state, *, approval=None, job=None, provider=None,
                          ref=None, last_error=None, recon=None, tenant=None):
        await admin.execute(
            "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, draft, "
            "state, approval_id, job_id, idempotency_key, provider, provider_reference, provider_submitted_at, "
            "last_error, reconciliation_reason, created_by) VALUES ($1,$2,$3,'explicit','outreach',$4::jsonb,"
            "$5::jsonb,$6,$7,$8,$9,$10,$11, CASE WHEN $11::text IS NULL THEN NULL ELSE now() - interval '1 hour' END,"
            "$12,$13,$14)",
            cmd, tenant or T, ctype, json.dumps(target),
            json.dumps({"content": {"subject": f"Hi X {s}", "body": body}, "context": {"why": f"X {s} asked"}}),
            state, approval, job, f"ai:m-{s}:{cmd}", provider, ref, last_error, recon, owner)

    async def ins_trace(approval, proposal, signal, final=None, *, tenant=None):
        return str(await admin.fetchval(
            "INSERT INTO ai_decision_traces (tenant_id, agent_id, surface, action_type, risk_class, model_version, "
            "source_table, source_id, proposal, proposal_sha256, final, final_sha256, signal, decided_at, "
            "decision_latency_ms) VALUES ($1,$2,'approval','command:EMAIL','outreach','base:test','action_approvals',"
            "$3,$4::jsonb,repeat('a',64),$5::jsonb,CASE WHEN $5::jsonb IS NULL THEN NULL ELSE repeat('b',64) END,"
            "$6, now() - interval '2 hours', 1200) RETURNING id",
            tenant or T, owner, approval, json.dumps(proposal), json.dumps(final) if final else None, signal))

    async def ins_attempt(job, detail, *, tenant=None):
        await admin.execute(
            "INSERT INTO automation_job_attempts (tenant_id, job_id, attempt_number, worker_id, lease_token, "
            "finished_at, outcome, error_code, error_detail) VALUES ($1,$2,1,'worker-1',gen_random_uuid(),now(),"
            "'failed','SMTP_REFUSED',$3)", tenant or T, job, detail)

    try:
        async with admin.transaction():
            for tid, slug in ((T, f"redact-{s}"), (O, f"other-{s}")):
                await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", tid, slug, f"B {slug}")
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
            # 1. sent email: approval approved (traced), job succeeded with a refused attempt.
            a1 = await ins_approval(cmd1, xt, f"hello x {s}", "approved", reason=f"fine for X {s}")
            j1 = await ins_job(cmd1, a1, {"command_id": cmd1, "command_type": "EMAIL", "target": xt,
                                          "draft": {"body": f"hello x {s}"}, "tenant_id": T}, "succeeded",
                               result={"provider": "smtp", "reference": ref1, "status": "sent",
                                       "detail": {"to": x_email}})
            await ins_command(cmd1, "EMAIL", xt, f"hello x {s}", "succeeded", approval=a1, job=j1,
                              provider="smtp", ref=ref1, recon=f"confirmed with X {s} by phone")
            await ins_attempt(j1, f"550 5.1.1 <{x_email}> recipient refused")
            t1 = await ins_trace(a1, {"target": xt, "draft": {"body": f"hello x {s}"}}, "accepted_unchanged")
            # 2. a text linked by CONTACT only, waiting for approval.
            kt = {"contact_id": kx, "phone": x_phone, "state_code": "DE"}
            a2 = await ins_approval(cmd2, kt, f"text x {s}", "pending")
            await ins_command(cmd2, "SMS", kt, f"text x {s}", "awaiting_approval", approval=a2)
            # 3. an email linked to NO record, to X's address (case differs): failed.
            await ins_command(cmd3, "EMAIL", {"email": x_email.upper()}, f"to x {s}", "failed",
                              last_error=f"550 {x_email} mailbox unavailable")
            # 4. approved after an edit (traced 'edited') and queued, its job not yet run.
            a4 = await ins_approval(cmd4, xt, f"queued x {s}", "approved")
            j4 = await ins_job(cmd4, a4, {"command_id": cmd4, "command_type": "EMAIL", "target": xt,
                                          "draft": {"body": f"queued x {s}"}, "tenant_id": T}, "queued")
            await ins_command(cmd4, "EMAIL", xt, f"queued x {s}", "queued", approval=a4, job=j4)
            t4 = await ins_trace(a4, {"target": xt, "draft": {"body": f"first x {s}"}}, "edited",
                                 final={"target": xt, "draft": {"body": f"queued x {s}"}})
            # 5. an approval that is not a command's, about X's contact (rejected, traced).
            a5 = await ins_approval(cmd2, {"contact_id": kx}, f"other x {s}", "rejected",
                                    target_type="contact", target_id=kx, reason=f"no, X {s} said stop")
            t5 = await ins_trace(a5, {"target": {"contact_id": kx}, "body": f"other x {s}"}, "rejected")
            # 6. a text Telnyx accepted whose log row is missing (the repair sweep's case).
            await ins_command(cmd5, "SMS", {"client_id": x, "phone": x_phone, "state_code": "DE"}, f"sms x {s}",
                              "succeeded", provider="telnyx", ref=ref5)
            # 7. an email being sent at this moment.
            await ins_command(cmd6, "EMAIL", xt, f"inflight x {s}", "executing")

            # Someone else's rows: none of these may change.
            yt = {"client_id": y, "email": y_email}
            a_y = await ins_approval(cmd_y, yt, f"hello y {s}", "approved")
            j_y = await ins_job(cmd_y, a_y, {"command_id": cmd_y, "command_type": "EMAIL", "target": yt,
                                             "draft": {"body": f"hello y {s}"}, "tenant_id": T}, "succeeded")
            await ins_command(cmd_y, "EMAIL", yt, f"hello y {s}", "succeeded", approval=a_y, job=j_y,
                              provider="smtp", ref=f"<y-{s}>")
            await ins_attempt(j_y, f"451 <{y_email}> try later")
            t_y = await ins_trace(a_y, {"target": yt, "draft": {"body": f"hello y {s}"}}, "accepted_unchanged")
            await ins_command(cmd_other, "EMAIL", {"email": f"other-{s}@client.test"}, f"other {s}", "awaiting_approval")
            # Linked to Y, yet carrying X's address: tied to Y, never matched by address.
            await ins_command(cmd_yx, "EMAIL", {"client_id": y, "email": x_email}, f"yx {s}", "awaiting_approval")
            j_other = str(await admin.fetchval(
                "INSERT INTO automation_jobs (tenant_id, job_type, state, payload, idempotency_key, created_by) "
                "VALUES ($1,'privacy:export','succeeded',$2::jsonb,$3,$4) RETURNING id",
                T, json.dumps({"note": x_email}), f"export-{s}", owner))
            # Another brokerage's draft to the same address, with its copies.
            ot = {"email": x_email}
            a_o = await ins_approval(cmd_o, ot, f"o x {s}", "approved", tenant=O)
            j_o = await ins_job(cmd_o, a_o, {"command_id": cmd_o, "command_type": "EMAIL", "target": ot,
                                             "draft": {"body": f"o x {s}"}, "tenant_id": O}, "succeeded", tenant=O)
            await ins_command(cmd_o, "EMAIL", ot, f"o x {s}", "succeeded", approval=a_o, job=j_o, tenant=O,
                              provider="smtp", ref=f"<o-{s}>")
            await ins_attempt(j_o, f"550 <{x_email}> refused", tenant=O)
            t_o = await ins_trace(a_o, {"target": ot, "draft": {"body": f"o x {s}"}}, "accepted_unchanged", tenant=O)

        x_rows = {"command_executions": [cmd1, cmd2, cmd3, cmd4, cmd5, cmd6],
                  "action_approvals": [a1, a2, a4, a5], "automation_jobs": [j1, j4],
                  "ai_decision_traces": [t1, t4, t5]}
        untouched = {"command_executions": [cmd_y, cmd_other, cmd_yx, cmd_o],
                     "action_approvals": [a_y, a_o], "automation_jobs": [j_y, j_other, j_o],
                     "ai_decision_traces": [t_y, t_o]}

        async def rows(table, ids):
            return {str(r["id"]): dict(r) for r in await admin.fetch(
                f"SELECT * FROM {table} WHERE id = ANY($1::uuid[])", ids)}

        async def attempts(job):
            return [r["error_detail"] for r in await admin.fetch(
                "SELECT error_detail FROM automation_job_attempts WHERE job_id=$1", job)]

        before = {t: await rows(t, ids) for t, ids in x_rows.items()}
        before_other = {t: await rows(t, ids) for t, ids in untouched.items()}
        before_attempts = {j: await attempts(j) for j in (j_y, j_o)}

        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        async def ask(kind):
            return await privacy_requests.handle_subject_request(
                octx, kind=kind, email=x_email, phone=None, reason="asked", preview=False)

        # Access returns them, the address-only draft and the jobs included.
        found = await ask("dsr_access")
        for table in ("command_executions", "action_approvals", "automation_jobs"):
            assert sorted(r["id"] for r in found["records"][table]) == sorted(x_rows[table]), table
        assert cmd_yx not in json.dumps(found["records"]) and cmd_other not in json.dumps(found["records"])

        # A release between discovery and the deletion queues another job for cmd4.
        real_delete = privacy_requests._delete_subject
        late = {}

        async def delete_after_a_late_release(ctx, op_id, found_ids):
            late["job"] = await ins_job(cmd4, a4, {"command_id": cmd4, "command_type": "EMAIL", "target": xt,
                                                   "draft": {"body": f"late x {s}"}, "tenant_id": T},
                                        "queued", key=":late")
            return await real_delete(ctx, op_id, found_ids)

        monkeypatch.setattr(privacy_requests, "_delete_subject", delete_after_a_late_release)
        done = await ask("dsr_delete")
        j_late = late["job"]
        x_rows["automation_jobs"].append(j_late)
        assert done["redacted"] == {"command_executions": 6, "action_approvals": 4, "automation_jobs": 3}
        assert done["redaction_counts"] == {
            "sent": 2, "unsent_cancelled": 2, "in_flight": 1, "not_sent": 1, "jobs_cancelled": 2,
            "job_attempt_errors": 1, "approvals_revoked": 1, "decision_traces": 3}
        note = done["redaction"]
        assert note == privacy_requests.redaction_note(done["redacted"], done["redaction_counts"])
        for said in ("6 drafts, 4 approvals, 3 send jobs, the error text of 1 send attempt, 3 AI decision records",
                     "2 drafts had already been sent: those messages stay with their recipients.",
                     "2 drafts had not been sent and were cancelled; they will not be sent.",
                     "1 draft was being sent at the moment of deletion",
                     "1 draft had failed or been cancelled earlier and was not sent.",
                     "1 approval still waiting was revoked.", "2 waiting send jobs were cancelled.",
                     "Not reached by this deletion", "audit log"):
            assert said in note, said
        assert "unconfirmed" not in note and "kept_unlinked" not in done
        stored = _j(await admin.fetchval("SELECT receipt FROM privacy_operations WHERE id=$1::uuid",
                                         done["operation_id"]))
        assert stored["redacted"] == done["redacted"] and stored["redaction"] == note

        after = {t: await rows(t, ids) for t, ids in x_rows.items()}
        # Nothing about the person is left in any of their rows or copies.
        blob = (json.dumps(after, default=str) + json.dumps(await attempts(j1))).lower()
        for needle in (x_email.lower(), "5550181", f"x {s}".lower(), str(x), str(kx)):
            assert needle not in blob, needle
        assert await attempts(j1) == ["Redacted: the person it was for was deleted by a data-subject request."]

        marker = None
        for table, col in (("command_executions", "draft"), ("command_executions", "target"),
                           ("action_approvals", "draft_payload"), ("ai_decision_traces", "proposal")):
            for row in after[table].values():
                value = _j(row[col])
                assert set(value) == {"redacted", "at"} and value["redacted"] == "data_subject_erasure", (table, col)
                marker = marker or value
                assert value == marker   # one shape, one time, everywhere
                if table != "ai_decision_traces":
                    assert row["client_id"] is None and row["contact_id"] is None

        # The skeleton of each command stays; unsent ones are cancelled, the in-flight one is left to its worker.
        keep = ("id", "tenant_id", "command_type", "classification", "risk_class", "approval_id", "job_id",
                "idempotency_key", "provider", "provider_reference", "provider_submitted_at", "scheduled_at",
                "created_by", "created_at", "reconcile_attempts", "reconcile_checked_at")
        for rid, row in after["command_executions"].items():
            assert {k: row[k] for k in keep} == {k: before["command_executions"][rid][k] for k in keep}, rid
        ce = after["command_executions"]
        assert [ce[c]["state"] for c in (cmd1, cmd2, cmd3, cmd4, cmd5, cmd6)] == [
            "succeeded", "cancelled", "failed", "cancelled", "succeeded", "executing"]
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

        # Traces: same rows (links to them hold), decision skeleton kept, content gone, out of training.
        keep_t = ("id", "tenant_id", "agent_id", "surface", "action_type", "risk_class", "model_version",
                  "source_table", "source_id", "proposal_sha256", "final_sha256", "signal", "decided_at",
                  "decision_latency_ms", "outcome_kind", "created_at")
        at = after["ai_decision_traces"]
        for rid, row in at.items():
            assert {k: row[k] for k in keep_t} == {k: before["ai_decision_traces"][rid][k] for k in keep_t}, rid
            assert row["revoked_at"] is not None
        assert at[t1]["final"] is None and at[t5]["final"] is None and _j(at[t4]["final"]) == marker

        # Jobs: the copy of the draft goes, the ids the worker reads stay; the late one too.
        aj = after["automation_jobs"]
        assert _j(aj[j1]["payload"]) == {**marker, "command_id": cmd1, "command_type": "EMAIL"}
        assert _j(aj[j1]["result"]) == {**marker, "provider": "smtp", "reference": ref1, "status": "sent"}
        assert aj[j1]["state"] == "succeeded"
        for job in (j4, j_late):
            assert aj[job]["state"] == "cancelled" and aj[job]["next_retry_at"] is None, job
            assert aj[job]["status_message"].startswith("Cancelled:") and aj[job]["result"] is None
            assert _j(aj[job]["payload"]) == {**marker, "command_id": cmd4, "command_type": "EMAIL"}

        ledger = {r["target"]: (r["action"], r["rows_affected"], _j(r["detail"])) for r in await admin.fetch(
            "SELECT target, action, rows_affected, detail FROM erasure_ledger WHERE operation_id=$1::uuid "
            "AND target IN ('command_executions','action_approvals','automation_jobs')", done["operation_id"])}
        assert ledger == {
            "command_executions": ("anonymized", 6, {
                "mode": "redacted", "unsent_cancelled": 2, "sent": 2, "in_flight": 1, "uncertain": 0,
                "not_sent": 1, "jobs": {"jobs": 3, "jobs_cancelled": 2, "attempt_errors": 1}}),
            "automation_jobs": ("anonymized", 0, {"mode": "redacted", "jobs_cancelled": 0, "attempt_errors": 0}),
            "action_approvals": ("anonymized", 4, {"mode": "redacted", "pending_revoked": 1, "decision_traces": 3})}

        # The worker stops a cancelled command's job for good (no retries holding its payload).
        class Reporter:
            job = {"tenant_id": T, "approval_id": a4, "created_by": owner}

            async def progress(self, *_a, **_k):
                raise AssertionError("must stop before any work")

        with pytest.raises(RuntimeError) as stopped:
            await commands_api._execute_command_job(_j(aj[j4]["payload"]), Reporter())
        assert getattr(stopped.value, "job_terminal", False) is True
        assert getattr(stopped.value, "job_error_code", "") == "COMMAND_CANCELLED"

        # The Telnyx log-row repair never picks up a redacted command.
        assert await messaging_data.repair_missing_outbound_rows(only_tenant=T) == {
            "repaired": 0, "erased": 0, "no_sender": 0, "failed": 0}
        assert (await rows("command_executions", [cmd5]))[cmd5]["reconcile_checked_at"] is None

        # The app role still cannot rewrite a trace, not even to the exact marker inside a
        # privacy operation, nor call the job helper.
        async with tenant_tx(octx) as conn:
            await conn.execute("SELECT set_config('app.privacy_operation', $1, true)", str(uuid.uuid4()))
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await conn.execute("UPDATE ai_decision_traces SET proposal=$2::jsonb WHERE id=$1",
                                   t_y, json.dumps(marker))
        assert await admin.fetchval(
            "SELECT has_function_privilege('oracle_app_login', "
            "'privacy_redact_execute_jobs(uuid,uuid[],uuid[],jsonb)', 'EXECUTE')") is False
        # The owner check cannot be shadowed by a temp pg_class: the trigger's search_path is pinned.
        assert await admin.fetchval(
            "SELECT proconfig FROM pg_catalog.pg_proc WHERE oid = 'public.ai_decision_traces_immutable()'::regprocedure"
        ) == ["search_path=pg_catalog, pg_temp"]
        # Even the table owner may write only the exact marker, and only inside a privacy operation.
        table_owner = await admin.fetchval(
            "SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = 'public.ai_decision_traces'::regclass")

        async def as_owner(value, operation):
            await admin.execute(f'SET LOCAL ROLE "{table_owner}"')
            await admin.execute("SELECT set_config('app.privacy_operation', $1, true)", operation)
            await admin.execute("UPDATE ai_decision_traces SET proposal=$2::jsonb WHERE id=$1", t_y, json.dumps(value))

        for value, operation in (({**marker, "body": "FABRICATED"}, "op"),      # marker plus another key
                                 ({"body": "FABRICATED"}, "op"),                 # other content
                                 ({"redacted": "data_subject_erasure", "at": 1}, "op"),  # 'at' not a time
                                 (marker, "")):                                  # outside a privacy operation
            async with admin.transaction():
                with pytest.raises(asyncpg.PostgresError, match="append-only"):
                    await as_owner(value, operation)

        class RolledBack(Exception):
            pass

        with pytest.raises(RolledBack):   # the one accepted rewrite, then undone
            async with admin.transaction():
                await as_owner(marker, "op")
                assert _j(await admin.fetchval("SELECT proposal FROM ai_decision_traces WHERE id=$1", t_y)) == marker
                raise RolledBack

        # SQL guards: another brokerage's ids under this brokerage's deletion touch nothing;
        # only execute jobs; another tenant's context is refused; a legal hold refuses.
        async with tenant_tx(octx) as conn:
            op = await pl._create_operation(conn, tenant_id=T, kind="dsr_delete", requested_by=owner,
                                            subject_kind="contact", subject_ref=f"ref-{s}", state="running")
            for table, ids in (("command_executions", [cmd_o]), ("action_approvals", [a_o]),
                               ("automation_jobs", [j_o, j_other])):
                assert await conn.fetchval("SELECT privacy_erase_subject_rows($1,$2,$3::uuid[])",
                                           op["id"], table, ids) == 0, table
        async with tenant_tx(TenantContext(agent_id=owner, tenant_id=O, role=Role.BROKER_OWNER)) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval("SELECT privacy_erase_subject_rows($1,'command_executions',$2::uuid[])",
                                    op["id"], [cmd_y])
        await admin.execute("INSERT INTO legal_holds (tenant_id, scope, subject_ref, reason, placed_by) "
                            "VALUES ($1,'contact',$2,'litigation','counsel')", T, f"ref-{s}")
        async with tenant_tx(octx) as conn:
            with pytest.raises(asyncpg.PostgresError) as held:
                await conn.fetchval("SELECT privacy_erase_subject_rows($1,'command_executions',$2::uuid[])",
                                    op["id"], [cmd_yx])
        assert held.value.sqlstate == "P0001"
        await admin.execute("UPDATE legal_holds SET released_at=now(), released_by='counsel' WHERE tenant_id=$1", T)

        # Someone else's rows, in this brokerage and the other, are byte-for-byte what they were.
        assert {t: await rows(t, ids) for t, ids in untouched.items()} == before_other
        assert {j: await attempts(j) for j in (j_y, j_o)} == before_attempts

        # A restore that brings a draft's content back: re-applied, redacted again.
        await admin.execute(
            "UPDATE command_executions SET target=$2::jsonb, draft=$3::jsonb WHERE id=$1", cmd1,
            json.dumps(_j(before["command_executions"][cmd1]["target"])),
            json.dumps(_j(before["command_executions"][cmd1]["draft"])))
        key = done["restore_directive"]
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=False) if e.get("directive") == key]
        assert mine[0]["status"] == "resurrected" and mine[0]["rows_back"] == {"command_executions": 1}
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=True) if e.get("directive") == key]
        assert mine[0]["status"] == "reapplied" and mine[0]["redacted"] == {"command_executions": 1}
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
                await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = ANY($1::text[])', [T, O])
            await admin.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [T, O])
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
