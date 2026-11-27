"""A note the AI takes on a call is in the database before the AI says it is
saved (external audit 34.4 / RUN-03), against real PostgreSQL with FORCE RLS
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Owner decision 2026-11-20: one AI note per call, written when the first note
is taken and appended to by each later one — through the path call-end
delivery uses (sealed, author neoh-ai-call, review mode, the brokerage's
client) — and delivery at call end writes no second note. Proves:
- each save is committed when save_call_note returns (read back on another
  connection), so a crash after two saves loses neither;
- a retried tool call id appends nothing; a failed write keeps nothing;
- an append reopens review in 'review' mode, keeps the agent's edits, and
  'direct' mode never approves a proposed note;
- a note the agent discarded mid-call is not resurrected;
- the live frame goes to the requester and the assignee only;
- call end: exactly one note and one timeline row; notes that could not be
  saved are shown there, or in a task carrying no content; a consented call
  with no note (or no client) is closed too;
- a stale approve or edit (expected_updated_at) is refused with 409;
- the same tool call id with other text is written; a no after notes were
  saved deletes them; a held lock (2 s) or a long statement (5 s) rolls a
  note write, or a no, back; the timeline meta holds no note text.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "live-test-incremental-call-notes-master-key-0123"
CARD = "4" + "1" * 15  # the standard Luhn-valid test number


def test_each_call_note_is_saved_when_taken_and_the_call_keeps_one_note(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import call_note_settings
    import call_notes_service
    import client_enterprise
    from fastapi import HTTPException
    import client_note_crypto
    import commands_api
    import ws_hub
    from crypto import derive_tenant_key
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, client = str(uuid.uuid4()), uuid.uuid4()
    key = derive_tenant_key(tenant, MASTER)
    ref, direct_ref, fail_ref, lost_ref, gone_ref, lock_ref, quiet_ref, slow_ref = (
        f"{p}-{uuid.uuid4()}" for p in ("inc", "direct", "fail", "lost", "gone", "lock", "quiet", "slow"))
    agent_c = f"agent-c-{uuid.uuid4().hex[:8]}@live.test"
    pushes, tenant_wide = [], []

    async def fake_users_push(tenant_id, user_ids, payload, *, mirror=True, **_kw):  # sensitive= etc. (#167)
        for user_id in user_ids:
            pushes.append((user_id, payload, mirror))
        return len(user_ids)

    async def fake_tenant_push(tenant_id, payload):
        tenant_wide.append(payload)
        return 1

    async def opened(note_id):
        return await admin.fetchrow(
            "SELECT body, author_id, review_state, reviewed_by, reviewed_at, updated_at, "
            "pgp_sym_decrypt(body_ciphertext, $2) AS text FROM client_notes WHERE id=$1",
            uuid.UUID(note_id), key)

    async def call_rows(provider_ref):
        return await admin.fetch(
            "SELECT summary, meta FROM client_activities WHERE client_id=$1 "
            "AND meta->>'source'='ai_call_notes' AND meta->>'provider_call_id'=$2", client, provider_ref)

    async def ai_notes():
        return await admin.fetchval(
            "SELECT count(*) FROM client_notes WHERE client_id=$1 AND author_id='neoh-ai-call'", client)

    real_users_push, real_tenant_push = ws_hub.broadcast_users, ws_hub.broadcast
    ws_hub.broadcast_users, ws_hub.broadcast = fake_users_push, fake_tenant_push
    try:
        save = commands_api.save_call_note
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Incremental notes test')",
                            tenant, f"inc-{tenant[:8]}")
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-b','agent','agent-b','scrypt$x$y')", uuid.uuid4(), tenant)
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Sarah','agent-b')",
            client, tenant)
        agent_a_id = uuid.uuid4()
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent-a','agent','agent-a','scrypt$x$y')", agent_a_id, tenant)
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'edit','owner')", tenant, client, agent_a_id)
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,$3,'agent',$3,'scrypt$x$y')", uuid.uuid4(), tenant, agent_c)
        for provider_ref, creator in ((ref, "agent-a"), (direct_ref, agent_c), (fail_ref, "agent-a"),
                                      (lost_ref, "agent-a"), (gone_ref, "agent-a"), (lock_ref, "agent-a"),
                                      (quiet_ref, "agent-a"), (slow_ref, "agent-a")):
            await admin.execute(
                """INSERT INTO live_call_sessions (tenant_id, client_id, consent_recorded, consent_basis,
                                                   transcript_status, provider_call_id, created_by)
                   VALUES ($1,$2,false,'awaiting_explicit_live_transcription_consent','pending',$3,$4)""",
                tenant, client, provider_ref, creator)

        # Before a yes, nothing is written and nothing may be claimed.
        refused = await save(ref, "Prefers afternoons", tool_call_id="t0")
        assert refused.status == "refused" and not refused.saved and await ai_notes() == 0
        assert await commands_api.record_call_note_consent(ref, True) is True

        # The first note: committed when save returns (read on another connection).
        first = await save(ref, "Prefers afternoon showings", tool_call_id="t1", conversation_id="conv-1")
        assert (first.status, first.count) == ("saved", 1) and first.saved
        note_id = first.note_id
        row = await opened(note_id)
        assert row["body"] == "", "an AI call note was stored in plaintext"
        assert row["text"].endswith("\n- Prefers afternoon showings") and "the caller agreed to notes" in row["text"]
        assert (row["author_id"], row["review_state"]) == ("neoh-ai-call", "proposed")
        [activity] = await call_rows(ref)
        assert activity["summary"] == "Call notes from Neoh (1) — waiting for your review"
        assert sorted(u for u, _, _ in pushes) == ["agent-a", "agent-b"] and tenant_wide == []
        assert all(p["note_id"] == note_id and p["notes"] == ["Prefers afternoon showings"]
                   and p["review_state"] == "proposed" and m is False for _, p, m in pushes)

        # The provider retries the same tool call: nothing is appended, nothing is sent.
        pushes.clear()
        again = await save(ref, "Prefers afternoon showings", tool_call_id="t1")
        assert (again.status, again.note_id, again.saved) == ("duplicate", note_id, True)
        assert (await opened(note_id))["text"].count("Prefers afternoon showings") == 1 and pushes == []

        # The second note goes into the same note.
        second = await save(ref, "Would love waterfront", tool_call_id="t2")
        assert (second.note_id, second.count) == (note_id, 2)
        # The bridge dies here (no call-end delivery): both are already kept.
        text = (await opened(note_id))["text"]
        assert "- Prefers afternoon showings\n- Would love waterfront" in text
        assert await ai_notes() == 1 and len(await call_rows(ref)) == 1
        assert pushes[-1][1]["notes"] == ["Prefers afternoon showings", "Would love waterfront"]

        # The agent opens the note; the AI appends while they read it. Approving
        # the version they saw is refused: a line they never saw is not approved.
        agent_b = TenantContext(agent_id="agent-b", tenant_id=tenant, role=Role.AGENT)
        seen_at = (await opened(note_id))["updated_at"].isoformat()
        await save(ref, "Call back Friday", tool_call_id="t3")
        with pytest.raises(HTTPException) as stale:
            await client_enterprise.approve_note(str(client), note_id, client_enterprise.NoteApprove(
                expected_updated_at=seen_at), ctx=agent_b)
        assert (stale.value.status_code, stale.value.detail) == (409, client_enterprise.NOTE_CHANGED)
        assert (await opened(note_id))["review_state"] == "proposed"
        current = (await opened(note_id))["updated_at"].isoformat()
        approved = await client_enterprise.approve_note(str(client), note_id, client_enterprise.NoteApprove(
            expected_updated_at=current), ctx=agent_b)
        assert approved["review_state"] == "approved" and approved["body"].endswith("- Call back Friday")

        # Approved mid-call: new words reopen review.
        await save(ref, "Has a dog", tool_call_id="t4")
        row = await opened(note_id)
        assert (row["review_state"], row["reviewed_by"], row["reviewed_at"]) == ("proposed", None, None)
        assert row["text"].endswith("- Has a dog")

        # A stale editor cannot overwrite a line appended while it was open;
        # card numbers never reach the stored note or the live frame.
        editor_at = row["updated_at"].isoformat()
        pushes.clear()
        await save(ref, f"Pays the deposit with card {CARD}", tool_call_id="t5")
        assert pushes and all(CARD not in json.dumps(p).replace(" ", "") for _, p, _ in pushes), \
            "save_call_note sent a card number in the live frame"
        with pytest.raises(HTTPException) as stale:
            await client_enterprise.update_note(str(client), note_id, client_enterprise.NotePatch(
                body="Sarah: afternoons, waterfront, call Friday.", expected_updated_at=editor_at), ctx=agent_b)
        assert (stale.value.status_code, stale.value.detail) == (409, client_enterprise.NOTE_CHANGED)
        assert "- Pays the deposit with card" in (await opened(note_id))["text"], "the appended line was lost"

        # Edited from the latest version: the agent's words stay, the next note follows them.
        latest = (await opened(note_id))["updated_at"].isoformat()
        await client_enterprise.update_note(str(client), note_id, client_enterprise.NotePatch(
            body="Sarah: afternoons, waterfront, call Friday, deposit by link.", expected_updated_at=latest),
            ctx=agent_b)
        await save(ref, "Wants a garage", tool_call_id="t6")
        row = await opened(note_id)
        assert row["text"] == "Sarah: afternoons, waterfront, call Friday, deposit by link.\n- Wants a garage"
        # The timeline row is not sealed: its meta (tool call keys included)
        # carries no word of any note.
        [activity] = await call_rows(ref)
        for said in ("Prefers afternoon showings", "Would love waterfront", "Call back Friday",
                     "Has a dog", "Pays the deposit", "Wants a garage", "afternoon", "garage"):
            assert said not in activity["meta"], f"note text in the timeline meta: {said!r}"

        # Call end: delivery finds the note and writes no second one.
        pushes.clear()
        delivered = await commands_api.deliver_call_notes(
            ref, ["Prefers afternoon showings", "Would love waterfront", "Call back Friday", "x"], "conv-1")
        assert delivered is None and pushes == []
        assert await ai_notes() == 1 and len(await call_rows(ref)) == 1
        assert await admin.fetchval(
            "SELECT transcript_status FROM live_call_sessions WHERE provider_call_id=$1", ref) == "complete"
        # A note the caller was told could not be saved shows on the call's row.
        assert await commands_api.deliver_call_notes(ref, ["x"], unsaved=1) is None
        [activity] = await call_rows(ref)
        assert activity["summary"] == "Call notes from Neoh (6) — waiting for your review — 1 could not be saved"

        # Discarded mid-call: it stays gone; the next note starts the call's new note.
        await client_enterprise.delete_note(str(client), note_id, ctx=agent_b)
        fresh = await save(ref, "Has a dog", tool_call_id="t7")
        assert fresh.saved and fresh.note_id != note_id
        assert await admin.fetchval("SELECT count(*) FROM client_notes WHERE id=$1", uuid.UUID(note_id)) == 0
        [activity] = await call_rows(ref)
        assert json.loads(activity["meta"])["note_id"] == fresh.note_id
        assert (await opened(fresh.note_id))["text"].endswith("\n- Has a dog") and await ai_notes() == 1

        # 'direct': approved at once, and an append never approves a proposed note.
        ctx_c = TenantContext(agent_id=agent_c, tenant_id=tenant, role=Role.AGENT)
        async with connection.tenant_tx(ctx_c) as conn:
            assert await call_note_settings.write_mode(conn, tenant, agent_c, "direct") == "direct"
        assert await commands_api.record_call_note_consent(direct_ref, True) is True
        direct = await save(direct_ref, "Prefers mornings", tool_call_id="d1")
        assert (await opened(direct.note_id))["review_state"] == "approved"
        await admin.execute("UPDATE client_notes SET review_state='proposed' WHERE id=$1", uuid.UUID(direct.note_id))
        # The same tool call id with other text is a new note, not a retry.
        cats = await save(direct_ref, "Has two cats", tool_call_id="d1")
        assert (cats.status, cats.count) == ("saved", 2)
        row = await opened(direct.note_id)
        assert row["review_state"] == "proposed" and row["text"].endswith("- Prefers mornings\n- Has two cats")
        assert (await save(direct_ref, "Has two cats", tool_call_id="d1")).status == "duplicate"

        # The callee says no after notes were saved: nothing from the call is kept.
        assert await commands_api.record_call_note_consent(gone_ref, True) is True
        gone = await save(gone_ref, "Prefers evenings", tool_call_id="g1")
        await save(gone_ref, "Has a boat", tool_call_id="g2")
        assert await commands_api.record_call_note_consent(gone_ref, False) is True
        assert await admin.fetchval("SELECT count(*) FROM client_notes WHERE id=$1", uuid.UUID(gone.note_id)) == 0
        [withdrawn] = await call_rows(gone_ref)
        assert withdrawn["summary"] == commands_api.CALL_NOTES_WITHDRAWN
        meta = json.loads(withdrawn["meta"])
        assert meta["note_id"] is None and meta["withdrawn"] is True and meta["tool_call_keys"] == []
        assert "evenings" not in withdrawn["meta"] and "boat" not in withdrawn["meta"]
        assert (await save(gone_ref, "Has a boat", tool_call_id="g3")).status == "refused"
        assert await commands_api.deliver_call_notes(gone_ref, ["Prefers evenings"]) is None
        assert await admin.fetchval(
            "SELECT count(*) FROM client_notes n JOIN client_activities a ON a.meta->>'note_id' = n.id::text "
            "WHERE a.meta->>'provider_call_id'=$1", gone_ref) == 0

        # The caller waits on the write: a held lock rolls it back within the
        # lock timeout, and the bridge says "not saved" (any exception).
        assert await commands_api.record_call_note_consent(lock_ref, True) is True
        holder = admin.transaction()
        await holder.start()
        try:
            await admin.execute("SELECT 1 FROM live_call_sessions WHERE provider_call_id=$1 FOR UPDATE", lock_ref)
            started = time.monotonic()
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await asyncio.wait_for(save(lock_ref, "Prefers evenings", tool_call_id="l1"), 15)
            # 2 s here; the pool default (db.connection.LOCK_TIMEOUT) is longer.
            assert time.monotonic() - started < 4, "the lock wait was not bounded"
        finally:
            await holder.rollback()
        assert await call_rows(lock_ref) == []
        assert (await save(lock_ref, "Prefers evenings", tool_call_id="l1")).status == "saved"

        # A statement that runs past 5 s rolls the write back too.
        assert await commands_api.record_call_note_consent(slow_ref, True) is True
        kept = await save(slow_ref, "Prefers evenings", tool_call_id="s1")
        # _call_note_client is called by the withdrawal inside call_notes_service
        # (35-02 slice 3), so it is replaced there, not on commands_api.
        real_read_mode, real_client = call_note_settings.read_mode, call_notes_service._call_note_client

        async def slow_read_mode(conn, *args):
            await conn.execute("SELECT pg_sleep(6)")
            return await real_read_mode(conn, *args)

        call_note_settings.read_mode = slow_read_mode
        try:
            started = time.monotonic()
            with pytest.raises(asyncpg.exceptions.QueryCanceledError):
                await save(slow_ref, "Has a boat", tool_call_id="s2")
            assert time.monotonic() - started < 5.9, "the statement was not bounded"
        finally:
            call_note_settings.read_mode = real_read_mode
        assert (await opened(kept.note_id))["text"].endswith("\n- Prefers evenings")

        # A no is bounded the same way; a timeout keeps everything as it was
        # (the bridge then retries the no at hang-up).
        async def consent_and_note():
            return (await admin.fetchval("SELECT consent_recorded FROM live_call_sessions "
                                         "WHERE provider_call_id=$1", slow_ref),
                    await admin.fetchval("SELECT count(*) FROM client_notes WHERE id=$1",
                                         uuid.UUID(kept.note_id)))

        holder = admin.transaction()
        await holder.start()
        try:
            await admin.execute("SELECT 1 FROM client_notes WHERE id=$1 FOR UPDATE", uuid.UUID(kept.note_id))
            started = time.monotonic()
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await asyncio.wait_for(commands_api.record_call_note_consent(slow_ref, False), 15)
            assert time.monotonic() - started < 4, "the withdrawal's lock wait was not bounded"
        finally:
            await holder.rollback()
        assert await consent_and_note() == (True, 1)

        async def slow_client(conn, session):
            await conn.execute("SELECT pg_sleep(6)")
            return await real_client(conn, session)

        call_notes_service._call_note_client = slow_client
        try:
            started = time.monotonic()
            with pytest.raises(asyncpg.exceptions.QueryCanceledError):
                await commands_api.record_call_note_consent(slow_ref, False)
            assert time.monotonic() - started < 5.9, "the withdrawal's statement was not bounded"
        finally:
            call_notes_service._call_note_client = real_client
        assert await consent_and_note() == (True, 1)
        assert await commands_api.record_call_note_consent(slow_ref, False) is True
        assert await consent_and_note() == (False, 0)

        # Hang-up closes a consented call that took no note.
        assert await commands_api.record_call_note_consent(quiet_ref, True) is True
        assert await commands_api.deliver_call_notes(quiet_ref, []) is None
        assert await admin.fetchval(
            "SELECT transcript_status FROM live_call_sessions WHERE provider_call_id=$1", quiet_ref) == "complete"
        assert await call_rows(quiet_ref) == []

        # A failed write keeps nothing (rolled back); a retry of it is a fresh write.
        assert await commands_api.record_call_note_consent(fail_ref, True) is True
        os.environ.pop("ORACLE_ENCRYPTION_MASTER_KEY")
        try:
            with pytest.raises(client_note_crypto.NoteSealError):
                await save(fail_ref, "Prefers afternoons", tool_call_id="f1")
        finally:
            os.environ["ORACLE_ENCRYPTION_MASTER_KEY"] = MASTER
        assert await call_rows(fail_ref) == []
        retry = await save(fail_ref, "Prefers afternoons", tool_call_id="f1")
        assert retry.status == "saved" and retry.count == 1

        # Nothing saved during a call at all: a task tells the requester, with no content.
        assert await commands_api.record_call_note_consent(lost_ref, True) is True
        for _ in range(2):
            assert await commands_api.deliver_call_notes(lost_ref, [], unsaved=2) is None
        tasks = await admin.fetch(
            "SELECT client_id, title, details, assignee_id FROM client_tasks "
            "WHERE tenant_id=$1 AND created_by='neoh-ai-call'", uuid.UUID(tenant))
        assert len(tasks) == 1 and tasks[0]["title"] == "Call notes could not be saved"
        assert tasks[0]["client_id"] == client and tasks[0]["assignee_id"] == "agent-a"
        assert "could not save 2 note(s)" in tasks[0]["details"] and lost_ref in tasks[0]["details"]

        # A lead with no client record: nowhere to keep it; the requester is told once.
        prospect, prospect_ref = uuid.uuid4(), f"prospect-{uuid.uuid4()}"
        await admin.execute(
            "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, address) "
            "VALUES ($1,$2,$3,'DE',50,'2 Prospect Ln')", prospect, tenant, f"PROS-{prospect.hex[:6]}")
        await admin.execute(
            """INSERT INTO live_call_sessions (tenant_id, lead_id, consent_recorded, consent_basis,
                                               transcript_status, provider_call_id, created_by)
               VALUES ($1,$2,true,'consented_on_ai_call','active',$3,'agent-a')""",
            tenant, prospect, prospect_ref)
        before = await admin.fetchval("SELECT count(*) FROM client_notes WHERE tenant_id=$1", uuid.UUID(tenant))
        for call_id in ("p1", "p2"):
            assert (await save(prospect_ref, "Wants to sell in spring", tool_call_id=call_id)).status == "no_client"
        assert await admin.fetchval(
            "SELECT count(*) FROM client_notes WHERE tenant_id=$1", uuid.UUID(tenant)) == before
        told = await admin.fetch(
            "SELECT details FROM client_tasks WHERE tenant_id=$1 AND created_by='neoh-ai-call' "
            "AND title='Call notes were not kept: this lead has no client record'", uuid.UUID(tenant))
        assert len(told) == 1 and "Wants to sell" not in told[0]["details"]
        # Hang-up closes the no-client call too (it never reached delivery before).
        assert await commands_api.deliver_call_notes(prospect_ref, []) is None
        assert await admin.fetchval(
            "SELECT transcript_status FROM live_call_sessions WHERE provider_call_id=$1", prospect_ref) == "complete"
        assert len(await admin.fetch(
            "SELECT 1 FROM client_tasks WHERE tenant_id=$1 AND created_by='neoh-ai-call' "
            "AND title='Call notes were not kept: this lead has no client record'", uuid.UUID(tenant))) == 1
    finally:
        ws_hub.broadcast_users, ws_hub.broadcast = real_users_push, real_tenant_push
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
