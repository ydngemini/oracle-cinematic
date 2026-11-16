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
        tenant = str(uuid.uuid4())
        tag = uuid.uuid4().hex[:8]
        who = {k: f"{k}-{tag}@pull.test" for k in ("owner", "a", "b")}
        client = {k: uuid.uuid4() for k in ("a", "erased")}
        lead = {k: uuid.uuid4() for k in ("pool", "erased")}

        def ctx(k):
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
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Voice pull test')",
                                tenant, f"vp-{tenant[:8]}")
            for k, role in (("owner", "broker_owner"), ("a", "agent"), ("b", "agent")):
                await admin.execute(
                    "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
                    "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y')", uuid.uuid4(), tenant, who[k], role)
            for k in ("a", "erased"):
                await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,$3,$4)",
                                    client[k], tenant, f"Client {k}", who["a"])
            insert_lead = ("INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, "
                           "address) VALUES ($1,$2,$3,'DE',50,$4,'2 Test Rd')")
            await admin.execute(insert_lead, lead["pool"], tenant, f"VP-{tag}", None)
            await admin.execute(insert_lead, lead["erased"], tenant, f"VE-{tag}", client["erased"])
            insert_call = ("INSERT INTO live_call_sessions (tenant_id, client_id, lead_id, consent_recorded, "
                           "consent_basis, transcript_status, started_at, created_by) "
                           "VALUES ($1,$2,$3,true,'verbal','active',now(),$4) RETURNING id")
            private = await admin.fetchval(insert_call, tenant, client["a"], None, who["a"])
            pool = await admin.fetchval(insert_call, tenant, None, lead["pool"], who["b"])
            erased = await admin.fetchval(insert_call, tenant, None, lead["erased"], who["a"])
            await admin.execute("DELETE FROM clients WHERE id = $1", client["erased"])  # erasure: lead -> pool

            got = {
                "private": {k: await read(k, private) for k in who},
                "pool": {k: await read(k, pool) for k in who},
                "erased": {k: await read(k, erased) for k in who},
                "latest_for_owner": await read("owner", None),
                "missing": await record_access.may_read_call_content(tenant, uuid.uuid4(), who["a"]),
            }
            return got, {"private": str(private), "pool": str(pool), "erased": str(erased)}
        finally:
            await admin.execute("SET session_replication_role = replica")
            try:
                for table in ("live_call_sessions", "leads", "clients", "users"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
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
    # "Latest active" never hands the owner a pool call they did not make.
    assert got["latest_for_owner"] == ids["private"], got["latest_for_owner"]
    assert got["missing"] is False
