"""Message bodies, call transcript excerpts and chat turns are sealed at rest,
against real PostgreSQL with FORCE RLS (audit A01, ledger L17-21; skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Proves, as the application role:
- every writer (outbound + inbound SMS, call telemetry, voice telemetry, chat
  memory) stores no plaintext;
- every reader (voice telemetry REST feed, voice WebSocket hydrate, chat
  memory context + roll-up, subject access bundle, brokerage export) returns
  the text for sealed rows AND for pre-0152 plaintext rows in the same result;
- the previous release's plaintext write still works on the new schema, and a
  row holding both shapes is refused;
- the backfill seals old rows in bounded batches, resumes, is idempotent and
  reverses with --unseal; the append-only ledgers are sealed only through the
  0152 functions, which refuse an agent and another tenant's context;
- retention still purges a sealed transcript.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import uuid
import zipfile

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
MASTER = "live-test-sealed-text-master-key-0123456789"
SLICE_ONE = ("sms", "transcript", "interaction")  # 0152's fields; 0154's: test_sealed_text_two_live.py


def test_communication_text_is_sealed_and_still_read(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 - one ordered scenario
    import asyncpg

    import backfill_sealed_text as backfill
    import commands_api
    import messaging_data
    import privacy_export
    import privacy_requests
    import voice_intel
    from crypto import derive_tenant_key
    from db import connection
    from memory_core.session_manager import SessionManager
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client = str(uuid.uuid4()), str(uuid.uuid4())
    key = derive_tenant_key(tenant, MASTER)
    agent = TenantContext(agent_id="agent-seal-text", tenant_id=tenant, role=Role.AGENT)
    plat = TenantContext(agent_id="plat-seal-text", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    tag = tenant[:8]
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Sealed text test')",
                            tenant, f"sealtext-{tag}")
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-seal-text','agent','agent-seal-text','scrypt$x$y')", uuid.uuid4(), tenant)
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) "
            "VALUES ($1,$2,'Sealed Text Client','agent-seal-text')", client, tenant)
        call = await admin.fetchval(
            """INSERT INTO live_call_sessions (tenant_id, client_id, consent_recorded, consent_basis,
                                               transcript_status, started_at, created_by)
               VALUES ($1,$2,true,'verbal','active',now(),'agent-seal-text') RETURNING id::text""",
            tenant, client)

        # ── writers store no plaintext ─────────────────────────────────────
        await messaging_data.record_outbound_message(
            tenant_id=tenant, agent_id="agent-seal-text", contact_id=None, client_id=client,
            provider="telnyx", provider_message_id=f"out-{tag}", from_e164="+15550001001",
            to_e164="+15550001002", body="Gate code is 4411")
        inbound_id = await messaging_data.record_inbound_message(
            tenant_id=tenant, agent_id="agent-seal-text", provider="telnyx",
            provider_message_id=f"in-{tag}", from_e164="+15550001002", to_e164="+15550001001",
            text="Call me after five", media=[], opted_out=False)
        sms = await admin.fetch(
            "SELECT id, body, pgp_sym_decrypt(body_ciphertext, $2) AS opened FROM sms_messages "
            "WHERE tenant_id=$1 ORDER BY provider_message_id", tenant, key)
        assert [(r["body"], r["opened"]) for r in sms] == [
            (None, "Call me after five"), (None, "Gate code is 4411")]

        await commands_api.call_telemetry(
            call, commands_api.CallTelemetry(transcript_excerpt="We would take two fifty.",
                                             counter_offer=250000, arv=410000, rehab=40000), agent)
        await voice_intel.ingest_voice_telemetry(
            voice_intel.VoiceTelemetryRequest(session_id=call, transcript_chunk="Roof is new.",
                                              speaker="CLIENT"), agent)
        events = await admin.fetch(
            "SELECT transcript_excerpt, pgp_sym_decrypt(transcript_ciphertext, $2) AS opened "
            "FROM negotiation_events WHERE call_session_id=$1 ORDER BY id", uuid.UUID(call), key)
        assert [(r["transcript_excerpt"], r["opened"]) for r in events] == [
            (None, "We would take two fifty."), (None, "Roof is new.")]

        memory = SessionManager(agent)
        await memory.record_interaction("agent-seal-text", "user", "Seller is in probate")
        turns = await admin.fetch(
            "SELECT content, pgp_sym_decrypt(content_ciphertext, $2) AS opened FROM user_interactions "
            "WHERE tenant_id=$1", tenant, key)
        assert [(r["content"], r["opened"]) for r in turns] == [("", "Seller is in probate")]

        # ── the previous release's plaintext writes still work (expand) ────
        legacy_sms = await admin.fetchval(
            "INSERT INTO sms_messages (tenant_id, agent_id, client_id, direction, provider, "
            "provider_message_id, status, from_e164, to_e164, body, created_at) "
            "VALUES ($1,'agent-seal-text',$2,'outbound','telnyx',$3,'sent','+15550001001',"
            "'+15550001002','Legacy body', now() - interval '1 day') RETURNING id",
            tenant, client, f"legacy-{tag}")
        await admin.execute(
            "INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, transcript_excerpt, "
            "model_version, created_by, created_at) VALUES ($1,$2,'transcript','Legacy transcript',"
            "'legacy','agent-seal-text', now() - interval '1 hour')", tenant, uuid.UUID(call))
        await admin.execute(
            "INSERT INTO user_interactions (user_id, tenant_id, role, content, created_at) "
            "VALUES ('agent-seal-text',$1,'assistant','Legacy turn', now() - interval '1 day')", tenant)
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                "INSERT INTO user_interactions (user_id, tenant_id, role, content, content_ciphertext) "
                "VALUES ('agent-seal-text',$1,'user','both',pgp_sym_encrypt('both',$2))", tenant, key)
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute("UPDATE sms_messages SET body_ciphertext=pgp_sym_encrypt('x',$2) WHERE id=$1",
                                legacy_sms, key)

        async def read_everything() -> dict:
            feed = await voice_intel.read_voice_telemetry(
                session_id=uuid.UUID(call), after_event_id=0, limit=100, ctx=agent)
            async with connection.tenant_tx(agent) as conn:
                hydrate = await voice_intel.load_session_events(conn, call, limit=100)
            context = await memory.get_user_context("agent-seal-text")
            async with connection.tenant_tx(agent) as conn:
                ids = [r["id"] for r in await conn.fetch("SELECT id FROM sms_messages ORDER BY id")]
                bundle = await privacy_requests._access_bundle(conn, tenant, {"sms_messages": ids})
            return {
                "feed": sorted(e["transcript"]["text"] for e in feed["events"] if e["transcript"]),
                "hydrate": sorted(e["transcript_excerpt"] for e in hydrate if e["transcript_excerpt"]),
                "memory": sorted(t["content"] for t in context["recent_interactions"]),
                "bundle": sorted(m["body"] for m in bundle["sms_messages"]),
                "bundle_keys": {k for m in bundle["sms_messages"] for k in m},
            }

        want = {
            "feed": ["Legacy transcript", "Roof is new.", "We would take two fifty."],
            "hydrate": ["Legacy transcript", "Roof is new.", "We would take two fifty."],
            "memory": ["Legacy turn", "Seller is in probate"],
            "bundle": ["Call me after five", "Gate code is 4411", "Legacy body"],
        }
        got = await read_everything()
        assert {k: got[k] for k in want} == want, got
        assert not {"body_ciphertext", "body_ciphertext_tenant_id"} & got["bundle_keys"]

        # The brokerage export decrypts the sealed column and keeps the plaintext one.
        op = await admin.fetchval(
            "INSERT INTO privacy_operations (tenant_id, kind, state, requested_by, policy_version) "
            "VALUES ($1,'export','running','live-test','test') RETURNING id::text", tenant)
        buf = io.BytesIO()
        exported = {}
        with zipfile.ZipFile(buf, "w") as archive:
            for table in ("sms_messages", "negotiation_events", "user_interactions"):
                await privacy_export._export_table(privacy_export._platform_ctx(), op, table, key, archive)
        with zipfile.ZipFile(buf) as archive:
            for table, plain, sealed in (("sms_messages", "body", "sealed_body"),
                                         ("negotiation_events", "transcript_excerpt", "sealed_transcript_excerpt"),
                                         ("user_interactions", "content", "sealed_content")):
                rows = [json.loads(line) for line in archive.read(f"tables/{table}.jsonl").splitlines()]
                exported[table] = sorted(r[sealed] or r[plain] for r in rows if r[sealed] or r[plain])
        assert exported == {
            "sms_messages": ["Call me after five", "Gate code is 4411", "Legacy body"],
            "negotiation_events": ["Legacy transcript", "Roof is new.", "We would take two fifty."],
            "user_interactions": ["Legacy turn", "Seller is in probate"],
        }, exported

        # ── the 0152 functions refuse anyone but the row's own platform context ──
        async with connection.tenant_tx(agent) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval("SELECT seal_user_interactions($1, $2, 10, false)", tenant, key)
        other_plat = TenantContext(agent_id="plat-seal-text", tenant_id=str(uuid.uuid4()), role=Role.PLATFORM_ADMIN)
        async with connection.tenant_tx(other_plat) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval("SELECT seal_negotiation_transcripts($1::uuid, $2, 10, false)", tenant, key)
        async with connection.tenant_tx(agent) as conn:  # still append-only for the app
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE user_interactions SET content='x' WHERE tenant_id=$1", tenant)

        # ── the backfill: bounded, resumable, idempotent, reversible ───────
        only_ours = backfill.tenants_pending

        async def ours(field, *, unseal=False):
            return [t for t in await only_ours(field, unseal=unseal) if t == tenant]

        backfill.tenants_pending = ours
        try:
            first = await backfill.backfill_field("sms", batch_size=1, max_rows=1)
            assert first["changed"] == 1
            for field in SLICE_ONE:
                result = await backfill.backfill_field(field, batch_size=1)
                assert result["changed"] == (0 if field == "sms" else 1), result
            for field in SLICE_ONE:
                assert (await backfill.backfill_field(field, batch_size=50))["changed"] == 0, "not idempotent"
            assert await admin.fetchval(
                "SELECT count(*) FROM sms_messages WHERE tenant_id=$1 AND body_ciphertext IS NULL", tenant) == 0
            assert await admin.fetchval(
                "SELECT count(*) FROM negotiation_events WHERE tenant_id=$1 AND transcript_excerpt IS NOT NULL",
                tenant) == 0
            assert await admin.fetchval(
                "SELECT count(*) FROM user_interactions WHERE tenant_id=$1 AND content <> ''", tenant) == 0
            got = await read_everything()
            assert {k: got[k] for k in want} == want, got

            # Rollback: plaintext back, and the old release's readers would see it.
            for field in SLICE_ONE:
                assert (await backfill.backfill_field(field, batch_size=2, unseal=True))["changed"] == {
                    "sms": 3, "transcript": 3, "interaction": 2}[field]
            assert sorted(r["body"] for r in await admin.fetch(
                "SELECT body FROM sms_messages WHERE tenant_id=$1", tenant)) == want["bundle"]
            assert sorted(r["content"] for r in await admin.fetch(
                "SELECT content FROM user_interactions WHERE tenant_id=$1", tenant)) == want["memory"]
            for field in SLICE_ONE:
                await backfill.backfill_field(field, batch_size=500)
        finally:
            backfill.tenants_pending = only_ours

        # ── retention clears a sealed transcript (rolled back: shared dev DB) ──
        old_event = await admin.fetchval(
            "INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, transcript_ciphertext, "
            "model_version, created_by, created_at) VALUES ($1,$2,'transcript',pgp_sym_encrypt('Old words',$3),"
            "'v','agent-seal-text', now() - interval '400 days') RETURNING id", tenant, uuid.UUID(call), key)
        tx = admin.transaction()
        await tx.start()
        try:
            await admin.execute("SELECT set_config('app.current_role','platform_admin',true)")
            await admin.fetchval("SELECT purge_expired_platform_data(3650, 365)")
            row = await admin.fetchrow(
                "SELECT transcript_excerpt, transcript_ciphertext, payload FROM negotiation_events WHERE id=$1",
                old_event)
            assert row["transcript_excerpt"] is None and row["transcript_ciphertext"] is None
            assert "transcript_purged" in row["payload"]
        finally:
            await tx.rollback()

        # A sealed row without the key fails closed instead of reading as empty.
        os.environ.pop("ORACLE_ENCRYPTION_MASTER_KEY")
        try:
            with pytest.raises(RuntimeError):
                await memory.get_user_context("agent-seal-text")
        finally:
            os.environ["ORACLE_ENCRYPTION_MASTER_KEY"] = MASTER
        assert inbound_id and plat.tenant_id == tenant
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
