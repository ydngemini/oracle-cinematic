"""The customer-data lifecycle, end to end, against real PostgreSQL.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (a superuser DSN used only to
seed and to verify past RLS); the code under test connects as the real
oracle_app_login / oracle_platform_login, so every RLS policy, grant and
SECURITY DEFINER guard is live. Seeds two synthetic brokerages tagged with a
random sentinel, runs offboarding → subject requests → export → closure →
withdrawal → closure → erasure on the first, and proves the second was never
touched. Providers (Twilio, Google, Stripe) are fakes that record calls.

    ORACLE_LIVE_DB_ADMIN_DSN=postgresql://postgres:…@db/oracle \\
    ORACLE_DB_HOST=db ORACLE_DB_PASSWORD=… ORACLE_DB_PLATFORM_PASSWORD=… \\
    ORACLE_STORAGE_BACKEND=local ORACLE_MEDIA_ROOT=/tmp/neoh-media \\
    pytest tests/test_privacy_lifecycle_live.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import uuid
import zipfile

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "f" * 64


def test_full_lifecycle_against_real_postgres(monkeypatch, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="oracle.privacy.events")
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    monkeypatch.setenv("ORACLE_CLOSURE_GRACE_DAYS", "30")
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    asyncio.run(_scenario(monkeypatch, tmp_path))
    events = [json.loads(r.getMessage())["event"] for r in caplog.records if r.name == "oracle.privacy.events"]
    for expected in ("offboarding.completed", "export.requested", "export.completed",
                     "deletion.scheduled", "deletion.started", "deletion.completed"):
        assert expected in events, (expected, events)
    # telemetry carries no customer content
    assert not any("SENTINEL" in r.getMessage() or "@" in r.getMessage()
                   for r in caplog.records if r.name == "oracle.privacy.events")


async def _scenario(monkeypatch, media_root):
    import asyncpg

    import commands_api
    import privacy_export
    import privacy_lifecycle as pl
    import privacy_requests
    import voice_provider
    from command_providers import ProviderResult
    from db import connection
    from outreach_compliance import guard_outreach
    from tenancy import Role, TenantContext

    sentinel = "SENTINEL-" + secrets.token_hex(6)
    T, U = str(uuid.uuid4()), str(uuid.uuid4())
    owner, agent_a, agent_b = (f"owner-{sentinel}@live.test".lower(), f"Agent-A-{sentinel}@live.test",
                               f"agent-b-{sentinel}@live.test".lower())
    calls: dict[str, list] = {"release": [], "revoke": []}

    class FakeVoice:
        async def release_forwarding_number(self, sid, *, credentials=None):
            calls["release"].append(sid)
            return ProviderResult("twilio_number", sid, "released", {})

    async def fake_revoke(token):
        calls["revoke"].append(token)
        return "revoked"

    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda name: FakeVoice())
    monkeypatch.setattr(commands_api, "revoke_google_token", fake_revoke)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        ids = await _seed(admin, T, U, sentinel, owner, agent_a, agent_b, media_root)
        before_u = await _tenant_rowcount(admin, U)
        assert before_u > 0

        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        # ── 1. Offboarding preview changes nothing ────────────────────────
        preview = await pl.offboard_agent(octx, departing_agent_id=agent_a.lower(),  # case-folded on purpose
                                          successor_agent_id=agent_b, reason="leaving", preview=True)
        assert preview["preview"] is True
        assert preview["reassign"]["agent_contacts"] == 1 and preview["reassign"]["clients"] == 2
        assert preview["cancel_pending"]["command_executions"] == 1
        assert await admin.fetchval("SELECT is_active FROM users WHERE id=$1", ids["a"]) is True
        assert await admin.fetchval("SELECT assignee_id FROM clients WHERE id=$1", ids["c1"]) == agent_a

        # ── 2. Offboarding for real ───────────────────────────────────────
        done = await pl.offboard_agent(octx, departing_agent_id=agent_a, successor_agent_id=agent_b,
                                       reason="leaving", preview=False)
        assert done["telephony"] == "moved_to_successor"
        a_row = await admin.fetchrow("SELECT is_active, session_epoch FROM users WHERE id=$1", ids["a"])
        assert a_row["is_active"] is False and a_row["session_epoch"] == 1
        assert await admin.fetchval("SELECT assignee_id FROM clients WHERE id=$1", ids["c1"]) == agent_b
        assert await admin.fetchval("SELECT assigned_agent_id FROM agent_contacts WHERE id=$1", ids["k1"]) == agent_b
        assert await admin.fetchval("SELECT assignee_id FROM client_tasks WHERE id=$1", ids["task"]) == agent_b
        # authorship is history: the note still says A wrote it
        assert await admin.fetchval("SELECT author_id FROM client_notes WHERE id=$1", ids["note"]) == agent_a
        route = await admin.fetchrow("SELECT agent_id, agent_forward_e164 FROM telephony_routes WHERE id=$1", ids["route"])
        assert route["agent_id"] == agent_b and route["agent_forward_e164"] is None
        assert await admin.fetchval("SELECT agent_id FROM messaging_routes WHERE tenant_id=$1", T) == agent_b
        assert await admin.fetchval("SELECT state FROM command_executions WHERE id=$1", ids["cmd"]) == "cancelled"
        assert await admin.fetchval("SELECT state FROM automation_jobs WHERE id=$1", ids["job"]) == "cancelled"
        assert await admin.fetchval("SELECT count(*) FROM provider_credentials WHERE id=$1", ids["cred_a"]) == 0
        assert done["credential_revocation"][0]["status"] == "revoked"   # A's Google grant revoked at Google
        assert await admin.fetchval(
            "SELECT accepting_leads FROM agent_routing_state WHERE tenant_id=$1 AND agent_id=$2", T, agent_a) is False

        # the last owner cannot be offboarded / suspended away
        with pytest.raises(pl.LifecycleError):
            await pl.offboard_agent(octx, departing_agent_id=owner, successor_agent_id=agent_b,
                                    reason="x", preview=True)

        # ── 3. Subject requests ───────────────────────────────────────────
        found = await privacy_requests.handle_subject_request(
            octx, kind="dsr_access", email=f"c2-{sentinel}@client.test".upper(), phone=None,
            reason="subject asked", preview=False)
        assert found["matches"]["clients"] == 1
        assert sentinel in json.dumps(found["records"])
        # a person on a transaction is retained, not deleted
        with pytest.raises(pl.LifecycleError, match="transaction"):
            await privacy_requests.handle_subject_request(
                octx, kind="dsr_delete", email=f"c1-{sentinel}@client.test", phone=None,
                reason="subject asked", preview=False)
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id=$1", ids["c1"]) == 1
        deleted = await privacy_requests.handle_subject_request(
            octx, kind="dsr_delete", email=f"c2-{sentinel}@client.test", phone="(302) 555-0142",
            reason="subject asked", preview=False)
        assert deleted["deleted"]["clients"] == 1
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id=$1", ids["c2"]) == 0
        assert await admin.fetchval("SELECT count(*) FROM sms_messages WHERE id=$1", ids["sms2"]) == 0
        # …and they stay un-contactable: the opt-out outlived the record
        decision = await guard_outreach(octx, contact="+13025550142", channel="sms", state_code="DE", log=False)
        assert not decision.allowed and "opt-out" in " ".join(decision.blockers).lower()

        # ── 4. Export ─────────────────────────────────────────────────────
        export = await privacy_export.request_export(octx)
        built = await privacy_export.build_export(export["operation_id"])
        assert built["state"] == "succeeded"
        key = await admin.fetchval("SELECT artifact_key FROM privacy_operations WHERE id=$1", export["operation_id"])
        archive = zipfile.ZipFile(io.BytesIO((media_root / key).read_bytes()))
        names = set(archive.namelist())
        assert "manifest.json" in names and "tables/clients.jsonl" in names
        assert not any("provider_credentials" in n for n in names)
        blob = b"".join(archive.read(n) for n in names if n.endswith(".jsonl")).decode()
        assert sentinel in blob
        assert "password_hash" not in blob and "scrypt$" not in blob
        assert f"k1-{sentinel}" in blob                     # encrypted contact decrypted into the export
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["files"]["tables/clients.jsonl"]["rows"] == 1
        assert "users" in manifest["omitted_columns"] and "password_hash" in manifest["omitted_columns"]["users"]
        assert manifest["media"]["included"] == 1
        # The other tenant's sentinel never appears.
        other = await admin.fetchval("SELECT full_name FROM clients WHERE tenant_id=$1 LIMIT 1", U)
        assert other not in blob

        # ── 5. Closure, withdrawal, closure ───────────────────────────────
        with pytest.raises(pl.LifecycleError, match="exactly"):
            await pl.request_closure(octx, confirm_name="wrong", reason="closing")
        closing = await pl.request_closure(octx, confirm_name=f"Brokerage {sentinel}", reason="closing")
        assert closing["state"] == "closing"
        assert await admin.fetchval("SELECT is_active FROM users WHERE id=$1", ids["b"]) is False
        assert await admin.fetchval("SELECT is_active FROM users WHERE id=$1", ids["owner"]) is True
        assert await admin.fetchval("SELECT revoked_at IS NOT NULL FROM client_portals WHERE id=$1", ids["portal"])
        assert await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1", ids["route"]) is False
        # queued work was cancelled, and nothing new is claimable for a closing tenant
        assert await admin.fetchval("SELECT state FROM automation_jobs WHERE id=$1", ids["job2"]) == "cancelled"
        late_job = str(uuid.uuid4())
        await admin.execute(
            "INSERT INTO automation_jobs (id, tenant_id, job_type, idempotency_key, created_by, state, scheduled_at) "
            "VALUES ($1::uuid,$2,'live:test',$1::text,'x','queued', now() + interval '1 day')", late_job, T)
        assert await _claimable(admin, late_job) is False
        reopened = await pl.withdraw_closure(octx, reason="changed mind")
        assert reopened["state"] == "active" and reopened["restored_users"] == 1
        assert await admin.fetchval("SELECT active FROM telephony_routes WHERE id=$1", ids["route"]) is True
        assert await _claimable(admin, late_job) is True
        # a record-id subject request finds the same person as their address
        by_id = await privacy_requests.handle_subject_request(
            octx, kind="dsr_access", email=None, phone=None, reason="asked", preview=True,
            contact_id=ids["k1"])
        assert by_id["matches"]["clients"] == 1 and by_id["matches"]["agent_contacts"] == 1

        # dry run before closing: counts only, nothing changes
        dry = await pl.erasure_preview(T)
        assert dry["highlights"]["clients"] == 1 and dry["highlights"]["users"] == 3
        assert dry["stored_objects"] == 1 and dry["retained_after_erasure"]["subscriptions"] == 1
        assert sentinel not in json.dumps(dry)
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE tenant_id=$1", T) == 1
        await pl.request_closure(octx, confirm_name=f"Brokerage {sentinel}", reason="closing for good")

        # ── 6. Legal hold blocks erasure; release; erase ──────────────────
        await admin.execute("INSERT INTO legal_holds (tenant_id, scope, reason, placed_by) "
                            "VALUES ($1,'tenant','litigation','counsel')", T)
        assert await pl.begin_erasure(T, requested_by="test") is None
        assert await admin.fetchval("SELECT lifecycle_state FROM tenants WHERE id=$1", T) == "closing"
        await admin.execute("UPDATE legal_holds SET released_at=now(), released_by='counsel' WHERE tenant_id=$1", T)
        await admin.execute("UPDATE privacy_operations SET state='running' WHERE tenant_id=$1 "
                            "AND kind='closure' AND state='blocked_legal_hold'", T)
        op_id = await pl.begin_erasure(T, requested_by="test")
        assert op_id
        result = await pl.run_erasure(op_id)
        op = await admin.fetchrow("SELECT state, receipt, error FROM privacy_operations WHERE id=$1", op_id)
        receipt = json.loads(op["receipt"])
        assert op["state"] == "succeeded", (op["error"], receipt.get("verification"))
        assert receipt["verification"]["clean"] is True
        assert calls["release"] == ["PNlive0000000000000000000000000000"]
        assert len(calls["revoke"]) == 2                        # A's (offboarding) + the owner's (erasure)

        # ── 7. Verify past RLS ────────────────────────────────────────────
        retained = set(await admin.fetchval("SELECT privacy_retained_tables()"))
        leftovers = {}
        for table, ttype in await _tenant_tables(admin):
            if table in retained or table.startswith("zz_"):
                continue
            n = await _count(admin, table, ttype, T)
            if n:
                leftovers[table] = n
        assert not leftovers, leftovers
        tenant = await admin.fetchrow("SELECT name, lifecycle_state, website FROM tenants WHERE id=$1", T)
        assert tenant["name"] == "Erased brokerage" and tenant["lifecycle_state"] == "erased"
        assert sentinel not in json.dumps(dict(tenant))
        assert await admin.fetchval("SELECT count(*) FROM subscriptions WHERE tenant_id=$1", T) == 1  # retained
        assert await admin.fetchval("SELECT count(*) FROM suppression_tombstones WHERE tenant_id=$1", T) >= 1
        assert not (media_root / ids["media_key"]).exists()
        assert await admin.fetchval("SELECT count(*) FROM erasure_ledger WHERE operation_id=$1", op_id) > 5
        assert (media_root / f"privacy/erasure-directives/{op_id}.json").exists()
        assert "backups" in receipt and "7-day" in receipt["backups"]
        actor = await admin.fetchval("SELECT actor_id FROM audit_anomaly_alerts WHERE tenant_id=$1", T)
        assert actor.startswith("erased:")
        # resuming a finished erasure is a no-op
        again = await pl.run_erasure(op_id)
        assert again["state"] == "succeeded"

        # ── 7b. A restore from an older backup brings the data back… ─────
        await admin.execute("SET session_replication_role = replica")   # a restore bypasses triggers
        await admin.execute("UPDATE tenants SET lifecycle_state='active', name=$2 WHERE id=$1", T, f"Brokerage {sentinel}")
        await admin.execute("INSERT INTO clients (tenant_id, full_name) VALUES ($1,$2)", T, f"restored {sentinel}")
        await admin.execute("SET session_replication_role = origin")
        report = await pl.reapply_erasures(apply=False)
        mine = [e for e in report if e.get("tenant_id") == T]
        assert mine and mine[0]["status"] == "resurrected"
        # …and the directive erases it again.
        report = await pl.reapply_erasures(apply=True)
        mine = [e for e in report if e.get("tenant_id") == T]
        assert mine[0]["status"] == "reapplied", mine
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE tenant_id=$1", T) == 0
        assert await admin.fetchval("SELECT lifecycle_state FROM tenants WHERE id=$1", T) == "erased"
        # ── 7c. A terminated MLS licence purges that feed ────────────────
        feed = f"live-{sentinel}".lower()
        for i in range(3):
            await admin.execute(
                "INSERT INTO oracle_mls_listings (mls_id, mls_number, address, state_code, list_price) "
                "VALUES ($1,$2,'1 Test St','TX',1)", feed, f"{i}")
        await admin.execute("INSERT INTO mls_feed_entitlements (tenant_id, mls_id, granted_by) VALUES ($1,$2,'op')",
                            U, feed)
        plan = await pl.purge_mls_feed(feed, requested_by="test", reason="licence ended", preview=True)
        assert plan["listings"] == 3 and plan["brokerages_still_entitled"] == 1
        with pytest.raises(pl.LifecycleError, match="entitlement"):
            await pl.purge_mls_feed(feed, requested_by="test", reason="licence ended", preview=False)
        assert await admin.fetchval("SELECT count(*) FROM oracle_mls_listings WHERE mls_id=$1", feed) == 3
        await admin.execute("DELETE FROM mls_feed_entitlements WHERE mls_id=$1", feed)
        purged = await pl.purge_mls_feed(feed, requested_by="test", reason="licence ended", preview=False)
        assert purged["deleted"] == 3
        assert await admin.fetchval("SELECT count(*) FROM oracle_mls_listings WHERE mls_id=$1", feed) == 0
        await admin.execute("DELETE FROM privacy_operations WHERE id=$1", purged["operation_id"])
        await admin.execute("DELETE FROM erasure_ledger WHERE operation_id=$1", purged["operation_id"])

        # ── 8. The other brokerage was never touched ──────────────────────
        assert await _tenant_rowcount(admin, U) == before_u
    finally:
        await _cleanup(admin, T, U)
        await admin.close()
        await connection.close_pool()


async def _claimable(admin, job_id) -> bool:
    return bool(await admin.fetchval(
        """
        SELECT 1 FROM automation_jobs WHERE id=$1 AND state='queued'
           AND (job_type LIKE 'privacy:%' OR NOT EXISTS (
               SELECT 1 FROM tenants t WHERE t.id = automation_jobs.tenant_id AND t.lifecycle_state <> 'active'))
        """, job_id))


async def _tenant_tables(admin) -> list[tuple[str, str]]:
    """(table, tenant_id type). Compare as the column's own type: a ::text cast
    on a uuid column defeats the tenant index and seq-scans 10M leads."""
    return [(r[0], r[1]) for r in await admin.fetch(
        "SELECT c.relname, format_type(a.atttypid, a.atttypmod) FROM pg_class c "
        "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='tenant_id' "
        "AND NOT a.attisdropped WHERE c.relkind='r' AND c.relnamespace='public'::regnamespace")]


async def _count(admin, table, ttype, tenant_id) -> int:
    return await admin.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id = $1::{ttype}", str(tenant_id))


async def _tenant_rowcount(admin, tenant_id) -> int:
    total = 0
    for table, ttype in await _tenant_tables(admin):
        total += await _count(admin, table, ttype, tenant_id)
    return total


async def _seed(admin, T, U, s, owner, agent_a, agent_b, media_root) -> dict:
    from crypto import derive_tenant_key

    key_t = derive_tenant_key(T, MASTER)
    ids = {k: str(uuid.uuid4()) for k in (
        "owner", "a", "b", "c1", "c2", "k1", "note", "task", "route", "cmd", "job", "job2", "cred_a",
        "cred_o", "plan", "rev", "sms2", "media", "portal", "lead", "txn", "uo", "uc")}
    ids["media_key"] = f"property-media/{T}/photo/{secrets.token_hex(16)}"
    (media_root / ids["media_key"]).parent.mkdir(parents=True, exist_ok=True)
    (media_root / ids["media_key"]).write_bytes(b"\xff\xd8 photo " + s.encode())
    async with admin.transaction():
        await admin.execute("INSERT INTO tenants (id, slug, name, website) VALUES ($1,$2,$3,$4), ($5,$6,$7,NULL)",
                            T, f"live-{s}".lower(), f"Brokerage {s}", f"https://{s}.test".lower(),
                            U, f"other-{s}".lower(), f"Other {s}")
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) VALUES "
            "($1,$4,$5,'broker_owner',$5,'scrypt$x$y'), ($2,$4,$6,'agent',$6,'scrypt$x$y'), "
            "($3,$4,$7,'agent',$7,'scrypt$x$y'), ($8,$9,$10,'broker_owner',$10,'scrypt$x$y')",
            ids["owner"], ids["a"], ids["b"], T, owner, agent_a, agent_b, ids["uo"], U, f"uo-{s}@live.test".lower())
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, email, phone, assignee_id) VALUES "
            "($1,$4,$5,$6,'302-555-0141',$7), ($2,$4,$8,$9,'302-555-0142',$7), ($3,$10,$11,NULL,NULL,NULL)",
            ids["c1"], ids["c2"], ids["uc"], T, f"C1 {s}", f"c1-{s}@client.test", agent_a,
            f"C2 {s}", f"c2-{s}@client.test", U, f"U-client {s}")
        await admin.execute(
            "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, legacy_client_id, pii_ciphertext) "
            "VALUES ($1,$2,$3,$4, pgp_sym_encrypt($5, $6))",
            ids["k1"], T, agent_a, ids["c1"], json.dumps({"full_name": f"k1-{s}"}), key_t)
        await admin.execute("UPDATE clients SET contact_id=$1 WHERE id=$2", ids["k1"], ids["c1"])
        await admin.execute("INSERT INTO client_notes (id, tenant_id, client_id, body, author_id) VALUES ($1,$2,$3,$4,$5)",
                            ids["note"], T, ids["c1"], f"note {s}", agent_a)
        await admin.execute("INSERT INTO client_tasks (id, tenant_id, client_id, title, assignee_id, created_by, status) "
                            "VALUES ($1,$2,$3,'call back',$4,$4,'open')", ids["task"], T, ids["c1"], agent_a)
        await admin.execute(
            "INSERT INTO telephony_routes (id, tenant_id, agent_id, inbound_did, agent_forward_e164, active, "
            "inbound_forwarding_provider_sid, provider, twilio_account_sid, provider_account_id) "
            "VALUES ($1,$2,$3,$4,'+13025550188',true,'PNlive0000000000000000000000000000','twilio',$5,$6)",
            ids["route"], T, agent_a, "+1302" + str(secrets.randbelow(10**7)).zfill(7), "AC" + "0" * 32, "AC" + "0" * 32)
        await admin.execute("INSERT INTO messaging_routes (tenant_id, agent_id, active) VALUES ($1,$2,true)", T, agent_a)
        pkey = derive_tenant_key(T, MASTER)
        await admin.execute(
            "INSERT INTO provider_credentials (id, tenant_id, provider, account_label, token_ciphertext, "
            "refresh_ciphertext, created_by) VALUES "
            "($1,$3,'google',$4, pgp_sym_encrypt('access-a',$6), pgp_sym_encrypt('refresh-a',$6), $4), "
            "($2,$3,'google','default', pgp_sym_encrypt('access-o',$6), pgp_sym_encrypt('refresh-o',$6), $5)",
            ids["cred_a"], ids["cred_o"], T, agent_a, owner, pkey)
        await admin.execute(
            "INSERT INTO command_executions (id, tenant_id, command_type, classification, risk_class, target, draft, "
            "state, idempotency_key, created_by) VALUES ($1,$2,'EMAIL','external','outreach',"
            "'{}'::jsonb,'{}'::jsonb,'awaiting_approval',$3,$4)", ids["cmd"], T, f"live-{s}", agent_a)
        for job, by in ((ids["job"], agent_a), (ids["job2"], owner)):
            await admin.execute(
                "INSERT INTO automation_jobs (id, tenant_id, job_type, idempotency_key, created_by, state, scheduled_at) "
                "VALUES ($1,$2,'live:test',$3,$4,'queued', now() + interval '1 day')", job, T, f"{job}", by)
        await admin.execute("INSERT INTO smart_plans (id, tenant_id, owner_agent_id, created_by, name) VALUES ($1,$2,$3,$3,'p')",
                            ids["plan"], T, agent_a)
        await admin.execute(
            "INSERT INTO smart_plan_revisions (id, tenant_id, plan_id, revision_number, definition, definition_hash, created_by) "
            "VALUES ($1,$2,$3,1,'{}'::jsonb,repeat('0',64),$4)", ids["rev"], T, ids["plan"], agent_a)
        await admin.execute("UPDATE smart_plans SET current_revision_id=$1 WHERE id=$2", ids["rev"], ids["plan"])
        await admin.execute("INSERT INTO outreach_suppression (tenant_id, contact, channel, reason) VALUES "
                            "($1,'+13025550141','*','stop_keyword')", T)
        await admin.execute(
            "INSERT INTO sms_messages (id, tenant_id, agent_id, provider, provider_message_id, direction, from_e164, to_e164, body) "
            "VALUES ($1,$2,$3,'telnyx',$4,'inbound','+13025550142','+13025550199',$5)",
            ids["sms2"], T, agent_a, f"msg-{s}", f"hi {s}")
        await admin.execute("INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score) VALUES ($1,$2,$3,'DE',1)",
                            ids["lead"], T, f"P-{s}")
        await admin.execute("INSERT INTO property_media (id, tenant_id, lead_id, url, s3_key) VALUES ($1,$2,$3,'x',$4)",
                            ids["media"], T, ids["lead"], ids["media_key"])
        await admin.execute(
            "INSERT INTO client_portals (id, tenant_id, lead_id, token_hash, access_expires_at) "
            "VALUES ($1,$2,$3,$4, now() + interval '7 days')", ids["portal"], T, ids["lead"], secrets.token_hex(32))
        await admin.execute("INSERT INTO transactions (id, tenant_id, client_id, client_party_role, state_code) VALUES ($1,$2,$3,'seller','DE')", ids["txn"], T, ids["c1"])
        await admin.execute("INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status) "
                            "VALUES ($1,'cus_live','sub_live','canceled')", T)
        await admin.execute(
            "INSERT INTO audit_anomaly_alerts (tenant_id, fingerprint, severity, anomaly_type, evidence, actor_id) "
            "VALUES ($1, repeat('a',64), 'low', 'test', '{}'::jsonb, $2)", T, agent_a)
    return ids


async def _cleanup(admin, T, U) -> None:
    """Remove the synthetic tenants entirely (superuser, test data only)."""
    for tenant in (T, U):
        await admin.execute("SET session_replication_role = replica")
        try:
            for table, ttype in await _tenant_tables(admin):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::{ttype}", tenant)
            for table in ("erasure_ledger", "privacy_operations", "legal_holds", "suppression_tombstones"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id=$1", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
