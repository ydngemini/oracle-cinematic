"""Sealed text slice 2 (0154): email subjects/bodies, interaction-log subjects
and content, AI tool receipts, belief source quotes and profile summaries,
against real PostgreSQL with FORCE RLS (audit A01, ledger L17-22; skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Proves, as the application role:
- every writer (CRM email + internal note, the send worker's bookkeeping row,
  perception events, belief assertions, tool receipts, the profile roll-up)
  stores no plaintext of the sealed fields;
- every reader returns the text for sealed rows AND for pre-0154 plaintext
  rows in one result: email delivery, the subject access bundle, the brokerage
  export, the operator outbox and activity feeds, the client interaction list,
  comms threads and thread view, the client AI context, the lead dossier, the
  Neoh client card, the tool-call replay, beliefs (assert, read, correct) and
  the memory context;
- the previous release's plaintext writes still work, and a row holding both
  shapes is refused;
- the backfill seals old rows in bounded batches, is idempotent and reverses
  with --unseal; interaction_logs is sealed only through seal_interaction_logs,
  which refuses an agent and another tenant's context, and the app still
  cannot UPDATE it;
- a sealed row without the key fails closed (delivery leaves it queued).
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
MASTER = "live-test-sealed-text-two-master-key-0123456789"
SLICE_TWO = ("email", "interaction_log", "tool_result", "belief_quote", "profile_summary")


def test_slice_two_text_is_sealed_and_still_read(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.neoh.test")
    monkeypatch.delenv("ORACLE_DOMAIN", raising=False)
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):  # noqa: C901 - one ordered scenario
    import asyncpg

    import admin_ops
    import ai_chat_store
    import backfill_sealed_text as backfill
    import belief_store
    import brokerage_onboarding as bo
    import client_ai_automation
    import client_enterprise
    import commands_api
    import crm
    import email_outbox
    import intelligence_os_api
    import lead_dossier
    import neoh_intents
    import privacy_export
    import privacy_requests
    from command_providers import ProviderResult
    from crypto import derive_tenant_key
    from db import connection
    from memory_core.session_manager import SessionManager
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client, lead = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    key = derive_tenant_key(tenant, MASTER)
    tag = tenant[:8]
    agent_id, owner_id = f"agent-{tag}@seal2.test", f"owner-{tag}@seal2.test"
    agent = TenantContext(agent_id=agent_id, tenant_id=tenant, role=Role.AGENT)
    owner = TenantContext(agent_id=owner_id, tenant_id=tenant, role=Role.BROKER_OWNER)
    plat = TenantContext(agent_id="plat-seal2", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    person = f"pat-{tag}@client.test"

    sent: list[dict] = []

    async def relay(draft, credentials=None, reply_to=None, list_unsubscribe_url=None):
        sent.append(dict(draft))
        return ProviderResult("smtp", f"<{uuid.uuid4().hex}@seal2.test>", "submitted", {})

    async def no_credential(*a, **k):
        return None

    monkeypatch.setattr(commands_api, "_resolve_email_provider", lambda: (relay, "smtp"))
    monkeypatch.setattr(commands_api, "_load_provider_credential", no_credential)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Seal Two Realty')",
                            tenant, f"seal2-{tag}")
        for who, role in ((agent_id, "agent"), (owner_id, "broker_owner")):
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y','Seal Two',true)", uuid.uuid4(), tenant, who, role)
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id, email) "
            "VALUES ($1,$2,'Pat Sealed',$3,$4)", client, tenant, agent_id, person)
        await admin.execute(
            "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
            "VALUES ($1,$2,$3,'DE',50,$4,'1 Sealed Way')", lead, tenant, f"P-{tag}", client)
        await bo.update_profile(bo.BrokerageProfileUpdate(postal_address="1 Loockerman St\nDover, DE 19901"),
                                owner)

        # ── writers store no plaintext ─────────────────────────────────────
        # A perception event: free text sealed, behavioural keys queryable.
        await intelligence_os_api.capture_events(intelligence_os_api.PerceptionBatch(events=[
            intelligence_os_api.PerceptionEvent(client_id=client, interaction_type="search",
                                                payload={"text": "pool near school", "zip": "19901"})]),
            agent)
        ev = await admin.fetchrow(
            "SELECT payload, pgp_sym_decrypt(content_ciphertext,$2) AS c FROM interaction_logs "
            "WHERE tenant_id=$1 AND interaction_type='search'", uuid.UUID(tenant), key)
        assert json.loads(ev["payload"]) == {"zip": "19901"} and json.loads(ev["c"]) == {"text": "pool near school"}

        sent_email = await crm.send_message(client, crm.MessageCreate(
            channel="email", subject="Gate code inside", body="The gate code is 4411."), agent)
        assert sent_email["interaction"]["subject"] == "Gate code inside"
        outbox_id = sent_email["queued_email_id"]
        row = await admin.fetchrow(
            "SELECT subject, body_text, pgp_sym_decrypt(subject_ciphertext,$2) AS s, "
            "pgp_sym_decrypt(body_text_ciphertext,$2) AS b FROM email_outbox WHERE id=$1",
            uuid.UUID(outbox_id), key)
        assert (row["subject"], row["body_text"], row["s"], row["b"]) == (
            "", "", "Gate code inside", "The gate code is 4411.")

        # The send worker's bookkeeping row: subject sealed, metadata plain.
        await commands_api._record_send_bookkeeping(
            agent, command_id=str(uuid.uuid4()), command_type=commands_api.CommandType.EMAIL,
            target={"client_id": client}, draft={"subject": "Offer follow-up"},
            provider_result=ProviderResult("smtp", f"<bk-{tag}@seal2.test>", "submitted", {}))
        bk = await admin.fetchrow(
            "SELECT subject, payload, pgp_sym_decrypt(subject_ciphertext,$2) AS s FROM interaction_logs "
            "WHERE tenant_id=$1 AND external_message_id=$3", uuid.UUID(tenant), key, f"<bk-{tag}@seal2.test>")
        assert bk["subject"] is None and bk["s"] == "Offer follow-up"
        assert set(json.loads(bk["payload"])) == {"command_id", "provider"}

        note = await crm.send_message(client, crm.MessageCreate(
            channel="note", body="Seller is going through a divorce."), agent)
        assert note["interaction"]["payload"]["body"] == "Seller is going through a divorce."
        logs = await admin.fetch(
            "SELECT subject, payload, pgp_sym_decrypt(subject_ciphertext,$2) AS s, "
            "pgp_sym_decrypt(content_ciphertext,$2) AS c FROM interaction_logs "
            "WHERE tenant_id=$1 AND interaction_type IN ('email','message') AND external_message_id IS NULL "
            "ORDER BY created_at", uuid.UUID(tenant), key)
        assert [(r["subject"], json.loads(r["payload"]).get("body"), r["s"], json.loads(r["c"]))
                for r in logs] == [
            (None, None, "Gate code inside", {"body": "The gate code is 4411."}),
            (None, None, None, {"body": "Seller is going through a divorce."}),
        ]


        # Beliefs: the quote is sealed.
        made = await belief_store.assert_belief(
            agent, subject_type="client", subject_id=client, predicate="prefers_area", value="Dover",
            status="reported", confidence=0.7,
            source=belief_store.BeliefSource(kind="call", ref="call-1", quote="We only want Dover."))
        assert made["source"]["quote"] == "We only want Dover."
        q = await admin.fetchrow(
            "SELECT source_quote, pgp_sym_decrypt(source_quote_ciphertext,$2) AS q FROM beliefs WHERE id=$1",
            uuid.UUID(made["id"]), key)
        assert (q["source_quote"], q["q"]) == (None, "We only want Dover.")

        # Tool receipt: sealed.
        assistant = uuid.uuid4()
        await admin.execute(
            "INSERT INTO ai_chat_messages (id, tenant_id, user_id, role, content_ciphertext, status, request_id) "
            "VALUES ($1,$2,$3,'assistant','\\x00'::bytea,'streaming',$4)", assistant, tenant, agent_id, uuid.uuid4())
        async with connection.tenant_tx(agent) as conn:
            await ai_chat_store._ledger_claim(conn, agent, agent_id, str(assistant), 0, "update_client", "h0")
        await ai_chat_store._ledger_attach_result(agent, str(assistant), 0, {"ok": True, "phone": "+15550001234"})
        tr = await admin.fetchrow(
            "SELECT result, pgp_sym_decrypt(result_ciphertext,$2) AS r FROM ai_tool_operations "
            "WHERE assistant_id=$1 AND call_index=0", assistant, key)
        assert tr["result"] is None and json.loads(tr["r"]) == {"ok": True, "phone": "+15550001234"}

        # Profile summary: sealed. The roll-up itself cannot finish as the app
        # role (pre-existing, L17-22 defect): it DELETEs the archived turns and
        # 0009 grants oracle_app only SELECT/INSERT on user_interactions, so
        # the whole transaction rolls back and nothing is written.
        memory = SessionManager(agent, token_threshold=1, keep_recent=1)

        async def summarize(transcript, prior):
            return "Prefers quick closes."

        memory._summarize = summarize
        for text in ("one", "two", "three"):
            await memory.record_interaction(agent_id, "user", text)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await memory.summarize_rolling_memory(agent_id)
        assert await admin.fetchval("SELECT count(*) FROM user_profiles WHERE user_id=$1", agent_id) == 0
        async with connection.tenant_tx(agent) as conn:
            await memory.store_profile_summary(conn, agent_id, "Prefers quick closes.")
        ps = await admin.fetchrow(
            "SELECT profile_summary, pgp_sym_decrypt(profile_summary_ciphertext,$2) AS p "
            "FROM user_profiles WHERE user_id=$1", agent_id, key)
        assert (ps["profile_summary"], ps["p"]) == ("", "Prefers quick closes.")

        # ── the previous release's plaintext writes still work (expand) ────
        legacy_outbox = await admin.fetchval(
            "INSERT INTO email_outbox (tenant_id, client_id, to_email, subject, body_text, created_by, created_at) "
            "VALUES ($1,$2,$3,'Legacy subject','Legacy body',$4, now() - interval '1 day') RETURNING id",
            uuid.UUID(tenant), uuid.UUID(client), person, agent_id)
        await admin.execute(
            "INSERT INTO interaction_logs (tenant_id, client_id, lead_id, actor_role, interaction_type, direction, "
            "subject, payload, thread_id, created_at) VALUES ($1,$2,$3,'seller','email','inbound','Legacy reply',"
            "'{\"body\": \"Legacy reply body\"}'::jsonb, $4, now() - interval '2 days')",
            uuid.UUID(tenant), uuid.UUID(client), uuid.UUID(lead),
            uuid.UUID(sent_email["interaction"]["thread_id"]))
        await admin.execute(
            "INSERT INTO interaction_logs (tenant_id, lead_id, actor_role, interaction_type, payload, created_at) "
            "VALUES ($1,$2,'agent','voice_note','{\"transcript\": \"Legacy roof note\", \"agent_id\": \"x\"}'::jsonb,"
            " now() - interval '3 days')", uuid.UUID(tenant), uuid.UUID(lead))
        await admin.execute(
            "INSERT INTO beliefs (tenant_id, subject_type, subject_id, predicate, object_value, status, confidence, "
            "source_kind, source_quote, learned_at) VALUES ($1,'client',$2,'timeline','\"60 days\"','reported',0.6,"
            "'sms','Legacy quote', now() - interval '1 day')", uuid.UUID(tenant), client)
        assistant_old = uuid.uuid4()
        await admin.execute(
            "INSERT INTO ai_chat_messages (id, tenant_id, user_id, role, content_ciphertext, status, request_id) "
            "VALUES ($1,$2,$3,'assistant','\\x00'::bytea,'streaming',$4)", assistant_old, tenant, agent_id,
            uuid.uuid4())
        await admin.execute(
            "INSERT INTO ai_tool_operations (tenant_id, user_id, assistant_id, call_index, tool_name, "
            "arguments_hash, status, result) VALUES ($1,$2,$3,0,'update_client','h1','completed',"
            "'{\"ok\": true, \"legacy\": 1}')", uuid.UUID(tenant), agent_id, assistant_old)
        owner_memory = SessionManager(owner)
        await admin.execute(
            "INSERT INTO user_profiles (user_id, tenant_id, profile_summary) VALUES ($1,$2,'Legacy summary')",
            owner_id, tenant)
        for sql in (
            "INSERT INTO email_outbox (tenant_id, client_id, to_email, subject, body_text, subject_ciphertext) "
            "VALUES ($1,$3,'x@y.test','both','b',pgp_sym_encrypt('both',$2))",
            "INSERT INTO interaction_logs (tenant_id, client_id, actor_role, interaction_type, payload, "
            "content_ciphertext) VALUES ($1,$3,'agent','message','{\"body\":\"both\"}',pgp_sym_encrypt('{}',$2))",
            "INSERT INTO beliefs (tenant_id, subject_type, subject_id, predicate, object_value, status, confidence, "
            "source_kind, source_quote, source_quote_ciphertext, learned_at) VALUES ($1,'client',$3::uuid::text,'x','1',"
            "'reported',0.5,'sms','both',pgp_sym_encrypt('both',$2), now())",
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await admin.execute(sql, uuid.UUID(tenant), key, uuid.UUID(client))

        async def read_everything() -> dict:
            got: dict = {}
            got["interactions"] = sorted(
                (i["subject"] or "", i["payload"].get("body") or i["payload"].get("transcript") or "")
                for i in (await crm.client_interactions(client, 50, agent))["interactions"]
                if i["interaction_type"] in ("email", "message", "voice_note"))
            threads = (await crm.comms_threads(agent))["threads"]
            got["thread_last"] = [t["last"]["snippet"] for t in threads if t["client"]["id"] == client]
            thread = await client_enterprise.comms_thread(sent_email["interaction"]["thread_id"], agent)
            got["thread"] = sorted((m["subject"] or "", m["body"] or "") for m in thread["messages"])
            async with connection.tenant_tx(agent) as conn:
                bundle = await client_ai_automation._load_bundle(conn, client)
            got["ai_context"] = sorted(
                client_ai_automation._payload_text(r["payload"]) for r in bundle["interactions"]
                if client_ai_automation._payload_text(r["payload"]))
            dossier = await lead_dossier.get_dossier(lead, agent)
            got["dossier"] = sorted(
                (i["payload"].get("transcript") or i["payload"].get("body") or "") for i in dossier["interactions"])
            _, _, timeline = await neoh_intents._person_detail(agent, client)
            got["card"] = sorted(t["label"] for t in timeline)
            beliefs = await belief_store.beliefs_about(agent, "client", client)
            got["quotes"] = sorted(b["source"]["quote"] for items in beliefs["beliefs"].values() for b in items)
            async with connection.tenant_tx(agent) as conn:
                fresh = await ai_chat_store._ledger_replay(conn, agent, str(assistant), 0, "update_client", "h0")
                old = await ai_chat_store._ledger_replay(conn, agent, str(assistant_old), 0, "update_client", "h1")
            got["replay"] = [fresh, old]
            got["summaries"] = [
                (await memory.get_user_context(agent_id))["parameters"]["profile_summary"],
                (await owner_memory.get_user_context(owner_id))["parameters"]["profile_summary"],
            ]
            async with connection.tenant_tx(agent) as conn:
                ids = [r["id"] for r in await conn.fetch("SELECT id FROM email_outbox WHERE client_id=$1", client)]
                access = await privacy_requests._access_bundle(conn, tenant, {"email_outbox": ids})
            got["access"] = sorted((m["subject"], m["body_text"]) for m in access["email_outbox"])
            got["access_keys"] = {k for m in access["email_outbox"] for k in m}
            feed = await admin_ops.activity(200, plat)
            got["activity"] = sorted(i["subject"] for i in feed["interactions"]
                                     if i["tenant_id"] == tenant and i["subject"])
            box = await admin_ops.outbox(200, None, plat)
            got["outbox"] = sorted(e["subject"] for e in box["emails"] if e["tenant_id"] == tenant)
            return got

        want = {
            "interactions": [("", "Legacy roof note"), ("", "Seller is going through a divorce."),
                             ("Gate code inside", "The gate code is 4411."),
                             ("Legacy reply", "Legacy reply body"), ("Offer follow-up", "")],
            "thread_last": ["Seller is going through a divorce."],
            "thread": [("Gate code inside", "The gate code is 4411."), ("Legacy reply", "Legacy reply body"),
                       ("Offer follow-up", "")],
            "ai_context": sorted(["Legacy reply body", "Seller is going through a divorce.",
                                  "The gate code is 4411.", "pool near school"]),
            "dossier": ["Legacy reply body", "Legacy roof note"],
            "card": sorted(["Gate code inside", "Legacy reply", "Offer follow-up", "message", "search"]),
            "quotes": ["Legacy quote", "We only want Dover."],
            "replay": [{"ok": True, "phone": "+15550001234", "replayed": True},
                       {"ok": True, "legacy": 1, "replayed": True}],
            "summaries": ["Prefers quick closes.", "Legacy summary"],
            "access": [("Gate code inside", "The gate code is 4411."), ("Legacy subject", "Legacy body")],
            "activity": ["Gate code inside", "Legacy reply", "Offer follow-up"],
            "outbox": ["Gate code inside", "Legacy subject"],
        }
        got = await read_everything()
        assert {k: got[k] for k in want} == want, got
        assert not {"subject_ciphertext", "body_text_ciphertext"} & got["access_keys"]

        # Correcting a belief returns its quote too.
        pinned = await belief_store.correct_belief(agent, made["id"], action="pin")
        assert pinned["source"]["quote"] == "We only want Dover."

        # The brokerage export decrypts every sealed column; plaintext ones kept.
        op = await admin.fetchval(
            "INSERT INTO privacy_operations (tenant_id, kind, state, requested_by, policy_version) "
            "VALUES ($1,'export','running','live-test','test') RETURNING id::text", tenant)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for table in ("email_outbox", "interaction_logs", "ai_tool_operations", "beliefs", "user_profiles"):
                await privacy_export._export_table(privacy_export._platform_ctx(), op, table, key, archive)
        with zipfile.ZipFile(buf) as archive:
            def rows(table):
                return [json.loads(line) for line in archive.read(f"tables/{table}.jsonl").splitlines()]

            assert sorted(r["sealed_body_text"] or r["body_text"] for r in rows("email_outbox")) == [
                "Legacy body", "The gate code is 4411."]
            contents = sorted(r["sealed_content"] for r in rows("interaction_logs") if r["sealed_content"])
            assert '{"body": "The gate code is 4411."}' in contents
            assert sorted(r["sealed_source_quote"] or r["source_quote"]
                          for r in rows("beliefs")) == ["Legacy quote", "We only want Dover."]
            def result(r):
                value = r["sealed_result"] or r["result"]
                return json.dumps(json.loads(value) if isinstance(value, str) else value, sort_keys=True)

            assert sorted(result(r) for r in rows("ai_tool_operations")) == [
                '{"legacy": 1, "ok": true}', '{"ok": true, "phone": "+15550001234"}']
            assert sorted(r["sealed_profile_summary"] or r["profile_summary"]
                          for r in rows("user_profiles")) == ["Legacy summary", "Prefers quick closes."]

        # ── delivery opens both shapes; without the key the claim rolls back ──
        os.environ.pop("ORACLE_ENCRYPTION_MASTER_KEY")
        try:
            with pytest.raises(RuntimeError):
                await email_outbox.deliver(tenant, outbox_id)
        finally:
            os.environ["ORACLE_ENCRYPTION_MASTER_KEY"] = MASTER
        assert await admin.fetchval("SELECT status FROM email_outbox WHERE id=$1",
                                    uuid.UUID(outbox_id)) == "queued", "a sealed row is never left claimed"
        assert await email_outbox.deliver(tenant, outbox_id) == {"status": "sent"}
        assert await email_outbox.deliver(tenant, str(legacy_outbox)) == {"status": "sent"}
        assert [(m["subject"], m["body"].split("\n\n--\n")[0]) for m in sent] == [
            ("Gate code inside", "The gate code is 4411."), ("Legacy subject", "Legacy body")]

        # ── seal_interaction_logs refuses anyone but the row's own platform context ──
        async with connection.tenant_tx(agent) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval("SELECT seal_interaction_logs($1::uuid, $2, 10, false)", tenant, key)
        other_plat = TenantContext(agent_id="plat-seal2", tenant_id=str(uuid.uuid4()), role=Role.PLATFORM_ADMIN)
        async with connection.tenant_tx(other_plat) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval("SELECT seal_interaction_logs($1::uuid, $2, 10, false)", tenant, key)
        async with connection.tenant_tx(agent) as conn:  # still append-only for the app
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE interaction_logs SET subject='x' WHERE tenant_id=$1", tenant)

        # ── the backfill: bounded, idempotent, reversible ──────────────────
        only_ours = backfill.tenants_pending

        async def ours(field, *, unseal=False):
            return [t for t in await only_ours(field, unseal=unseal) if t.lower() == tenant]

        backfill.tenants_pending = ours
        try:
            first = await backfill.backfill_field("interaction_log", batch_size=1, max_rows=1)
            assert first["changed"] == 1
            for field in SLICE_TWO:
                await backfill.backfill_field(field, batch_size=1)
            for field in SLICE_TWO:
                assert (await backfill.backfill_field(field, batch_size=50))["changed"] == 0, "not idempotent"
            leftover = await admin.fetchrow(
                """SELECT
                     (SELECT count(*) FROM email_outbox WHERE tenant_id=$1 AND (subject<>'' OR body_text<>'')) AS e,
                     (SELECT count(*) FROM interaction_logs WHERE tenant_id=$1 AND (COALESCE(subject,'')<>''
                        OR payload ?| ARRAY['body','text','transcript','summary','extraction'])) AS i,
                     (SELECT count(*) FROM ai_tool_operations WHERE tenant_id=$1 AND result IS NOT NULL) AS t,
                     (SELECT count(*) FROM beliefs WHERE tenant_id=$1 AND source_quote IS NOT NULL) AS b,
                     (SELECT count(*) FROM user_profiles WHERE tenant_id=$1::text AND profile_summary<>'') AS p""",
                uuid.UUID(tenant))
            assert dict(leftover) == {"e": 0, "i": 0, "t": 0, "b": 0, "p": 0}, dict(leftover)
            # The behavioural keys stayed where filters can read them.
            assert await admin.fetchval(
                "SELECT payload->>'zip' FROM interaction_logs WHERE tenant_id=$1 AND interaction_type='search'",
                uuid.UUID(tenant)) == "19901"
            got = await read_everything()
            assert {k: got[k] for k in want} == want, got

            # Rollback: plaintext back, where the old release reads it.
            for field in SLICE_TWO:
                await backfill.backfill_field(field, batch_size=2, unseal=True)
            plain = await admin.fetchrow(
                """SELECT
                     (SELECT count(*) FROM email_outbox WHERE tenant_id=$1 AND subject_ciphertext IS NOT NULL) AS e,
                     (SELECT count(*) FROM interaction_logs WHERE tenant_id=$1
                        AND (subject_ciphertext IS NOT NULL OR content_ciphertext IS NOT NULL)) AS i,
                     (SELECT string_agg(payload->>'body', '|' ORDER BY created_at) FROM interaction_logs
                       WHERE tenant_id=$1 AND interaction_type IN ('email','message')) AS bodies,
                     (SELECT string_agg(source_quote, '|' ORDER BY source_quote) FROM beliefs
                       WHERE tenant_id=$1) AS quotes""", uuid.UUID(tenant))
            assert (plain["e"], plain["i"]) == (0, 0)
            assert plain["bodies"] == "Legacy reply body|The gate code is 4411.|Seller is going through a divorce."
            assert plain["quotes"] == "Legacy quote|We only want Dover."
            for field in SLICE_TWO:
                await backfill.backfill_field(field, batch_size=500)
        finally:
            backfill.tenants_pending = only_ours

        # A sealed row without the key fails closed instead of reading as empty.
        os.environ.pop("ORACLE_ENCRYPTION_MASTER_KEY")
        try:
            with pytest.raises(RuntimeError):
                await belief_store.beliefs_about(agent, "client", client)
            with pytest.raises(Exception):
                await crm.client_interactions(client, 50, agent)
        finally:
            os.environ["ORACLE_ENCRYPTION_MASTER_KEY"] = MASTER
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
