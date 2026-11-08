"""A sealing batch changes at most the rows it was asked for (0157, L17-24),
even when PostgreSQL picks the nested-loop plan that re-runs the batch's
locking subquery per outer row. Against real PostgreSQL (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

The nested loop is forced (enable_hashjoin / enable_mergejoin off) so the
plan the small-table statistics produced by chance is produced every time.
The old statement shape is run first as a reported control (whether it
over-runs also depends on scan order, so it is not asserted).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "a1" * 32
ROWS, LIMIT = 6, 2
FORCE_NESTED_LOOP = ("SET LOCAL enable_hashjoin = off", "SET LOCAL enable_mergejoin = off",
                     "SET LOCAL enable_material = off")

OLD_SHAPE = """
    UPDATE client_notes
       SET body_ciphertext = pgp_sym_encrypt(body, $2::text, 's2k-mode=1'), body = ''
     WHERE id IN (SELECT id FROM client_notes
                   WHERE tenant_id = $1::uuid AND body_ciphertext IS NULL AND body <> ''
                   ORDER BY id LIMIT $3
                   FOR UPDATE SKIP LOCKED)
    RETURNING id
"""


def test_every_sealing_batch_is_bounded(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901
    import asyncpg

    import backfill_client_notes
    import backfill_sealed_text
    import sealed_text
    from client_note_crypto import _key as note_key
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = str(uuid.uuid4())
    client = uuid.uuid4()
    platform = TenantContext(agent_id="bounded-test", tenant_id=tenant, role=Role.PLATFORM_ADMIN)

    async def forced(sql: str, *args, fetch: bool = True):
        async with connection.tenant_tx(platform) as conn:
            for s in FORCE_NESTED_LOOP:
                await conn.execute(s)
            return await (conn.fetch(sql, *args) if fetch else conn.fetchval(sql, *args))

    async def plaintext_notes() -> int:
        return await admin.fetchval("SELECT count(*) FROM client_notes WHERE tenant_id=$1::uuid "
                                    "AND body_ciphertext IS NULL AND body <> ''", tenant)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Bounded')",
                            tenant, f"bounded-{tenant[:8]}")
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'Bounded')",
                            client, tenant)
        for i in range(ROWS * 2):
            await admin.execute("INSERT INTO client_notes (tenant_id, client_id, body, author_id) "
                                "VALUES ($1,$2,$3,'agent-bounded')", tenant, client, f"note {i}")
            await admin.execute(
                "INSERT INTO user_interactions (user_id, tenant_id, role, content) "
                "VALUES ('agent-bounded',$1,'user',$2)", tenant, f"turn {i}")
            await admin.execute(
                "INSERT INTO interaction_logs (tenant_id, client_id, actor_role, interaction_type, subject, "
                "payload) VALUES ($1,$2,'agent','email',$3,'{\"body\": \"b\"}'::jsonb)",
                uuid.UUID(tenant), client, f"subject {i}")

        # Control: when the planner re-runs the locking subquery per outer row
        # (a nested-loop semi join — what small-table statistics produce), the
        # old shape over-runs. With other statistics it may put the subquery on
        # the outer side, which happens to stay bounded; the fixed shape must be
        # bounded either way, and the control is asserted only when it applies.
        plan = "\n".join(r[0] for r in await forced("EXPLAIN " + OLD_SHAPE, tenant, note_key(tenant), LIMIT))
        before = await plaintext_notes()
        old = await forced(OLD_SHAPE, tenant, note_key(tenant), LIMIT)
        # Whether it over-runs also depends on the outer scan meeting rows in id
        # order, so the control is reported, not asserted (the reproduction is
        # in the L17-24 ledger row: 2 asked, 12 sealed by a 0154 function).
        print(f"control: old shape changed {len(old)} row(s) for LIMIT {LIMIT}; "
              f"rescanning plan: {'Semi Join' in plan}")
        assert await plaintext_notes() == before - len(old)

        # The fixed client-note batch: exactly LIMIT.
        before = await plaintext_notes()
        done = await forced(backfill_client_notes._SEAL, tenant, note_key(tenant), LIMIT)
        assert len(done) == LIMIT and await plaintext_notes() == before - LIMIT
        done = await forced(backfill_client_notes._UNSEAL, tenant, note_key(tenant), LIMIT)
        assert len(done) == LIMIT

        # Every direct statement in backfill_sealed_text is the bounded shape.
        for seal, unseal in backfill_sealed_text._DIRECT.values():
            for sql in (seal, unseal):
                assert "picked AS MATERIALIZED" in sql, sql[:80]
                assert ("IN (SELECT id FROM picked)" in sql
                        or "IN (SELECT user_id FROM picked)" in sql), sql[:80]
                assert sql.count("SKIP LOCKED") == 1

        # The 0157 functions: exactly LIMIT per call, sealing and unsealing.
        key = sealed_text.key_for(tenant)
        for fn, tenant_arg in (("seal_user_interactions", "$1::text"), ("seal_interaction_logs", "$1::uuid")):
            for unseal in (False, True):
                n = await forced(f"SELECT {fn}({tenant_arg}, $2::text, $3::int, $4::boolean)",
                                 tenant, key, LIMIT, unseal, fetch=False)
                assert n == LIMIT, (fn, unseal, n)
        sealed_turns = await admin.fetchval(
            "SELECT count(*) FROM user_interactions WHERE tenant_id=$1 AND content_ciphertext IS NOT NULL", tenant)
        assert sealed_turns == 0  # sealed LIMIT, then unsealed LIMIT
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            await admin.execute("DELETE FROM user_interactions WHERE tenant_id = $1", tenant)
            for table in ("interaction_logs", "client_notes", "clients"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
