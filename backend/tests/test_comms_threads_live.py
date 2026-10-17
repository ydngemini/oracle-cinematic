"""The comms rollup is one pass, and answers exactly what it did (L17-25,
L17-22 defect), against real PostgreSQL as the request login (skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Pins: for 300 clients and 6,000 touches (some reached through a client's
house-lead), crm.comms_threads returns the same clients, counts, newest touch
and order as the previous query (kept here as the control); and it is faster
than the control on that bench (both timings printed — the old shape scanned
every touch twice per client).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

CLIENTS, TOUCHES = 300, 6000

OLD_SQL = """
    WITH touches AS (
        SELECT COALESCE(il.client_id, ld.seller_client_id) AS cid,
               il.id, il.interaction_type, il.direction, il.subject,
               left(il.payload->>'body', 80) AS snippet_body, il.created_at
          FROM interaction_logs il
          LEFT JOIN leads ld ON ld.id = il.lead_id
         WHERE COALESCE(il.client_id, ld.seller_client_id) IS NOT NULL
    )
    SELECT c.id, t.id AS last_id, t.created_at, COALESCE(n.cnt, 0) AS cnt
      FROM clients c
      LEFT JOIN LATERAL (
            SELECT id, created_at FROM touches WHERE cid = c.id ORDER BY created_at DESC LIMIT 1
      ) t ON true
      LEFT JOIN LATERAL (SELECT count(*) AS cnt FROM touches WHERE cid = c.id) n ON true
     ORDER BY t.created_at DESC NULLS LAST, c.created_at DESC
     LIMIT 200
"""


def test_the_comms_rollup_is_one_pass_and_unchanged(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-comms-master-key-0123456789abcdef")
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import crm
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = str(uuid.uuid4())
    owner = TenantContext(agent_id=f"owner-{tenant[:8]}@comms.test", tenant_id=tenant, role=Role.BROKER_OWNER)
    rng = random.Random(1725)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Comms')",
                            uuid.UUID(tenant), f"comms-{tenant[:8]}")
        clients = [uuid.uuid4() for _ in range(CLIENTS)]
        await admin.executemany(
            "INSERT INTO clients (id, tenant_id, full_name, created_at) VALUES ($1,$2,$3, now() - make_interval(mins => $4))",
            [(c, uuid.UUID(tenant), f"Client {i}", i) for i, c in enumerate(clients)])
        leads = {}
        for c in clients[:40]:  # some touches arrive through the client's house-lead
            leads[c] = await admin.fetchval(
                "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score, seller_client_id) "
                "VALUES ($1,$2,'DE',0,$3) RETURNING id", uuid.UUID(tenant), f"P-{uuid.uuid4().hex[:10]}", c)
        rows = []
        for i in range(TOUCHES):
            c = clients[rng.randrange(CLIENTS - 30)]  # 30 clients never touched
            via_lead = c in leads and rng.random() < 0.5
            rows.append((uuid.UUID(tenant), None if via_lead else c, leads[c] if via_lead else None,
                         f"subject {i}", f"now() - interval '{rng.randrange(1_000_000)} seconds'"))
        await admin.executemany(
            "INSERT INTO interaction_logs (tenant_id, client_id, lead_id, actor_role, interaction_type, subject, "
            "payload, created_at) VALUES ($1,$2,$3,'agent','email',$4,'{\"body\": \"hello there\"}'::jsonb, "
            "now() - make_interval(secs => $5))",
            [(r[0], r[1], r[2], r[3], rng.randrange(1_000_000)) for r in rows])
        await admin.execute("ANALYZE clients; ANALYZE interaction_logs; ANALYZE leads")

        async with connection.tenant_tx(owner) as conn:
            started = time.perf_counter()
            old = await conn.fetch(OLD_SQL)
            old_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        new = (await crm.comms_threads(ctx=owner))["threads"]
        new_ms = (time.perf_counter() - started) * 1000
        print(f"comms rollup, {CLIENTS} clients x {TOUCHES} touches: old {old_ms:.1f} ms, new {new_ms:.1f} ms")

        assert [str(r["id"]) for r in old] == [t["client"]["id"] for t in new]
        for o, n in zip(old, new):
            assert int(o["cnt"]) == n["count"]
            if o["created_at"] is None:
                assert n["last"] is None
            else:
                assert n["last"]["created_at"] == o["created_at"].isoformat()
        assert sum(t["count"] for t in new) <= TOUCHES
        assert new_ms < old_ms / 2, (new_ms, old_ms)  # measured ~6x locally; 2x leaves room for noisy runners
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("interaction_logs", "leads", "clients"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
