"""Live frames about a client's or contact's call follow the record (CONTRACT
C3.6, ledger L03-08, 0145), against real PostgreSQL with FORCE RLS, as the
application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Two halves:
1. app_record_audience() — who the hub delivers to — agrees, for EVERY
   member and every client/contact combination, with app_linked_access()
   evaluated as that member (the rule the API and RLS use, 0140): owner,
   assignee, view share, edit share, revoked share, expired share, colleague,
   pool, unlinked; another brokerage's agent is never listed.
2. End to end through the real routes and the real hub (in-process sockets,
   no provider, no network): agent B's socket does not receive agent A's
   private-client NEGOTIATION_TELEMETRY / CALL_CONSENT / VOICE_TELEMETRY while
   the broker owner's and the view-share holder's do; the same frames about a
   pool client reach only the member on the call (owner 2026-11-16); the
   platform-admin firehose gets one operational-metadata frame for each —
   never the transcript, offer, ARV/MAO or record ids (audit 27.5, owner
   decision 2026-11-20; before it got a full copy); the voice-session stream
   reaches only the stream sockets of members in the call record's audience
   at push time, so a share revoked mid-call stops the next frame (L03-17
   blocker, owner decision 2026-11-20), and is never mirrored.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


class Socket:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    def types(self) -> list[str]:
        return [f.get("type") for f in self.frames]


def test_live_frames_follow_the_record(monkeypatch):
    # Call and voice telemetry seal the transcript (0152, L17-21): writes need the key.
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-realtime-scope-master-key-0123456789")
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import commands_api
    import record_access
    import voice_intel
    import ws_hub
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    names = ("owner", "a", "b", "c", "d", "e", "f")
    uid = {k: uuid.uuid4() for k in (*names, "x")}
    agent = {k: f"{k}-{tag}@rt.test" for k in (*names, "x")}
    client = {k: uuid.uuid4() for k in ("a", "b", "pool")}
    contact = {k: uuid.uuid4() for k in ("a", "b", "pool")}

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other if who == "x" else tenant, role=role)

    saved = (ws_hub._sockets, ws_hub._user_sockets)
    ws_hub._sockets, ws_hub._user_sockets = ws_hub.defaultdict(set), ws_hub.defaultdict(set)
    try:
        for t, slug in ((tenant, "rt"), (other, "rt-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Realtime scope test')",
                                t, f"{slug}-{t[:8]}")
        for k in (*names, "x"):
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y')",
                uid[k], other if k == "x" else tenant, agent[k], "broker_owner" if k == "owner" else "agent")
        for k in ("a", "b", "pool"):
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                client[k], tenant, f"Client {k}", None if k == "pool" else agent[k])
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'realtime','pending_encryption')",
                contact[k], tenant, None if k == "pool" else agent[k])
        # C views A's client; D edits A's contact; E's share was revoked; F's expired.
        share = ("INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
                 "granted_by, created_at, expires_at, revoked_at) VALUES ($1,$2,$3,$4,$5,'owner',$6,$7,$8)")
        now = await admin.fetchval("SELECT now()")
        from datetime import timedelta

        await admin.execute(share, tenant, "client", client["a"], uid["c"], "view", now, None, None)
        await admin.execute(share, tenant, "contact", contact["a"], uid["d"], "edit", now, None, None)
        await admin.execute(share, tenant, "client", client["a"], uid["e"], "edit", now, None, now)
        await admin.execute(share, tenant, "client", client["a"], uid["f"], "view",
                            now - timedelta(hours=2), now - timedelta(hours=1), None)

        # ── 1. the audience IS app_linked_access, per member ────────────────
        combos = [(cl, co) for cl in (None, client["a"], client["b"], client["pool"])
                  for co in (None, contact["a"], contact["b"], contact["pool"])]
        for cl, co in combos:
            audience = await record_access.linked_audience(tenant, cl, co)
            assert audience is None or agent["x"] not in audience, "another brokerage's agent was listed"
            for who in names:
                async with connection.tenant_tx(ctx(who)) as conn:
                    may = await conn.fetchval("SELECT app_linked_access($1::uuid, $2::uuid, false)", cl, co)
                gets = audience is None or agent[who] in audience
                assert gets == bool(may), (who, cl, co, audience, may)
        assert await record_access.linked_audience(tenant, None, None) is None
        assert await record_access.linked_audience(tenant, client["pool"], contact["pool"]) is None
        assert sorted(await record_access.linked_audience(tenant, client["a"], None)) == sorted(
            [agent["owner"], agent["a"], agent["c"]])
        assert await record_access.linked_audience(tenant, uuid.uuid4(), None) == [], "unknown record: nobody"
        # The answer does not depend on the calling context (a platform-admin
        # caller of the function still lists only the record's brokerage).
        plat = TenantContext(agent_id=f"plat-{tag}", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
        async with connection.tenant_tx(plat) as conn:
            listed = await conn.fetchval("SELECT app_record_audience($1::uuid, NULL)", client["a"])
        assert sorted(listed) == sorted([agent["owner"], agent["a"], agent["c"]])

        # ── 2. end to end ───────────────────────────────────────────────────
        socks = {k: Socket() for k in ("owner", "a", "b", "c", "x", "firehose")}
        for k in ("owner", "a", "b", "c"):
            ws_hub.register(tenant, socks[k], agent[k])
        ws_hub.register(other, socks["x"], agent["a"])  # same id, other brokerage
        ws_hub.register(ws_hub.FIREHOSE_TENANT_ID, socks["firehose"], "ops")

        calls = {}
        for k in ("a", "pool"):
            calls[k] = await admin.fetchval(
                """INSERT INTO live_call_sessions (tenant_id, client_id, consent_recorded, consent_basis,
                                                   transcript_status, started_at, created_by)
                   VALUES ($1,$2,true,'verbal','active',now(),$3) RETURNING id::text""",
                tenant, client[k], agent["a"])

        def clear():
            for s in socks.values():
                s.frames.clear()

        # What was said and computed on these calls; none of it may reach the
        # platform firehose (27.5). mirrored_at is the hub's clock, left out.
        call_content = ("We would take two fifty", "Call me after five", "Thanks.", "250000", "410000",
                        "247000", "Client a", "Client pool", calls["a"], calls["pool"], str(client["a"]))

        def firehose_types() -> list[str]:
            frames = socks["firehose"].frames
            assert all(f["type"] == "FIREHOSE_METADATA" and f["content_withheld"] is True
                       and f["source_tenant"] == tenant for f in frames), frames
            text = json.dumps([{k: v for k, v in f.items() if k != "mirrored_at"} for f in frames])
            leaked = [x for x in call_content if x in text]
            assert not leaked, (leaked, frames)
            return [f["frame_type"] for f in frames]

        for k in ("a", "pool"):
            clear()
            await commands_api.call_telemetry(
                calls[k],
                commands_api.CallTelemetry(transcript_excerpt="We would take two fifty.",
                                           counter_offer=250000, arv=410000, rehab=40000),
                ctx("a"))
            await commands_api.record_call_consent(
                calls[k], commands_api.CallConsent(consent_recorded=True, consent_basis="verbal_on_call"), ctx("a"))
            await voice_intel.ingest_voice_telemetry(
                voice_intel.VoiceTelemetryRequest(session_id=calls[k], transcript_chunk="Call me after five.",
                                                  speaker="CLIENT"),
                ctx("a"))
            expected = ["NEGOTIATION_TELEMETRY", "CALL_CONSENT", "VOICE_TELEMETRY"]
            if k == "a":
                for who in ("owner", "a", "c"):
                    assert socks[who].types() == expected, (who, socks[who].types())
                assert socks["b"].frames == [], "agent B received a colleague's private-client call"
                assert firehose_types() == expected
                negotiation = socks["owner"].frames[0]
                assert negotiation["counter_offer"] == 250000 and negotiation["call_session_id"] == calls["a"]
                assert socks["c"].frames[2]["transcript"]["text"] == "Call me after five."
            else:
                # A pool client's call: sensitive content goes to the member on
                # the call only — never the whole brokerage (owner 2026-11-16).
                assert socks["a"].types() == expected, socks["a"].types()
                for who in ("owner", "b", "c"):
                    assert socks[who].frames == [], (who, "received a pool client's call content")
                assert firehose_types() == expected
            assert socks["x"].frames == [], "another brokerage's socket"

        # B cannot even post to A's call (0140) — and receives nothing for trying.
        clear()
        with pytest.raises(Exception) as denied:
            await commands_api.call_telemetry(
                calls["a"],
                commands_api.CallTelemetry(transcript_excerpt="x", counter_offer=1, arv=2, rehab=0),
                ctx("b"))
        assert getattr(denied.value, "status_code", None) == 404
        assert all(s.frames == [] for s in socks.values())

        # The voice-session stream: each socket registered with its member (as
        # server.voice_telemetry_websocket does); a frame reaches the stream
        # sockets of the call record's audience at push time, never mirrored.
        # B (colleague) and E (revoked share) joined the group but are not in
        # the audience — e.g. access lost since their socket's last re-check.
        group = voice_intel.voice_session_group(tenant, calls["a"])
        watchers = {k: Socket() for k in ("owner", "a", "b", "c", "e")}
        for k, w in watchers.items():
            ws_hub.register(group, w, agent[k])
        pool_group = voice_intel.voice_session_group(tenant, calls["pool"])
        pool_watchers = {k: Socket() for k in ("owner", "a", "b")}
        for k, w in pool_watchers.items():
            ws_hub.register(pool_group, w, agent[k])

        async def say(call: str, text: str) -> None:
            await voice_intel.ingest_voice_telemetry(
                voice_intel.VoiceTelemetryRequest(session_id=call, transcript_chunk=text, speaker="AGENT"),
                ctx("a"))

        try:
            await say(calls["a"], "Thanks.")
            got = {k for k, w in watchers.items() if w.frames}
            assert got == {"owner", "a", "c"}, got
            assert watchers["c"].frames[0]["transcript"]["text"] == "Thanks."
            # One metadata frame (through the dashboards' copy), none from the stream.
            assert firehose_types() == ["VOICE_TELEMETRY"]

            # C's view share is revoked mid-call: the very next frame skips C's
            # stream socket (no waiting for its 60 s re-check).
            for w in watchers.values():
                w.frames.clear()
            await admin.execute("UPDATE record_shares SET revoked_at = now() WHERE resource_id = $1 "
                                "AND grantee_user_id = $2", client["a"], uid["c"])
            await say(calls["a"], "Thanks.")
            got = {k for k, w in watchers.items() if w.frames}
            assert got == {"owner", "a"}, got

            # A pool client's call: only the member who is both the actor and
            # the call's creator (A) — not the broker owner, not B.
            clear()
            await say(calls["pool"], "Thanks.")
            got = {k for k, w in pool_watchers.items() if w.frames}
            assert got == {"a"}, got
        finally:
            for k, w in watchers.items():
                ws_hub.unregister(group, w, agent[k])
            for k, w in pool_watchers.items():
                ws_hub.unregister(pool_group, w, agent[k])
    finally:
        ws_hub._sockets, ws_hub._user_sockets = saved
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for t in (tenant, other):
                for row in tables:
                    cast = "uuid" if row["data_type"] == "uuid" else "text"
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()





def test_sensitive_lead_frames_fail_closed_live():
    """Owner decision 2026-11-16 / audit 27.1, 27.4, against real PostgreSQL
    (FORCE RLS, app role) and the real hub: a transcript/offer frame about a
    lead reaches exactly the lead's audience, and NEVER the whole brokerage —
    pool lead, erased client (ON DELETE SET NULL turns its lead into a pool
    lead), deleted lead or another brokerage's lead. A non-sensitive frame
    about a pool lead keeps its pool audience."""

    class Sock:
        def __init__(self):
            self.frames = []

        async def send_text(self, text):
            self.frames.append(json.loads(text))

    async def go():
        import asyncpg

        import record_access  # noqa: F401  (the hub calls it)
        import ws_hub
        from db import connection

        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=2)
        tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
        tag = uuid.uuid4().hex[:8]
        who = {k: f"{k}-{tag}@lead.test" for k in ("owner", "a", "b", "x")}
        clients = {k: uuid.uuid4() for k in ("a", "erased")}
        lead = {k: uuid.uuid4() for k in ("private", "pool", "erased", "deleted", "foreign")}
        saved = (ws_hub._sockets, ws_hub._user_sockets)
        ws_hub._sockets, ws_hub._user_sockets = ws_hub.defaultdict(set), ws_hub.defaultdict(set)
        socks = {k: Sock() for k in who}
        results = {}
        try:
            for t, slug in ((tenant, "ld"), (other, "ld-other")):
                await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Lead audience test')",
                                    t, f"{slug}-{t[:8]}")
            for k, role in (("owner", "broker_owner"), ("a", "agent"), ("b", "agent"), ("x", "agent")):
                await admin.execute(
                    "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
                    "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y')", uuid.uuid4(), other if k == "x" else tenant, who[k], role)
            for k in ("a", "erased"):
                await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                                    clients[k], tenant, f"Client {k}", who["a"])
            insert = ("INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, "
                      "address) VALUES ($1,$2,$3,'DE',50,$4,'1 Test Rd')")
            await admin.execute(insert, lead["private"], tenant, f"LA-{tag}", clients["a"])
            await admin.execute(insert, lead["pool"], tenant, f"LP-{tag}", None)
            await admin.execute(insert, lead["erased"], tenant, f"LE-{tag}", clients["erased"])
            await admin.execute(insert, lead["deleted"], tenant, f"LD-{tag}", clients["a"])
            await admin.execute(insert, lead["foreign"], other, f"LF-{tag}", None)
            # The private client is erased (its lead becomes a pool lead) and a lead is deleted.
            await admin.execute("DELETE FROM clients WHERE id = $1", clients["erased"])
            assert await admin.fetchval("SELECT seller_client_id FROM leads WHERE id=$1", lead["erased"]) is None
            await admin.execute("DELETE FROM leads WHERE id = $1", lead["deleted"])
            for k in ("owner", "a", "b"):
                ws_hub.register(tenant, socks[k], who[k])
            ws_hub.register(other, socks["x"], who["x"])

            async def deliver(lead_key, actor, sensitive=True):
                for s in socks.values():
                    s.frames.clear()
                await ws_hub.broadcast_record(tenant, {"type": "NEGOTIATION_TELEMETRY", "mao": 231000},
                                              lead_id=str(lead[lead_key]), actor=who[actor],
                                              sensitive=sensitive, mirror=False)
                return {k for k, s in socks.items() if s.frames}

            results["private"] = await deliver("private", "a")
            results["pool"] = await deliver("pool", "b")
            results["erased"] = await deliver("erased", "b")   # not the former assignee
            results["deleted"] = await deliver("deleted", "a")
            results["foreign"] = await deliver("foreign", "a")
            results["pool_non_sensitive"] = await deliver("pool", "b", sensitive=False)
            return results
        finally:
            ws_hub._sockets, ws_hub._user_sockets = saved
            await admin.execute("SET session_replication_role = replica")
            try:
                for t in (tenant, other):
                    for table in ("leads", "clients", "users"):
                        await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                    await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
            finally:
                await admin.execute("SET session_replication_role = origin")
                await admin.close()
                await connection.close_pool()

    got = asyncio.run(go())
    assert got["private"] == {"owner", "a"}, got["private"]          # authorized recipients only
    assert got["pool"] == {"b"}, got["pool"]                         # the member on the call
    assert got["erased"] == {"b"}, got["erased"]                     # erased client: the caller only
    assert got["deleted"] == set(), got["deleted"]                   # deleted lead: nobody
    assert got["foreign"] == set(), got["foreign"]                   # another brokerage's lead: nobody
    assert got["pool_non_sensitive"] == {"owner", "a", "b"}          # ordinary pool frames unchanged
