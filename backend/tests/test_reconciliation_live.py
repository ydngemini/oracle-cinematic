"""The reconciliation sweep against real PostgreSQL (skipped without
ORACLE_LIVE_DB_ADMIN_DSN — see test_privacy_lifecycle_live.py for how it runs).

Seeds one synthetic brokerage with every kind of stuck or ambiguous state and
proves each is resolved — or left visibly for a human — and that nothing
belonging to another tenant moves. Providers are fakes."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_sweep_resolves_every_stuck_state(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import reconciliation
    from db import connection

    T = str(uuid.uuid4())
    ids = {k: str(uuid.uuid4()) for k in ("client", "contact", "cmd_exec", "cmd_ref_ok", "cmd_ref_never",
                                         "cmd_noref", "chat", "recon", "intent", "purchase", "outbox", "lead",
                                         "sending_lost", "sending_fresh", "sending_known",
                                         "orphan_turn", "orphan_user")}
    asked = []

    async def fake_ask(row):
        asked.append(str(row["id"]))
        return {"CAok": "happened", "CAnever": "never"}.get(row["provider_reference"])

    async def fake_find(tctx, since, used):
        return ("PNadopted", "+13025550111")

    monkeypatch.setattr(reconciliation, "_ask_provider", fake_ask)
    monkeypatch.setattr(reconciliation, "_find_unclaimed_twilio_number", fake_find)
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Recon test')", T, f"recon-{T[:8]}")
            await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'C')", ids["client"], T)
            await admin.execute("INSERT INTO agent_contacts (id, tenant_id) VALUES ($1,$2)", ids["contact"], T)
            old = "now() - interval '2 hours'"
            for key, state, ref in (("cmd_exec", "executing", None), ("cmd_ref_ok", "reconciliation_required", "CAok"),
                                    ("cmd_ref_never", "reconciliation_required", "CAnever"),
                                    ("cmd_noref", "reconciliation_required", None)):
                await admin.execute(
                    f"INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, "
                    f"target, draft, state, idempotency_key, created_by, provider, provider_reference, updated_at) "
                    f"VALUES ($1::uuid,$2,'CALL','external','live_call','{{}}','{{}}',$3,$1::text,'a@b.test','twilio',$4,{old})",
                    ids[key], T, state, ref)
            await admin.execute(
                f"INSERT INTO ai_chat_messages (id, tenant_id, user_id, role, content_ciphertext, request_id, status, updated_at) "
                f"VALUES ($1,$2,'a@b.test','assistant','\\x00',$3,'pending',{old})", ids["chat"], T, str(uuid.uuid4()))
            # RUN-01: committed 'pending' 5 minutes ago, and its job was never
            # enqueued (the process died in between).
            orphan_agent, orphan_request = f"orphan-{T[:8]}@b.test", str(uuid.uuid4())
            await admin.execute("INSERT INTO users (id, tenant_id, agent_id, role) VALUES ($1,$2,$3,'agent')",
                                ids["orphan_user"], T, orphan_agent)
            await admin.execute(
                "INSERT INTO ai_chat_messages (id, tenant_id, user_id, role, content_ciphertext, request_id, status, "
                "created_at, updated_at) VALUES ($1,$2,$3,'assistant','\\x00',$4,'pending', "
                "now() - interval '5 minutes', now() - interval '5 minutes')",
                ids["orphan_turn"], T, orphan_agent, orphan_request)
            await admin.execute("INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score) "
                                "VALUES ($1,$2,$3,'DE',1)", ids["lead"], T, f"P-{T[:8]}")
            await admin.execute(
                "INSERT INTO reconstruction_jobs (id, tenant_id, lead_id, status, updated_at) "
                "VALUES ($1,$2,$3,'running', now() - interval '30 hours')", ids["recon"], T, ids["lead"])
            await admin.execute(
                "INSERT INTO agent_call_intents (id, tenant_id, agent_id, contact_id, state, created_at, expires_at) "
                "VALUES ($1,$2,'a@b.test',$3,'prepared', now() - interval '20 minutes', now() - interval '1 minute')", ids["intent"], T, ids["contact"])
            await admin.execute(
                "INSERT INTO provider_purchases (id, tenant_id, agent_id, provider, kind, state, created_at) "
                "VALUES ($1,$2,'a@b.test','twilio','phone_number','intended', now() - interval '20 minutes')",
                ids["purchase"], T)
            await admin.execute(
                f"INSERT INTO email_outbox (id, tenant_id, client_id, to_email, subject, body_text, status, updated_at) "
                f"VALUES ($1,$2,$3,'c@client.test','s','b','queued',{old})", ids["outbox"], T, ids["client"])
            # RUN-04: claimed then lost (no marker), claimed just now, and an
            # unknown already recorded by deliver().
            for key, error, age in (("sending_lost", None, old), ("sending_fresh", None, "now()"),
                                    ("sending_known", "delivery_unknown: timeout", old)):
                await admin.execute(
                    f"INSERT INTO email_outbox (id, tenant_id, client_id, to_email, subject, body_text, status, "
                    f"error, updated_at) VALUES ($1,$2,$3,'c@client.test','s','b','sending',$4,{age})",
                    ids[key], T, ids["client"], error)
        before_other = await admin.fetchval(
            "SELECT count(*) FROM command_executions WHERE tenant_id <> $1 AND state='reconciliation_required'", T)
        other_jobs = await admin.fetchval(
            "SELECT count(*) FROM automation_jobs WHERE tenant_id <> $1 AND job_type='email:outbox'", T)

        out = await reconciliation.run_sweep(only_tenant=T)

        state = lambda t, k: admin.fetchval(f"SELECT {'state' if t in ('command_executions','agent_call_intents','provider_purchases') else 'status'} FROM {t} WHERE id=$1", ids[k])  # noqa: E731
        assert out["executing_to_reconcile"] >= 1
        assert await state("command_executions", "cmd_exec") == "reconciliation_required"
        assert await state("command_executions", "cmd_ref_ok") == "succeeded"
        assert await state("command_executions", "cmd_ref_never") == "failed"
        # no provider id: still unknown, never guessed — and counted, bounded
        noref = await admin.fetchrow("SELECT state, reconcile_attempts, reconciliation_reason FROM command_executions WHERE id=$1", ids["cmd_noref"])
        assert noref["state"] == "reconciliation_required" and noref["reconcile_attempts"] == 1
        assert "unknown" in noref["reconciliation_reason"]
        assert await state("ai_chat_messages", "chat") == "failed"
        recon = await admin.fetchrow("SELECT status, error FROM reconstruction_jobs WHERE id=$1", ids["recon"])
        assert recon["status"] == "failed" and "originals" not in recon["error"] and "kept" in recon["error"]
        assert await state("agent_call_intents", "intent") == "expired"
        # intended → unknown in the first pass; the provider check then adopts the number
        purchase = await admin.fetchrow("SELECT state, provider_ref FROM provider_purchases WHERE id=$1", ids["purchase"])
        assert purchase["state"] in ("unknown", "confirmed")
        out2 = await reconciliation.run_sweep(only_tenant=T)
        purchase = await admin.fetchrow("SELECT state, provider_ref FROM provider_purchases WHERE id=$1", ids["purchase"])
        assert purchase["state"] == "confirmed" and purchase["provider_ref"] == "PNadopted", (out2, dict(purchase))
        # the stale queued email got a delivery job
        assert await admin.fetchval(
            "SELECT count(*) FROM automation_jobs WHERE tenant_id=$1 AND job_type='email:outbox' "
            "AND payload->>'outbox_id'=$2", T, ids["outbox"]) == 1
        # RUN-04: the lost 'sending' row is now visibly unknown, still 'sending'
        # and never re-enqueued; the fresh one and the known one are untouched.
        mail = {k: await admin.fetchrow("SELECT status, error FROM email_outbox WHERE id=$1", ids[k])
                for k in ("sending_lost", "sending_fresh", "sending_known")}
        assert out["emails_sending_to_unknown"] == 1, out
        assert mail["sending_lost"]["status"] == "sending"
        assert mail["sending_lost"]["error"].startswith("delivery_unknown: still sending")
        assert mail["sending_fresh"]["error"] is None
        assert mail["sending_known"]["error"] == "delivery_unknown: timeout"
        assert await admin.fetchval(
            "SELECT count(*) FROM automation_jobs WHERE tenant_id=$1 AND job_type='email:outbox' "
            "AND payload->>'outbox_id' = ANY($2::text[])", T,
            [ids["sending_lost"], ids["sending_fresh"], ids["sending_known"]]) == 0, "never resent"
        # RUN-01: the orphaned turn got its job, as its own user and role; the
        # two-hour-old turn was failed, not re-queued.
        job = await admin.fetchrow(
            "SELECT job_type, payload, queue_name FROM automation_jobs WHERE tenant_id=$1 AND idempotency_key=$2",
            T, f"ai-chat:{orphan_agent}:{orphan_request}")
        assert job is not None and job["job_type"] == "ai_chat:response", out
        payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
        assert payload["assistant_id"] == ids["orphan_turn"] and payload["role"] == "agent"
        assert out["chat_turns_requeued"] >= 1
        assert await admin.fetchval(
            "SELECT count(*) FROM automation_jobs WHERE tenant_id=$1 AND payload->>'assistant_id'=$2",
            T, ids["chat"]) == 0
        # bounded: a row is not re-asked within 10 minutes
        asked.clear()
        await reconciliation.run_sweep(only_tenant=T)
        assert ids["cmd_noref"] not in asked
        # other tenants untouched — including their queued emails
        assert await admin.fetchval(
            "SELECT count(*) FROM automation_jobs WHERE tenant_id <> $1 AND job_type='email:outbox'", T) == other_jobs
        assert await admin.fetchval(
            "SELECT count(*) FROM command_executions WHERE tenant_id <> $1 AND state='reconciliation_required'",
            T) == before_other
    finally:
        await admin.execute("SET session_replication_role = replica")
        for table in ("automation_jobs", "email_outbox", "provider_purchases", "agent_call_intents",
                      "reconstruction_jobs", "ai_chat_messages", "command_executions", "agent_contacts", "clients",
                      "leads", "users"):
            await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", T)
        await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
