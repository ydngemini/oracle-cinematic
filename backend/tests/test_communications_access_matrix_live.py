"""Texts, emails, calls and the rows derived from them follow their client or
contact (0140) — and the compliance gate still sees the whole brokerage.
Against real PostgreSQL with FORCE RLS, as the application role (skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 03 AC03.01/AC03.02 for the communications tables, owner decision
D-2026-10-13-01. The sibling of test_brokerage_access_matrix_live.py (clients,
0131) and test_contact_access_matrix_live.py (contacts, 0134).

Actors: agent A, agent B, agent C (B's client and contact shared with C to
VIEW), the broker owner, a platform admin, an agent of another brokerage, and a
brokerage-scope pipeline (inbound SMS/voice matching, webhooks).

A compliance check that sees fewer rows fails OPEN, so the second half proves
an agent who cannot see a colleague's client is still blocked by that client's
STOP text and spoken opt-out, and still counts the colleague's calls.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "c" * 64  # throwaway encryption key for this test only

# table -> (has client_id, has contact_id, write needs edit)
LINKED = {
    "sms_messages": (True, True, True),
    "email_outbox": (True, False, True),
    "live_call_sessions": (True, True, True),
    "inbound_voice_calls": (True, True, True),
    "contract_synthesis_artifacts": (True, False, True),
    "smart_plan_enrollments": (False, True, True),
    "agent_call_intents": (False, True, True),
    "lead_response_events": (True, True, False),
    "outcome_events": (True, False, False),
    "mission_candidates": (True, False, False),
}
CHILDREN = {"negotiation_events": "live_call_sessions", "smart_plan_step_runs": "smart_plan_enrollments"}
COMPLIANCE = ("outreach_consent", "outreach_suppression", "outreach_attempt_log", "suppression_tombstones")
TENANT_WALL = {"outreach_consent": "tenant_isolation", "outreach_suppression": "tenant_isolation",
               "outreach_attempt_log": "tenant_isolation",
               "suppression_tombstones": "suppression_tombstones_tenant_isolation"}


def test_communications_follow_the_record(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


def test_communications_policy_composition_cannot_widen(monkeypatch):
    # Call telemetry seals the transcript (0152, L17-21): writes need the key.
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_policy_composition())


async def _policy_composition():
    """AC03.02: each table keeps exactly its tenant wall as the permissive
    policy plus four restrictive 0140 ones (a new permissive policy would OR
    around them, F03.01); the compliance tables carry NO agent narrowing."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT tablename, policyname, permissive, cmd FROM pg_policies WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])", [*LINKED, *CHILDREN, *COMPLIANCE])
    finally:
        await admin.close()
    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r["tablename"], []).append(r)
    for t in (*LINKED, *CHILDREN):
        permissive = [r for r in by_table[t] if r["permissive"] == "PERMISSIVE"]
        assert len(permissive) == 1 and permissive[0]["cmd"] == "ALL", (t, permissive)
        restrictive = {(r["policyname"], r["cmd"]) for r in by_table[t] if r["permissive"] == "RESTRICTIVE"}
        assert restrictive == {(f"{t}_linked_read", "SELECT"), (f"{t}_linked_insert", "INSERT"),
                               (f"{t}_linked_update", "UPDATE"), (f"{t}_linked_delete", "DELETE")}, (t, restrictive)
    for t in COMPLIANCE:
        assert {(r["policyname"], r["permissive"]) for r in by_table[t]} == {(TENANT_WALL[t], "PERMISSIVE")}, (
            t, by_table[t])


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import commands_api
    import inbound_voice
    import media_api
    import messaging_data
    import record_access
    import sales_api
    import telephony_api
    import voice_intel
    from contact_truth import lookup_hash
    from crypto import encrypt_pii
    from db import connection
    from outreach_compliance import Channel, ConsentLedger, VoiceMode, guard_outreach
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@comms.test" for k in users}
    # One client + contact per kind: A's, B's (shared view-only with C), the pool.
    people = {k: {"client": uuid.uuid4(), "contact": uuid.uuid4()} for k in ("a", "b", "pool")}
    assignee = {"a": agent["a"], "b": agent["b"], "pool": None}
    phone_b, phone_cap, phone_voice = "+13025550101", "+13025550102", "+13025550103"

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other_tenant if who == "d" else tenant, role=role)

    admin_ctx = TenantContext(agent_id="comms-admin", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    pipeline = brokerage_scope(TenantContext(agent_id=f"inbound:{tag}", tenant_id=tenant, role=Role.AGENT))
    actors = {"a": ctx("a"), "b": ctx("b"), "c": ctx("c"), "owner": ctx("owner"), "d": ctx("d"),
              "admin": admin_ctx, "pipeline": pipeline}

    rows: dict[str, dict[str, str]] = {t: {} for t in (*LINKED, *CHILDREN)}  # table -> kind -> row id
    unlinked_sms = str(uuid.uuid4())

    async def visible(c: TenantContext, table: str) -> set[str]:
        async with connection.tenant_tx(c) as conn:
            found = await conn.fetch(f"SELECT id FROM {table} WHERE tenant_id = ANY($1::uuid[])",
                                     [tenant, other_tenant])
        mine = set(rows[table].values()) | ({unlinked_sms} if table == "sms_messages" else set())
        return {str(r["id"]) for r in found} & mine

    def kinds(table: str, *ks: str) -> set[str]:
        return {rows[table][k] for k in ks}

    try:
        for t, slug in ((tenant, "comms"), (other_tenant, "comms-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Comms matrix')",
                                t, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other_tenant if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for k, p in people.items():
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state, phone_lookup_hash) "
                "VALUES ($1,$2,$3,'matrix','pending_encryption',$4)", p["contact"], tenant, assignee[k],
                lookup_hash(tenant, "phone", phone_b) if k == "b" else None)
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, contact_id, email) "
                "VALUES ($1,$2,$3,$4,$5,$6)", p["client"], tenant, f"Comms {k}", assignee[k], p["contact"],
                f"{k}-{tag}@client.test")
        for kind in ("client", "contact"):
            await admin.execute(
                "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
                "granted_by) VALUES ($1,$2,$3,$4,'view',$5)", tenant, kind, people["b"][kind], users["c"], agent["b"])

        caller_ct = await encrypt_pii(admin, "+13025550177", inbound_voice._tenant_key(tenant))
        # Communications rows, one per kind and table. Foreign keys to plans,
        # missions, templates and routes are not what this test is about:
        # seeded with replication-role replica (clients/contacts above went
        # through their triggers normally).
        await admin.execute("SET session_replication_role = replica")
        route_a = uuid.uuid4()
        await admin.execute(
            "INSERT INTO telephony_routes (id, tenant_id, agent_id, inbound_did, provider, provider_account_id, "
            "forward_on_request, forward_when_ai_unavailable) "
            "VALUES ($1,$2,$3,'+13025550199','plivo','MAmatrix0140',false,false)", route_a, tenant, agent["a"])
        for k, p in people.items():
            cl, co = p["client"], p["contact"]
            ids = {t: uuid.uuid4() for t in (*LINKED, *CHILDREN)}
            await admin.execute(
                "INSERT INTO sms_messages (id, tenant_id, agent_id, contact_id, client_id, direction, provider, "
                "provider_message_id, status, from_e164, to_e164, body) VALUES ($1,$2,$3,$4,$5,'outbound','telnyx',"
                "$6,'sent','+13025550199','+13025550100','matrix body')",
                ids["sms_messages"], tenant, agent["a"], co, cl, f"m-{tag}-{k}")
            await admin.execute(
                "INSERT INTO email_outbox (id, tenant_id, client_id, to_email, subject, body_text, created_by) "
                "VALUES ($1,$2,$3,'x@client.test','s','b',$4)", ids["email_outbox"], tenant, cl, agent["a"])
            await admin.execute(
                "INSERT INTO live_call_sessions (id, tenant_id, client_id, contact_id, created_by) "
                "VALUES ($1,$2,$3,$4,$5)", ids["live_call_sessions"], tenant, cl, co, agent["a"])
            ids["negotiation_events"] = await admin.fetchval(  # bigint id
                "INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, transcript_excerpt, "
                "payload, model_version, created_by) VALUES ($1,$2,'transcript','said a thing','{}','m',$3) "
                "RETURNING id", tenant, ids["live_call_sessions"], agent["a"])
            await admin.execute(
                "INSERT INTO inbound_voice_calls (id, tenant_id, route_id, contact_id, client_id, provider, "
                "provider_call_sid, provider_status, intake_mode, caller_phone_lookup_hash, caller_phone_ciphertext, "
                "transcript_status, handoff_status, disclosure_version, disclosed_at) VALUES "
                "($1,$2,$3,$4,$5,'plivo',$6,'completed','auto',$7,$8,'complete','matched','v1',now())",
                ids["inbound_voice_calls"], tenant, route_a, co, cl, str(uuid.uuid4()), "0" * 64, caller_ct)
            await admin.execute(
                "INSERT INTO contract_synthesis_artifacts (id, tenant_id, client_id, doc_id, state_code, template_id, "
                "template_sha256, status, created_by) VALUES ($1,$2,$3,$4,'DE',$5,$6,'generating',$7)",
                ids["contract_synthesis_artifacts"], tenant, cl, f"doc-{k}", uuid.uuid4(), "a" * 64, agent["a"])
            await admin.execute(
                "INSERT INTO smart_plan_enrollments (id, tenant_id, plan_id, revision_id, contact_id, status, "
                "preview_hash, created_by) VALUES ($1,$2,$3,$4,$5,'active',$6,$7)",
                ids["smart_plan_enrollments"], tenant, uuid.uuid4(), uuid.uuid4(), co, "b" * 64, agent["c"])
            await admin.execute(
                "INSERT INTO smart_plan_step_runs (id, tenant_id, enrollment_id, step_key, step_index, step_type, "
                "scheduled_for) VALUES ($1,$2,$3,'s1',0,'wait',now())",
                ids["smart_plan_step_runs"], tenant, ids["smart_plan_enrollments"])
            await admin.execute(
                "INSERT INTO agent_call_intents (id, tenant_id, agent_id, contact_id, state, expires_at) "
                "VALUES ($1,$2,$3,$4,'prepared',now() + interval '5 minutes')",
                ids["agent_call_intents"], tenant, agent["a"], co)
            await admin.execute(
                "INSERT INTO lead_response_events (id, tenant_id, contact_id, client_id, origin, channel, "
                "disposition, lead_created_at) VALUES ($1,$2,$3,$4,'speed_to_lead','sms','staged',now())",
                ids["lead_response_events"], tenant, co, cl)
            await admin.execute(
                "INSERT INTO outcome_events (id, tenant_id, outcome_kind, subject_type, subject_id, client_id, "
                "occurred_at, source_table, source_id) VALUES ($1,$2,'reply_received','client',$3,$4,now(),"
                "'interaction_logs',$5)", ids["outcome_events"], tenant, str(cl), cl, uuid.uuid4())
            await admin.execute(
                "INSERT INTO mission_candidates (id, tenant_id, mission_id, subject_type, subject_id, client_id) "
                "VALUES ($1,$2,$3,'client',$4,$5)", ids["mission_candidates"], tenant, uuid.uuid4(), str(cl), cl)
            for t, i in ids.items():
                rows[t][k] = str(i)
        await admin.execute(
            "INSERT INTO sms_messages (id, tenant_id, agent_id, direction, provider, provider_message_id, status, "
            "from_e164, to_e164, body) VALUES ($1,$2,$3,'inbound','telnyx',$4,'delivered','+13025550177',"
            "'+13025550199','who is this')", uuid.UUID(unlinked_sms), tenant, agent["a"], f"m-{tag}-unlinked")
        await admin.execute("SET session_replication_role = origin")

        # ── read: each actor sees the rows of the records they can see ──────
        for table in (*LINKED, *CHILDREN):
            extra = {unlinked_sms} if table == "sms_messages" else set()
            everything = kinds(table, "a", "b", "pool") | extra
            expected = {
                "a": kinds(table, "a", "pool") | extra,
                "b": kinds(table, "b", "pool") | extra,
                "c": kinds(table, "b", "pool") | extra,  # B's records, shared view-only
                "owner": everything, "admin": everything, "pipeline": everything,
                "d": set(),  # another brokerage
            }
            for who, want in expected.items():
                got = await visible(actors[who], table)
                assert got == want, (table, who, sorted(got), sorted(want))

        # The policy and app_linked_access() (what the API reads back) agree
        # for every actor and row, for view and for edit.
        link_rows = await admin.fetch("SELECT id, client_id, contact_id FROM sms_messages WHERE tenant_id=$1", tenant)
        for who, c in actors.items():
            seen = await visible(c, "sms_messages")
            async with connection.tenant_tx(c) as conn:
                for r in link_rows:
                    view = await conn.fetchval("SELECT app_linked_access($1, $2, false)", r["client_id"],
                                               r["contact_id"])
                    edit = await conn.fetchval("SELECT app_linked_access($1, $2, true)", r["client_id"],
                                               r["contact_id"])
                    rid = str(r["id"])
                    if who == "d":
                        continue  # the tenant wall, not this function, keeps another brokerage out
                    assert view == (rid in seen), (who, rid, view)
                    assert edit == (view and not (who == "c" and rid == rows["sms_messages"]["b"])), (who, rid, edit)

        # ── write: a view share reads a conversation, it does not send in it ─
        sms_b = uuid.UUID(rows["sms_messages"]["b"])
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await conn.execute("UPDATE sms_messages SET status='delivered' WHERE id=$1", sms_b) == "UPDATE 0"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("c")) as conn:
                await conn.execute(
                    "INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, payload, model_version, "
                    "created_by) VALUES ($1,$2,'consent','{}','m',$3)",
                    tenant, uuid.UUID(rows["live_call_sessions"]["b"]), agent["c"])
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # nor log a text on a colleague's record
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute(
                    "INSERT INTO sms_messages (tenant_id, agent_id, client_id, direction, provider, "
                    "provider_message_id, from_e164, to_e164) VALUES ($1,$2,$3,'outbound','telnyx',$4,'+1','+1')",
                    tenant, agent["a"], people["b"]["client"], f"m-{tag}-sneak")
        async with connection.tenant_tx(ctx("a")) as conn:  # own record: fine
            await conn.execute(
                "INSERT INTO sms_messages (tenant_id, agent_id, client_id, contact_id, direction, provider, "
                "provider_message_id, from_e164, to_e164) VALUES ($1,$2,$3,$4,'outbound','telnyx',$5,'+1','+1')",
                tenant, agent["a"], people["a"]["client"], people["a"]["contact"], f"m-{tag}-own")
        # Ledgers record facts about a visible record: a view share may add one.
        async with connection.tenant_tx(ctx("c")) as conn:
            await conn.execute(
                "INSERT INTO outcome_events (tenant_id, outcome_kind, subject_type, subject_id, client_id, "
                "occurred_at, source_table, source_id) VALUES ($1,'showing_held','client',$2,$3,now(),'showings',$4)",
                tenant, str(people["b"]["client"]), people["b"]["client"], uuid.uuid4())
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # ...but not about one they cannot see
            async with connection.tenant_tx(ctx("a")) as conn:
                await conn.execute(
                    "INSERT INTO outcome_events (tenant_id, outcome_kind, subject_type, subject_id, client_id, "
                    "occurred_at, source_table, source_id) VALUES ($1,'showing_held','client',$2,$3,now(),"
                    "'showings',$4)", tenant, str(people["b"]["client"]), people["b"]["client"], uuid.uuid4())

        # ── the API: 404 when invisible, 403 when view-only ────────────────
        consent = commands_api.CallConsent(consent_recorded=True, consent_basis="verbal consent on the call")
        call_b = rows["live_call_sessions"]["b"]
        for who, code in (("a", 404), ("c", 403)):
            with pytest.raises(HTTPException) as e:
                await commands_api.record_call_consent(call_b, consent, ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
        with pytest.raises(HTTPException) as e:
            await commands_api.call_telemetry(
                call_b, commands_api.CallTelemetry(transcript_excerpt="x", counter_offer=1, arv=2, rehab=1),
                ctx=ctx("c"))
        assert e.value.status_code == 403
        await commands_api.record_call_consent(call_b, consent, ctx=ctx("b"))  # the assignee may
        for who, code in (("a", 404), ("c", 403)):
            with pytest.raises(HTTPException) as e:
                await voice_intel.create_voice_session(
                    voice_intel.VoiceSessionRequest(client_id=people["b"]["client"], consent_recorded=False,
                                                    consent_basis="no consent recorded yet"), ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
        with pytest.raises(HTTPException) as e:
            await media_api.list_contract_synthesis_artifacts(client_id=people["b"]["client"], state_code=None,
                                                              ctx=ctx("a"))
        assert e.value.status_code == 404
        listed = await media_api.list_contract_synthesis_artifacts(client_id=people["b"]["client"], state_code=None,
                                                                   ctx=ctx("c"))
        assert [x["id"] for x in listed["artifacts"]] == [rows["contract_synthesis_artifacts"]["b"]]
        with pytest.raises(HTTPException) as e:
            await media_api.download_contract_synthesis_artifact(
                uuid.UUID(rows["contract_synthesis_artifacts"]["b"]), expiration_seconds=60, ctx=ctx("a"))
        assert e.value.status_code == 404
        # The inbound call on A's own line from B's client is B's record now.
        assert await inbound_voice.get_inbound_call(ctx("a"), rows["inbound_voice_calls"]["b"]) is None
        assert await inbound_voice.get_inbound_call(ctx("a"), rows["inbound_voice_calls"]["a"]) is not None
        assert {x["id"] for x in await inbound_voice.list_inbound_calls(ctx("a"))} == kinds(
            "inbound_voice_calls", "a", "pool")
        # A plan on a contact the creator (C) may now only view: 403, not 404;
        # anyone who cannot see it: 404.
        enr_b = rows["smart_plan_enrollments"]["b"]
        for who, code in (("c", 403), ("a", 404)):
            with pytest.raises(HTTPException) as e:
                await sales_api.pause_enrollment(enr_b, ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
        for who, code in (("c", 403), ("a", 404)):
            with pytest.raises(HTTPException) as e:
                await telephony_api.prepare_agent_call(
                    telephony_api.AgentCallPrepare(contact_id=str(people["b"]["contact"])), ctx=ctx(who))
            assert e.value.status_code == code, (who, e.value.detail)
        async with connection.tenant_tx(ctx("c")) as conn:
            assert await record_access.linked_access_level(conn, "agent_call_intents",
                                                           rows["agent_call_intents"]["b"]) == "view"
            assert await record_access.linked_access_level(conn, "agent_call_intents",
                                                           rows["agent_call_intents"]["a"]) is None

        # ── compliance stays brokerage-wide ────────────────────────────────
        noon_et = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
        for phone in (phone_b, phone_cap, phone_voice):
            for channel in (Channel.SMS, Channel.VOICE):
                await ConsentLedger.record_consent(
                    ctx("b"), contact=phone, channel=channel, consent_type="express_written", state_code="DE",
                    proof_source="matrix", proof_text="signed form", client_id=str(people["b"]["client"]))

        # 1. B's client texts STOP to B's line — the real inbound path.
        message_id = await messaging_data.record_inbound_message(
            tenant_id=tenant, agent_id=agent["b"], provider="telnyx", provider_message_id=f"m-{tag}-stop",
            from_e164=phone_b, to_e164="+13025550198", text="STOP", media=[], opted_out=True)
        await ConsentLedger.suppress(ctx("b"), contact=phone_b, channel="*", reason="stop_keyword",
                                     source_text="STOP")  # exactly as messaging_api.telnyx_webhook does
        linked = await admin.fetchrow("SELECT client_id, contact_id FROM sms_messages WHERE id=$1",
                                      uuid.UUID(message_id))
        assert linked["client_id"] == people["b"]["client"] and linked["contact_id"] == people["b"]["contact"]
        async with connection.tenant_tx(ctx("a")) as conn:  # A cannot read B's thread...
            assert await conn.fetchval("SELECT count(*) FROM sms_messages WHERE id=$1", uuid.UUID(message_id)) == 0
        decision = await guard_outreach(ctx("a"), channel=Channel.SMS, contact=phone_b, state_code="DE",
                                        now_utc=noon_et, tz_name="America/New_York", log=False)
        assert not decision.allowed  # ...and is still blocked by it
        assert decision.blockers == ("contact is on the do-not-contact / opt-out list",), decision.blockers

        # 2. The voice frequency cap counts a colleague's calls.
        for _ in range(3):
            d = await guard_outreach(ctx("b"), channel=Channel.VOICE, contact=phone_cap, state_code="DE",
                                     now_utc=noon_et, tz_name="America/New_York", voice_mode=VoiceMode.AI,
                                     recording_enabled=False)
            assert d.allowed, d.blockers
        d = await guard_outreach(ctx("a"), channel=Channel.VOICE, contact=phone_cap, state_code="DE",
                                 now_utc=noon_et, tz_name="America/New_York", voice_mode=VoiceMode.AI,
                                 recording_enabled=False, log=False)
        assert not d.allowed and d.blockers == ("frequency cap reached (3/3 calls in 24h)",), d.blockers

        # 3. "Stop calling me" on an inbound call reaches the gate (it only
        #    flagged that one contact's nurture before this slice).
        key = inbound_voice._tenant_key(tenant)
        async with connection.tenant_tx(inbound_voice._tenant_context(tenant, agent["b"])) as conn:
            ciphertext = await encrypt_pii(conn, "(302) 555-0103", key)
            await inbound_voice._record_voice_opt_out(conn, tenant, ciphertext, key)
        for channel in (Channel.SMS, Channel.VOICE):
            d = await guard_outreach(ctx("a"), channel=channel, contact=phone_voice, state_code="DE",
                                     now_utc=noon_et, tz_name="America/New_York", log=False,
                                     voice_mode=VoiceMode.AGENT, recording_enabled=False)
            assert not d.allowed and "contact is on the do-not-contact / opt-out list" in d.blockers, d.blockers
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
