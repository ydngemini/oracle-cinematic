"""Concurrent messaging-route initialization (audit A07, ledger L10-06), against
real PostgreSQL with FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The invariant: the first committed creation of an agent's messaging route
wins. Nothing after it changes the route's provider or provider account
implicitly (switching is a disconnect + reconnect), and the provider-side
hosted-messaging order — with the ownership-verification text it sends — is
submitted at most once per route, however many requests race.

The races are made deterministic, not left to luck:
- route creation: a separate transaction holds the first INSERT uncommitted
  until every racer is provably blocked behind it (pg_stat_activity), then
  commits;
- hosted order: the mock provider waits until every racer has had the chance
  to reach it before answering.

No provider is called: the messaging adapter is a mock that only counts.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

RACERS = 6
NUMBER = "+13025550142"


class CountingAdapter:
    """A mock messaging provider. begin_hosted_messaging holds each caller
    until `expect` callers arrived (or a short deadline), so a missing guard
    shows up as more than one order rather than as a lucky interleaving."""

    def __init__(self, *, expect: int = 1, fail_with: BaseException | None = None):
        self.expect = expect
        self.fail_with = fail_with
        self.order_calls = 0
        self.verify_calls: list[str] = []
        self._arrived = asyncio.Event()

    async def begin_hosted_messaging(self, phone_number, *, messaging_profile_id=None, credentials=None):
        from command_providers import ProviderResult

        self.order_calls += 1
        n = self.order_calls
        if n >= self.expect:
            self._arrived.set()
        try:
            await asyncio.wait_for(self._arrived.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        if self.fail_with is not None:
            raise self.fail_with
        return ProviderResult("telnyx_hosted_order", f"order-{n}", "pending", {"phone_number": phone_number})

    async def start_ownership_verification(self, order_id, phone_number, *, method="sms", credentials=None):
        self.verify_calls.append(order_id)


async def _setup(admin, *, agents: list[str]) -> str:
    tenant = str(uuid.uuid4())
    await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Route race test')",
                        tenant, f"route-race-{tenant[:8]}")
    for agent in agents:
        await admin.execute(
            "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,$3,'broker_owner',$3,'scrypt$x$y')", uuid.uuid4(), tenant, agent)
    return tenant


async def _teardown(admin, tenant: str) -> None:
    await admin.execute("SET session_replication_role = replica")
    try:
        tables = await admin.fetch(
            """SELECT c.table_name, c.data_type FROM information_schema.columns c
                 JOIN information_schema.tables t
                   ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                  AND t.table_type = 'BASE TABLE'""")
        for row in tables:
            cast = "uuid" if row["data_type"] == "uuid" else "text"
            await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
    finally:
        await admin.execute("SET session_replication_role = origin")


async def _verified_number(admin, tenant: str, agent: str, number: str) -> None:
    await admin.execute(
        "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, voice_caller_id_e164, "
        "voice_caller_id_verified, outbound_verification_status, provider, provider_account_id, "
        "forward_on_request, forward_when_ai_unavailable) "
        "VALUES ($1,$2,$3,$3,true,'verified','plivo','plivo-live-test',false,false)",
        tenant, agent, number)


async def _eligible_route(admin, tenant: str, agent: str) -> None:
    await admin.execute(
        "INSERT INTO messaging_routes (tenant_id, agent_id, provider, provider_account_id, eligibility_status) "
        "VALUES ($1,$2,'telnyx','acct-1','eligible')", tenant, agent)


async def _wait_for_blocked_inserts(admin, n: int) -> int:
    seen = 0
    for _ in range(200):
        seen = await admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
            "AND query ILIKE '%INSERT INTO messaging_routes%'")
        if seen >= n:
            return seen
        await asyncio.sleep(0.05)
    return seen


def _ctx(tenant: str, agent: str):
    from tenancy import Role, TenantContext

    return TenantContext(agent_id=agent, tenant_id=tenant, role=Role.BROKER_OWNER)


def _run(scenario):
    async def wrapper():
        import asyncpg

        from db import connection

        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=RACERS + 2)
        try:
            await scenario(admin)
        finally:
            await admin.close()
            await connection.close_pool()

    asyncio.run(wrapper())


# ── route row ──────────────────────────────────────────────────────────────


def test_racing_first_use_never_overwrites_the_first_routes_account():
    async def scenario(admin):
        import asyncpg

        import messaging_data

        agent = f"race-a-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[agent])
        first = await asyncpg.connect(DSN)
        try:
            # The first request's INSERT is in flight (uncommitted) while
            # every racer reads "no route yet" and inserts behind it.
            tx = first.transaction()
            await tx.start()
            await first.execute(
                "INSERT INTO messaging_routes (tenant_id, agent_id, provider, provider_account_id) "
                "VALUES ($1,$2,'telnyx','acct-first')", tenant, agent)
            ctx = _ctx(tenant, agent)
            racers = [
                asyncio.create_task(messaging_data.ensure_messaging_route(
                    ctx, provider="twilio" if i % 2 else "telnyx", provider_account_id=f"acct-racer-{i}"))
                for i in range(RACERS)
            ]
            blocked = await _wait_for_blocked_inserts(admin, RACERS)
            assert blocked >= RACERS, f"only {blocked} racers blocked behind the first insert"
            await tx.commit()
            results = await asyncio.gather(*racers)

            rows = await admin.fetch(
                "SELECT id, provider, provider_account_id FROM messaging_routes WHERE tenant_id=$1",
                uuid.UUID(tenant))
            assert len(rows) == 1
            assert (rows[0]["provider"], rows[0]["provider_account_id"]) == ("telnyx", "acct-first"), (
                f"route was overwritten by a racer: {rows[0]['provider']}/{rows[0]['provider_account_id']}")
            seen = {(str(r["id"]), r["provider"], r["provider_account_id"]) for r in results}
            assert seen == {(str(rows[0]["id"]), "telnyx", "acct-first")}, f"racers saw {sorted(seen)}"
        finally:
            await first.close()
            await _teardown(admin, tenant)

    _run(scenario)


def test_an_unsynchronised_burst_agrees_on_one_route():
    async def scenario(admin):
        import messaging_data

        agent = f"race-b-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[agent])
        try:
            ctx = _ctx(tenant, agent)
            results = await asyncio.gather(*(
                messaging_data.ensure_messaging_route(
                    ctx, provider="twilio" if i % 2 else "telnyx", provider_account_id=f"acct-{i}")
                for i in range(RACERS)))
            row = await admin.fetchrow(
                "SELECT id, provider, provider_account_id, count(*) OVER () AS n FROM messaging_routes "
                "WHERE tenant_id=$1", uuid.UUID(tenant))
            assert row["n"] == 1
            assert {(str(r["id"]), r["provider"], r["provider_account_id"]) for r in results} == {
                (str(row["id"]), row["provider"], row["provider_account_id"])}

            # Later first-use calls with another provider/account change nothing.
            again = await messaging_data.ensure_messaging_route(
                ctx, provider="twilio" if row["provider"] == "telnyx" else "telnyx", provider_account_id="other")
            assert (again["provider"], again["provider_account_id"]) == (row["provider"], row["provider_account_id"])
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


# ── hosted order (the provider side effect) ───────────────────────────────


def test_racing_connects_submit_one_hosted_order(monkeypatch):
    async def scenario(admin):
        import messaging_data
        import messaging_provider

        agent = f"race-c-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[agent])
        try:
            await _verified_number(admin, tenant, agent, NUMBER)
            await _eligible_route(admin, tenant, agent)
            adapter = CountingAdapter(expect=RACERS)
            monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)
            ctx = _ctx(tenant, agent)
            outcomes = await asyncio.gather(*(
                messaging_data.connect_messaging(ctx, provider_account_id="acct-1", credentials={})
                for _ in range(RACERS)), return_exceptions=True)

            assert adapter.order_calls == 1, f"{adapter.order_calls} hosted orders submitted for one route"
            assert adapter.verify_calls == ["order-1"], f"verification texts sent: {adapter.verify_calls}"
            routes = [o for o in outcomes if isinstance(o, dict)]
            errors = [o for o in outcomes if not isinstance(o, dict)]
            assert routes, "nobody connected"
            assert all(r["hosted_order_id"] == "order-1" for r in routes)
            assert all(isinstance(e, messaging_data.MessagingDataError) for e in errors), errors
            row = await admin.fetchrow(
                "SELECT hosted_order_id, hosted_order_status FROM messaging_routes WHERE tenant_id=$1",
                uuid.UUID(tenant))
            assert tuple(row) == ("order-1", "pending_verification")
            ledger = await admin.fetch(
                "SELECT provider, kind, state, provider_ref FROM provider_purchases WHERE tenant_id=$1",
                uuid.UUID(tenant))
            assert [tuple(r) for r in ledger] == [("telnyx", "hosted_messaging_order", "attached", "order-1")]

            # Once connected, a further connect is a read, not a provider call.
            assert (await messaging_data.connect_messaging(
                ctx, provider_account_id="acct-1", credentials={}))["hosted_order_id"] == "order-1"
            assert adapter.order_calls == 1
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_an_unconfirmed_order_blocks_resubmission(monkeypatch):
    async def scenario(admin):
        import messaging_data
        import messaging_provider

        agent = f"race-d-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[agent])
        try:
            await _verified_number(admin, tenant, agent, NUMBER)
            await _eligible_route(admin, tenant, agent)
            # The provider may have created the order: the answer never came.
            adapter = CountingAdapter(fail_with=asyncio.TimeoutError())
            monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)
            ctx = _ctx(tenant, agent)
            with pytest.raises(messaging_data.MessagingDataError):
                await messaging_data.connect_messaging(ctx, provider_account_id="acct-1", credentials={})
            assert await admin.fetchval(
                "SELECT state FROM provider_purchases WHERE tenant_id=$1", uuid.UUID(tenant)) == "unknown"
            with pytest.raises(messaging_data.MessagingDataError):
                await messaging_data.connect_messaging(ctx, provider_account_id="acct-1", credentials={})
            assert adapter.order_calls == 1, "a possibly-created order was submitted again"
            assert adapter.verify_calls == []
            assert await admin.fetchval(
                "SELECT hosted_order_id FROM messaging_routes WHERE tenant_id=$1", uuid.UUID(tenant)) is None
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_a_confirmed_but_unattached_order_is_reused(monkeypatch):
    async def scenario(admin):
        import messaging_data
        import messaging_provider

        agent = f"race-e-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[agent])
        try:
            await _verified_number(admin, tenant, agent, NUMBER)
            await _eligible_route(admin, tenant, agent)
            # A crash after the provider answered, before the route was updated.
            await admin.execute(
                "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, provider_ref) "
                "VALUES ($1,$2,'telnyx','hosted_messaging_order','confirmed','order-earlier')", tenant, agent)
            adapter = CountingAdapter()
            monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)
            route = await messaging_data.connect_messaging(
                _ctx(tenant, agent), provider_account_id="acct-1", credentials={})
            assert adapter.order_calls == 0
            assert route["hosted_order_id"] == "order-earlier"
            assert adapter.verify_calls == ["order-earlier"]
            assert await admin.fetchval(
                "SELECT state FROM provider_purchases WHERE tenant_id=$1", uuid.UUID(tenant)) == "attached"
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_a_rejected_order_can_be_retried(monkeypatch):
    async def scenario(admin):
        import messaging_data
        import messaging_provider
        from command_providers import ProviderRejectedError

        agent = f"race-f-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[agent])
        try:
            await _verified_number(admin, tenant, agent, NUMBER)
            await _eligible_route(admin, tenant, agent)
            rejecting = CountingAdapter(fail_with=ProviderRejectedError("number not portable"))
            monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: rejecting)
            ctx = _ctx(tenant, agent)
            with pytest.raises(ProviderRejectedError):
                await messaging_data.connect_messaging(ctx, provider_account_id="acct-1", credentials={})
            assert await admin.fetchval(
                "SELECT state FROM provider_purchases WHERE tenant_id=$1", uuid.UUID(tenant)) == "failed"

            accepting = CountingAdapter()
            monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: accepting)
            route = await messaging_data.connect_messaging(ctx, provider_account_id="acct-1", credentials={})
            assert accepting.order_calls == 1 and route["hosted_order_id"] == "order-1"
        finally:
            await _teardown(admin, tenant)

    _run(scenario)
