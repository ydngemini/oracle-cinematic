"""Every deal status change is an immutable event; illegal ones are refused
(0163, T13.02, AC13.01, L13-03), against real PostgreSQL with FORCE RLS, as
the request login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Pins: creating a deal and each legal move (active -> under_contract ->
closed; active -> lost through the real lose route) writes exactly one event
with from, to, version and the acting agent; illegal moves (back to active,
out of a terminal state) are refused by the database and write nothing; the
application can neither write nor edit nor delete an event; the trail reads
only where the deal does; a deleted deal takes its trail with it.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_deal_status_changes_are_immutable_events():
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import portfolio_api
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = str(uuid.uuid4())
    owner = TenantContext(agent_id=f"owner-{tenant[:8]}@deal.test", tenant_id=tenant, role=Role.BROKER_OWNER)
    agent = TenantContext(agent_id=f"agent-{tenant[:8]}@deal.test", tenant_id=tenant, role=Role.AGENT)
    colleague = f"colleague-{tenant[:8]}@deal.test"

    async def events(deal) -> list[tuple]:
        rows = await admin.fetch(
            "SELECT from_status, to_status, deal_version, actor, source FROM transaction_status_events "
            "WHERE transaction_id=$1 ORDER BY occurred_at, deal_version", deal)
        return [tuple(r) for r in rows]

    async def as_owner(sql, *args):
        async with connection.tenant_tx(owner) as conn:
            return await conn.fetchval(sql, *args)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Deal events')",
                            uuid.UUID(tenant), f"events-{tenant[:8]}")
        for login, role in ((owner.agent_id, "broker_owner"), (agent.agent_id, "agent"), (colleague, "agent")):
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,$3,$2)",
                                uuid.UUID(tenant), login, role)

        # ── create + legal moves, each one event with its actor ─────────────
        deal = await as_owner(
            "INSERT INTO transactions (tenant_id, state_code, created_by) VALUES ($1::uuid,'DE',$2) RETURNING id",
            tenant, owner.agent_id)
        await as_owner("UPDATE transactions SET status='under_contract', version=version+1 WHERE id=$1", deal)
        await as_owner("UPDATE transactions SET notes='kept', version=version+1 WHERE id=$1", deal)  # not a move
        await as_owner("UPDATE transactions SET status='closed', closed_at=now(), version=version+1 WHERE id=$1", deal)
        assert await events(deal) == [
            (None, "active", 1, owner.agent_id, "trigger"),
            ("active", "under_contract", 2, owner.agent_id, "trigger"),
            ("under_contract", "closed", 4, owner.agent_id, "trigger"),
        ]

        # ── illegal moves are refused and write nothing ──────────────────────
        for target in ("active", "lost", "under_contract"):   # out of a terminal state
            with pytest.raises(asyncpg.CheckViolationError, match="illegal deal transition"):
                await as_owner("UPDATE transactions SET status=$2 WHERE id=$1", deal, target)
        other = await as_owner(
            "INSERT INTO transactions (tenant_id, state_code, created_by) VALUES ($1::uuid,'DE',$2) RETURNING id",
            tenant, owner.agent_id)
        await as_owner("UPDATE transactions SET status='under_contract' WHERE id=$1", other)
        with pytest.raises(asyncpg.CheckViolationError):
            await as_owner("UPDATE transactions SET status='active' WHERE id=$1", other)  # falling back: not yet legal
        assert len(await events(deal)) == 3 and len(await events(other)) == 2

        # ── the real lose route records its move ─────────────────────────────
        lost = await as_owner(
            "INSERT INTO transactions (tenant_id, state_code, created_by) VALUES ($1::uuid,'DE',$2) RETURNING id",
            tenant, owner.agent_id)
        await portfolio_api.lose_transaction(
            lost, portfolio_api.TransactionLose(version=1, reason_code="financing", reason="Loan denied"), ctx=owner)
        assert (await events(lost))[-1][:3] == ("active", "lost", 2)
        trail = await portfolio_api.list_transaction_events(lost, ctx=owner)
        assert [e["to_status"] for e in trail["events"]] == ["active", "lost"]

        # ── the application cannot write, edit or delete the trail ───────────
        for sql in ("INSERT INTO transaction_status_events (tenant_id, transaction_id, to_status, deal_version, actor) "
                    "VALUES ($1::uuid, $2, 'closed', 9, 'forged')",
                    "UPDATE transaction_status_events SET to_status='forged' WHERE transaction_id=$2 AND $1::uuid IS NOT NULL",
                    "DELETE FROM transaction_status_events WHERE transaction_id=$2 AND $1::uuid IS NOT NULL"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.tenant_tx(owner) as conn:
                    await conn.execute(sql, tenant, deal)
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await admin.execute("UPDATE transaction_status_events SET actor='x' WHERE transaction_id=$1", deal)

        # ── the trail reads only where the deal does ─────────────────────────
        private = uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, assignee_id) VALUES ($1,$2,'Private',$3)",
                            private, uuid.UUID(tenant), colleague)
        hidden = await as_owner(
            "INSERT INTO transactions (tenant_id, state_code, client_id, client_party_role, created_by) "
            "VALUES ($1::uuid,'DE',$2,'buyer',$3) RETURNING id", tenant, private, colleague)
        async with connection.tenant_tx(agent) as conn:
            assert await conn.fetchval("SELECT count(*) FROM transaction_status_events WHERE transaction_id=$1", hidden) == 0
            assert await conn.fetchval("SELECT count(*) FROM transaction_status_events WHERE transaction_id=$1", deal) == 3
        with pytest.raises(HTTPException) as nope:
            await portfolio_api.list_transaction_events(hidden, ctx=agent)
        assert nope.value.status_code == 404

        # ── a deleted deal takes its trail with it ───────────────────────────
        await admin.execute("DELETE FROM transactions WHERE id=$1", other)
        assert await events(other) == []
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("transaction_status_events", "transactions", "clients", "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
