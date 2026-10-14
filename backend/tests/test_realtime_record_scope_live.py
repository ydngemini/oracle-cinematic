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
   pool client still reach everyone; the platform-admin firehose gets one
   full copy of each (owner 2026-10-14), the voice-session stream none.
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
                assert [f["type"] for f in socks["firehose"].frames] == expected
                assert all(f["source_tenant"] == tenant for f in socks["firehose"].frames)
                negotiation = socks["owner"].frames[0]
                assert negotiation["counter_offer"] == 250000 and negotiation["call_session_id"] == calls["a"]
                assert socks["c"].frames[2]["transcript"]["text"] == "Call me after five."
            else:
                for who in ("owner", "a", "b", "c"):
                    assert socks[who].types() == expected, (who, socks[who].types())
                assert [f["type"] for f in socks["firehose"].frames] == expected
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

        # The voice-session stream: admitted per session, never mirrored.
        watcher = Socket()
        group = voice_intel.voice_session_group(tenant, calls["a"])
        ws_hub.register(group, watcher)
        try:
            await voice_intel.ingest_voice_telemetry(
                voice_intel.VoiceTelemetryRequest(session_id=calls["a"], transcript_chunk="Thanks.",
                                                  speaker="AGENT"),
                ctx("a"))
        finally:
            ws_hub.unregister(group, watcher)
        assert watcher.types() == ["VOICE_TELEMETRY"]
        # One full copy (through broadcast_record), not a second from the stream.
        assert [f["type"] for f in socks["firehose"].frames] == ["VOICE_TELEMETRY"], socks["firehose"].frames
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
