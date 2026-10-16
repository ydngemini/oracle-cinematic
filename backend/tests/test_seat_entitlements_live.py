"""Exactly the plan's seats can be used (0161, T18.04, AC18.02, L18-05),
against real PostgreSQL with FORCE RLS, as the application role (skipped
without ORACLE_LIVE_DB_ADMIN_DSN).

Pins: with seat_limit 20 and 19 seats used, two activations racing for the
last seat commit exactly one; the 21st invitation acceptance is refused with
409 and the invitation stays usable; suspension frees a seat and reinstating
past the limit is refused; a brokerage with no limit is unaffected; only a
platform admin can change the limit from the application.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

LIMIT = 20


def test_exactly_the_plans_seats_can_be_used():
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException, Response

    import auth
    import brokerage_onboarding as bo
    import seats
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=2, max_size=4)
    tenant, unlimited = str(uuid.uuid4()), str(uuid.uuid4())
    platform = TenantContext(agent_id="seat-test", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    owner = TenantContext(agent_id=f"owner@{tenant[:8]}.test", tenant_id=tenant, role=Role.BROKER_OWNER)

    async def used() -> int:
        return await admin.fetchval(
            "SELECT count(*) FROM users WHERE tenant_id=$1 AND is_active AND role <> 'platform_admin'",
            uuid.UUID(tenant))

    async def add_user(conn, n: int, t: str = tenant, role: str = "agent"):
        return await conn.fetchval(
            "INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1::uuid, $2, $3, $2) RETURNING id",
            t, f"agent{n}-{t[:8]}@seat.test", role)

    owner_id = None

    async def invitation(email: str) -> str:
        raw = secrets.token_urlsafe(24)
        await admin.execute(
            "INSERT INTO brokerage_invitations (token_hash, tenant_id, email, invited_by, invited_by_agent_id, "
            "expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            bo.hash_invitation_token(raw), uuid.UUID(tenant), email, owner_id, owner.agent_id,
            datetime.now(timezone.utc) + timedelta(days=1))
        return raw

    try:
        for t, name in ((tenant, "Seats"), (unlimited, "No limit")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                uuid.UUID(t), f"seat-{t[:8]}", name)
        await admin.execute("UPDATE tenants SET seat_limit=$2 WHERE id=$1", uuid.UUID(tenant), LIMIT)
        owner_id = await add_user(admin, 0, role="broker_owner")
        for n in range(1, LIMIT - 1):
            await add_user(admin, n)
        assert await used() == LIMIT - 1

        # ── two activations race for the last seat: exactly one commits ─────
        first_in = asyncio.Event()
        release = asyncio.Event()

        async def first():
            async with connection.tenant_tx(platform) as conn:
                await add_user(conn, 100)
                first_in.set()
                await release.wait()   # hold the brokerage lock while the second arrives

        async def second():
            await first_in.wait()
            async with connection.tenant_tx(platform) as conn:
                await add_user(conn, 101)

        t1 = asyncio.create_task(first())
        t2 = asyncio.create_task(second())
        try:
            await first_in.wait()
            await asyncio.sleep(0.3)
            waited = not t2.done()
        finally:
            release.set()   # never leave the first transaction open
        outcomes = await asyncio.gather(t1, t2, return_exceptions=True)
        assert outcomes[0] is None, outcomes[0]
        assert waited, "the second activation must wait for the first"
        assert isinstance(outcomes[1], asyncpg.PostgresError) and seats.is_seat_limit(outcomes[1]), outcomes[1]
        assert await used() == LIMIT

        # ── the 21st invitation acceptance is refused; the link stays usable ──
        email = f"late-{tenant[:8]}@seat.test"
        raw = await invitation(email)
        body = auth.AcceptInviteRequest(token=raw, password="a-long-enough-pass-phrase-21", full_name="Late Agent")
        with pytest.raises(HTTPException) as refused:
            await auth.accept_invite(body, Response())
        assert refused.value.status_code == 409 and "every seat" in refused.value.detail
        state = await admin.fetchval("SELECT state FROM brokerage_invitation_preview($1)", bo.hash_invitation_token(raw))
        assert state == "pending"
        assert await admin.fetchval("SELECT count(*) FROM users WHERE agent_id=$1", email) == 0

        # ── suspension frees a seat; reinstating past the limit is refused ───
        victim = await admin.fetchval(
            "SELECT id FROM users WHERE tenant_id=$1 AND role='agent' ORDER BY agent_id LIMIT 1", uuid.UUID(tenant))
        await admin.execute("UPDATE users SET is_active=false WHERE id=$1", victim)
        assert await used() == LIMIT - 1
        await auth.accept_invite(body, Response())            # the freed seat is taken
        assert await used() == LIMIT
        with pytest.raises(HTTPException) as again:
            await bo.reinstate_member(victim, bo.MemberStatusChange(reason="back from leave"), ctx=owner)
        assert again.value.status_code == 409
        assert await used() == LIMIT

        # ── a profile edit of an active member is never a new seat ───────────
        await admin.execute("UPDATE users SET full_name='Renamed' WHERE tenant_id=$1 AND role='agent'", uuid.UUID(tenant))
        await admin.execute("UPDATE users SET is_active=true WHERE tenant_id=$1 AND is_active", uuid.UUID(tenant))

        # ── no limit, no ceiling ─────────────────────────────────────────────
        for n in range(LIMIT + 2):
            await add_user(admin, n, t=unlimited)

        # ── only a platform admin changes the limit from the application ─────
        async with connection.tenant_tx(platform) as conn:
            await conn.execute("UPDATE tenants SET seat_limit = 25 WHERE id = $1::uuid", tenant)
        with pytest.raises(asyncpg.PostgresError) as denied:
            async with connection.tenant_tx(owner) as conn:
                await conn.execute("UPDATE tenants SET seat_limit = 500 WHERE id = $1::uuid", tenant)
        assert getattr(denied.value, "sqlstate", "") == "42501"
        assert await admin.fetchval("SELECT seat_limit FROM tenants WHERE id=$1", uuid.UUID(tenant)) == 25
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for t in (tenant, unlimited):
                for table in ("team_memberships", "brokerage_invitations", "users"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", t)
                await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", t)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
