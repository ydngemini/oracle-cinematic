"""Live-call content over the PULL paths follows the record, fail-closed
(privacy review of PR #77; owner decision 2026-11-16), against real
PostgreSQL with FORCE RLS as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

GET /api/voice/telemetry and the voice-session socket used to admit anyone
who could read the call under RLS: for a pool lead, or a lead whose private
client was erased (seller_client_id ON DELETE SET NULL, readable by every
member under 0151), any member could read the transcript, counter offer, MAO
and objection draft — and with no session_id the REST feed handed out the
latest active call. Now: the record's audience, else the member who created
the call; a missing session, no one.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import textwrap
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
live = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_socket_checks_call_content_before_its_transaction():
    """server.voice_telemetry_websocket calls may_read_call_content before
    opening the caller's tenant_tx (no nested pool connections) and before
    any snapshot is sent."""
    import server

    tree = ast.parse(textwrap.dedent(inspect.getsource(server.voice_telemetry_websocket)))
    calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    check = min(line for line, name in calls if name.endswith("may_read_call_content"))
    tx = min(line for line, name in calls if name == "tenant_tx")
    send = min(line for line, name in calls if name.endswith("send_json"))
    assert check < tx < send
    # The answer is kept for everyone, platform admins included (L03-22) ...
    assigns = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
               and "may_read_call_content" in ast.unparse(node.value)]
    assert [ast.unparse(t) for a in assigns for t in a.targets] == ["may_read_content"]
    # ... and the refusal is on a NEGATED check that closes the socket: an
    # inverted check (admit the refused, refuse the admitted) fails here. Only
    # a platform admin passes without content.
    guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
              and "close(code=4404)" in ast.unparse(node.body[0])
              and "may_read_content" in ast.unparse(node.test)]
    assert len(guards) == 1
    test = guards[0].test
    assert isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And), ast.unparse(test)
    assert all(isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not) for v in test.values), ast.unparse(test)
    assert sorted(ast.unparse(v.operand) for v in test.values) == ["ctx.is_platform_admin", "may_read_content"]


@live
def test_call_content_pull_paths_fail_closed_live():
    from fastapi import HTTPException

    async def go():
        import asyncpg

        import record_access
        import voice_intel
        from db import connection
        from tenancy import Role, TenantContext

        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=2)
        tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
        tag = uuid.uuid4().hex[:8]
        who = {k: f"{k}-{tag}@pull.test" for k in ("owner", "a", "b")}
        client = {k: uuid.uuid4() for k in ("a", "erased")}
        lead = {k: uuid.uuid4() for k in ("pool", "erased", "foreign")}
        contact_a = uuid.uuid4()

        member_admin = f"padmin-{tag}@pull.test"

        def ctx(k):
            if k == "ops":
                return TenantContext(agent_id=f"ops-{tag}@platform.test", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
            if k == "member-admin":
                return TenantContext(agent_id=member_admin, tenant_id=tenant, role=Role.PLATFORM_ADMIN)
            if k == "B-upper":
                return TenantContext(agent_id=who["b"].upper(), tenant_id=tenant, role=Role.AGENT)
            role = Role.BROKER_OWNER if k == "owner" else Role.AGENT
            return TenantContext(agent_id=who[k], tenant_id=tenant, role=role)

        async def read(k, session):
            try:
                out = await voice_intel.read_voice_telemetry(session_id=session, after_event_id=0, limit=100,
                                                            ctx=ctx(k))
                return out["session_id"]
            except HTTPException as error:
                return error.status_code

        try:
            for t, slug in ((tenant, "vp"), (other, "vp-other")):
                await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Voice pull test')",
                                    t, f"{slug}-{t[:8]}")
            for k, role in (("owner", "broker_owner"), ("a", "agent"), ("b", "agent")):
                await admin.execute(
                    "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
                    "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y')", uuid.uuid4(), tenant, who[k], role)
            # A platform admin whose account belongs to this brokerage: in the
            # record audience by the normal rule (0145 lists the tenant's
            # platform_admin users), so a reader of a private client's call.
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
                "VALUES ($1,$2,$3,'platform_admin',$3,'scrypt$x$y')", uuid.uuid4(), tenant, member_admin)
            for k in ("a", "erased"):
                await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                                    client[k], tenant, f"Client {k}", who["a"])
            insert_lead = ("INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, "
                           "address) VALUES ($1,$2,$3,'DE',50,$4,'2 Test Rd')")
            await admin.execute(insert_lead, lead["pool"], tenant, f"VP-{tag}", None)
            await admin.execute(insert_lead, lead["erased"], tenant, f"VE-{tag}", client["erased"])
            await admin.execute(insert_lead, lead["foreign"], other, f"VF-{tag}", None)
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'voice-pull','pending_encryption')", contact_a, tenant, who["a"])
            insert_call = ("INSERT INTO live_call_sessions (tenant_id, client_id, lead_id, consent_recorded, "
                           "consent_basis, transcript_status, started_at, created_by) "
                           "VALUES ($1,$2,$3,true,'verbal','active',now(),$4) RETURNING id")
            private = await admin.fetchval(insert_call, tenant, client["a"], None, who["a"])
            pool = await admin.fetchval(insert_call, tenant, None, lead["pool"], who["b"])
            erased = await admin.fetchval(insert_call, tenant, None, lead["erased"], who["a"])
            foreign = await admin.fetchval(insert_call, tenant, None, lead["foreign"], who["a"])
            contact_only = await admin.fetchval(
                "INSERT INTO live_call_sessions (tenant_id, contact_id, consent_recorded, consent_basis, "
                "transcript_status, started_at, created_by) VALUES ($1,$2,true,'verbal','active',now(),$3) "
                "RETURNING id", tenant, contact_a, who["b"])
            await admin.execute("DELETE FROM clients WHERE id = $1", client["erased"])  # erasure: lead -> pool
            # Call content on the pool call and the private call (legacy
            # plaintext excerpt; the sealed shape is test_sealed_text_live's).
            insert_event = ("INSERT INTO negotiation_events (tenant_id, call_session_id, event_type, "
                            "transcript_excerpt, counter_offer, arv, rehab, mao, threshold, payload, "
                            "model_version, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,'v',$11)")
            for call in (pool, private):
                await admin.execute(insert_event, tenant, call, "consent", None, None, None, None, None, None,
                                    '{"consent_recorded": true, "basis": "verbal Pat Owner"}', who["b"])
                await admin.execute(
                    insert_event, tenant, call, "counter_offer", "Pat Owner: 251357 is my number",
                    251357, 410000, 41733, 233911, "amber",
                    '{"speaker": "CLIENT", "is_final": true, "guidance": {"objection_draft": '
                    '"Pat Owner, the roof needs 41733"}, "mao_unavailable_reason": "Pat Owner 302-555-0147"}',
                    who["b"])

            got = {
                "private": {k: await read(k, private) for k in who},
                "pool": {k: await read(k, pool) for k in who},
                "erased": {k: await read(k, erased) for k in who},
                "latest_for_owner": await read("owner", None),
                "contact_only": {k: await read(k, contact_only) for k in who},
                "foreign": {k: await read(k, foreign) for k in who},
                "pool_upper_case_creator": await read("B-upper", pool),
                "pool_platform_admin": await read("ops", pool),
                "missing": await record_access.may_read_call_content(tenant, uuid.uuid4(), who["a"]),
                # L03-22: what each reader actually gets back.
                "ops_pool_feed": await voice_intel.read_voice_telemetry(
                    session_id=pool, after_event_id=0, limit=100, ctx=ctx("ops")),
                "ops_pool_paged": await voice_intel.read_voice_telemetry(
                    session_id=pool, after_event_id=0, limit=1, ctx=ctx("ops")),
                "ops_latest": await voice_intel.read_voice_telemetry(
                    session_id=None, after_event_id=0, limit=100, ctx=ctx("ops")),
                "creator_pool_feed": await voice_intel.read_voice_telemetry(
                    session_id=pool, after_event_id=0, limit=100, ctx=ctx("b")),
                "member_admin_private_feed": await voice_intel.read_voice_telemetry(
                    session_id=private, after_event_id=0, limit=100, ctx=ctx("member-admin")),
                "member_admin_pool_feed": await voice_intel.read_voice_telemetry(
                    session_id=pool, after_event_id=0, limit=100, ctx=ctx("member-admin")),
                "member_admin_socket": await record_access.may_read_call_content(tenant, private, member_admin),
                "ops_socket": await record_access.may_read_call_content(tenant, pool, f"ops-{tag}@platform.test"),
            }
            async with connection.tenant_tx(ctx("ops")) as conn:
                rows = await voice_intel.load_session_event_metadata(conn, str(pool), limit=100)
                session = await conn.fetchrow(
                    "SELECT consent_recorded, transcript_status FROM live_call_sessions WHERE id=$1", pool)
            got["ops_hydrate"] = voice_intel.hydrate_metadata_frame(str(pool), session, rows)
            return got, {"private": str(private), "pool": str(pool), "erased": str(erased),
                         "contact_only": str(contact_only)}
        finally:
            await admin.execute("SET session_replication_role = replica")
            try:
                for t in (tenant, other):
                    for table in ("negotiation_events", "live_call_sessions", "agent_contacts", "leads",
                                  "clients", "users"):
                        await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                    await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
            finally:
                await admin.execute("SET session_replication_role = origin")
                await admin.close()
                await connection.close_pool()

    got, ids = asyncio.run(go())
    # A private client's call: its audience (assignee + broker owner); a colleague is told 404.
    assert got["private"] == {"owner": ids["private"], "a": ids["private"], "b": 404}, got["private"]
    # A pool lead's call: only the member who created it — not the broker owner, not a colleague.
    assert got["pool"] == {"owner": 404, "a": 404, "b": ids["pool"]}, got["pool"]
    # An erased client's lead (now a pool lead): only the call's creator.
    assert got["erased"] == {"owner": 404, "a": ids["erased"], "b": 404}, got["erased"]
    # "Latest active" is the NEWEST call the owner may read (the contact-only
    # call, created last) — never the newer-than-private pool or erased calls.
    assert got["latest_for_owner"] == ids["contact_only"], got["latest_for_owner"]
    assert got["latest_for_owner"] not in (ids["pool"], ids["erased"])
    assert got["missing"] is False
    # A contact-only call follows the contact (its agent + the broker owner). B
    # created the call, but the record HAS a specific audience, so B is refused.
    assert got["contact_only"] == {"owner": ids["contact_only"], "a": ids["contact_only"], "b": 404}, \
        got["contact_only"]
    # A call naming another brokerage's lead: no one here may read it, not even its creator.
    assert set(got["foreign"].values()) == {404}, got["foreign"]
    # Agent ids compare case-insensitively; the platform admin still sees the call ...
    assert got["pool_upper_case_creator"] == ids["pool"]
    assert got["pool_platform_admin"] == ids["pool"]

    # ... but outside its audience reads operational metadata only (L03-22):
    # event types, times and flags; never the transcript, amounts, threshold,
    # guidance, consent basis or anything a reason text said.
    feed = got["ops_pool_feed"]
    assert feed["content_withheld"] is True and feed["session_id"] == ids["pool"]
    assert [e["event_type"] for e in feed["events"]] == ["consent", "counter_offer"]
    assert feed["events"][0]["consent_recorded"] is True and feed["events"][1]["mao_unavailable"] is True
    assert feed["next_event_id"] == feed["events"][-1]["event_id"]
    assert got["ops_pool_paged"]["next_event_id"] == feed["events"][0]["event_id"], "the cursor still pages"
    allowed = {"type", "version", "session_id", "event_id", "event_type", "created_at", "content_withheld",
               "consent_recorded", "mao_unavailable"}
    for event in feed["events"] + got["ops_hydrate"]["events"]:
        assert set(event) <= allowed | {"id"}, set(event) - allowed
    for out in (feed, got["ops_latest"], got["ops_hydrate"]):
        text = repr(out)
        for content in ("Pat", "251357", "410000", "41733", "233911", "amber", "verbal", "302-555"):
            assert content not in text, (content, out)
    assert got["ops_latest"]["content_withheld"] is True, "the newest call it sees: metadata only too"
    assert got["ops_hydrate"]["content_withheld"] is True and got["ops_socket"] is False
    assert [e["event_type"] for e in got["ops_hydrate"]["events"]] == ["consent", "counter_offer"]
    # The creator reads the content as before.
    assert got["creator_pool_feed"].get("content_withheld") is None
    assert got["creator_pool_feed"]["events"][1]["counter_offer"] == 251357.0
    # A platform admin whose account is in the brokerage is in a private
    # client's audience by the normal rule: content, on both pull paths ...
    private_feed = got["member_admin_private_feed"]
    assert private_feed.get("content_withheld") is None and got["member_admin_socket"] is True
    assert private_feed["events"][1]["transcript"]["text"] == "Pat Owner: 251357 is my number"
    # ... but not a pool call it did not create (metadata only).
    assert got["member_admin_pool_feed"]["content_withheld"] is True
