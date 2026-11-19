"""AI32-01: update_assistant(..., unless_status="completed") leaves a turn
that already completed untouched, on real PostgreSQL through the app pool as
the agent. Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_a_failure_cannot_overwrite_a_completed_turn(monkeypatch):
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "f" * 64)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    from ai_chat_store import update_assistant
    from db import connection
    from tenancy import Role, TenantContext

    tenant, agent = str(uuid.uuid4()), "agent-ai32@example.test"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'AI32 live')",
                            tenant, f"ai32-{tenant[:8]}")

        async def turn(status):
            return str(await admin.fetchval(
                "INSERT INTO ai_chat_messages (tenant_id, user_id, role, content_ciphertext, status, request_id) "
                "VALUES ($1, $2, 'assistant', '\\x00'::bytea, $3, gen_random_uuid()) RETURNING id",
                tenant, agent, status))

        async def status_of(mid):
            return await admin.fetchval("SELECT status FROM ai_chat_messages WHERE id = $1::uuid", mid)

        ctx = TenantContext(agent_id=agent, tenant_id=tenant, role=Role.AGENT)
        done = await turn("completed")
        changed = await update_assistant(ctx, done, content="Neoh couldn't complete that response.",
                                         status_value="failed", error_code="AI_RESPONSE_UNAVAILABLE",
                                         unless_status="completed")
        assert changed is False and await status_of(done) == "completed"

        streaming = await turn("streaming")
        changed = await update_assistant(ctx, streaming, content="Neoh couldn't complete that response.",
                                         status_value="failed", error_code="AI_RESPONSE_UNAVAILABLE",
                                         unless_status="completed")
        assert changed is True and await status_of(streaming) == "failed"

        # Without the guard the update behaves as before.
        assert await update_assistant(ctx, done, content="x", status_value="completed") is True
    finally:
        await admin.execute("DELETE FROM ai_chat_messages WHERE tenant_id = $1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await admin.close()
        await connection.close_pool()
