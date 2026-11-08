"""The deal list pages authorized ids by key (0162, T16.05, AC16.03, L16-04),
against real PostgreSQL with FORCE RLS, as the request login (skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Pins: paging with next_cursor returns every deal the caller may read exactly
once, newest first, for the owner and for an agent who cannot read a
colleague's private client's deals; a deal inserted between two pages is not
re-shown and shifts nothing (OFFSET did); the page query plans on
idx_transactions_page under the tenant wall beside a big brokerage, with no
sequential scan of transactions; a malformed cursor is a 422.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

SMALL_DEALS, BIG_DEALS, PAGE = 230, 4000, 50


def test_the_deal_list_pages_authorized_ids_by_key():
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import portfolio_api
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    small, big = str(uuid.uuid4()), str(uuid.uuid4())
    owner = TenantContext(agent_id=f"owner-{small[:8]}@page.test", tenant_id=small, role=Role.BROKER_OWNER)
    agent = TenantContext(agent_id=f"agent-a-{small[:8]}@page.test", tenant_id=small, role=Role.AGENT)
    colleague = f"agent-b-{small[:8]}@page.test"

    async def walk(ctx) -> list[str]:
        seen, cursor = [], None
        while True:
            out = await portfolio_api.list_transactions(
                transaction_status=None, property_source=None, client_id=None,
                limit=PAGE, offset=0, after=cursor, ctx=ctx)
            seen.extend(t["id"] for t in out["transactions"])
            cursor = out["next_cursor"]
            if not cursor:
                return seen

    async def visible(ctx) -> list[str]:
        async with connection.tenant_tx(ctx) as conn:
            rows = await conn.fetch("SELECT id FROM transactions ORDER BY updated_at DESC, id DESC")
        return [str(r["id"]) for r in rows]

    try:
        for t, name in ((small, "Paging small"), (big, "Paging big")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                uuid.UUID(t), f"page-{t[:8]}", name)
        for login, role in ((owner.agent_id, "broker_owner"), (agent.agent_id, "agent"), (colleague, "agent")):
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,$3,$2)",
                                uuid.UUID(small), login, role)
        private = uuid.uuid4()
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1, $2, 'Colleague private', $3)",
            private, uuid.UUID(small), colleague)
        # Distinct timestamps, a few ties (same updated_at) to exercise the id tiebreak.
        await admin.execute(
            "INSERT INTO transactions (tenant_id, state_code, updated_at, client_id, client_party_role, created_by) "
            "SELECT $1::uuid, 'DE', now() - make_interval(mins => (g / 3)), "
            "       CASE WHEN g % 7 = 0 THEN $2::uuid END, CASE WHEN g % 7 = 0 THEN 'buyer' END, $3 "
            "  FROM generate_series(1, $4) g",
            uuid.UUID(small), private, colleague, SMALL_DEALS)
        await admin.execute(
            "INSERT INTO transactions (tenant_id, state_code, updated_at) "
            "SELECT $1::uuid, 'DE', now() - make_interval(secs => g) FROM generate_series(1, $2) g",
            uuid.UUID(big), BIG_DEALS)
        await admin.execute("ANALYZE transactions")

        # ── every readable deal exactly once, in order, for owner and agent ──
        owner_all = await visible(owner)
        assert len(owner_all) == SMALL_DEALS
        assert await walk(owner) == owner_all
        agent_all = await visible(agent)
        assert 0 < len(agent_all) < SMALL_DEALS, "the agent cannot read the colleague's private client's deals"
        assert await walk(agent) == agent_all

        # ── a deal added between pages is not re-shown and shifts nothing ────
        first = await portfolio_api.list_transactions(
            transaction_status=None, property_source=None, client_id=None,
            limit=PAGE, offset=0, after=None, ctx=owner)
        await admin.execute("INSERT INTO transactions (tenant_id, state_code) VALUES ($1, 'DE')", uuid.UUID(small))
        second = await portfolio_api.list_transactions(
            transaction_status=None, property_source=None, client_id=None,
            limit=PAGE, offset=0, after=first["next_cursor"], ctx=owner)
        ids1 = [t["id"] for t in first["transactions"]]
        ids2 = [t["id"] for t in second["transactions"]]
        assert not set(ids1) & set(ids2)
        assert ids2 == owner_all[PAGE:2 * PAGE]
        assert second["total"] == SMALL_DEALS + 1

        # ── the page query is index-driven under the tenant wall ─────────────
        def nodes(plan):
            stack = [plan[0]["Plan"] if isinstance(plan, list) else plan]
            while stack:
                n = stack.pop()
                yield n
                stack.extend(n.get("Plans", []))

        async def page_plan(ctx, tenant):
            async with connection.tenant_tx(ctx) as conn:
                raw = await conn.fetchval(
                    "EXPLAIN (FORMAT JSON) " + portfolio_api._TRANSACTION_PAGE_SQL,
                    tenant, None, None, None, PAGE + 1, None, None, 0)
            return list(nodes(json.loads(raw) if isinstance(raw, str) else raw))

        big_owner = TenantContext(agent_id=f"owner-{big[:8]}@page.test", tenant_id=big, role=Role.BROKER_OWNER)
        for ctx, tenant in ((owner, small), (agent, small), (big_owner, big)):
            plan = await page_plan(ctx, tenant)
            scans = [n for n in plan if n.get("Relation Name") == "transactions"]
            assert scans and all(n["Node Type"] in ("Index Scan", "Index Only Scan", "Bitmap Heap Scan")
                                 for n in scans), scans
        # A big brokerage walks the page index in order: no sort of all its deals.
        plan = await page_plan(big_owner, big)
        assert any(n.get("Index Name") == "idx_transactions_page" for n in plan), plan
        assert not any(n["Node Type"] == "Sort" for n in plan), plan

        # ── a bad cursor is the caller's error ───────────────────────────────
        with pytest.raises(HTTPException) as bad:
            await portfolio_api.list_transactions(
                transaction_status=None, property_source=None, client_id=None,
                limit=PAGE, offset=0, after="not-a-cursor", ctx=owner)
        assert bad.value.status_code == 422
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for t in (small, big):
                for table in ("transactions", "clients", "users"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
