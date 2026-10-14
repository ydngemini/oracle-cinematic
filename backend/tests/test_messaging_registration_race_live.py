"""10DLC brand and campaign registration never creates a duplicate at the
provider (audit A07 follow-up, ledger L10-09), against real PostgreSQL with
FORCE RLS (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The invariant: a brokerage has at most one 10DLC brand and one campaign as the
provider sees it. Each registration costs money at the carrier, so:
- a submission when one already exists is refused (409) and reaches no
  provider — no adapter can update a brand or campaign in place yet;
- racing submissions, from one broker owner or several, reach the provider
  exactly once (the intent goes through the provider side-effect ledger,
  provider_purchases, under the brand row's lock);
- an ambiguous provider outcome (timeout, lost answer) is recorded `unknown`
  and blocks resubmission until it has been checked; a definite refusal
  allows a retry; an accepted-but-unrecorded registration is reused.

The race is made deterministic: the mock provider holds each caller until
every racer has had the chance to reach it. No provider is called.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

RACERS = 6


class CountingAdapter:
    """A mock 10DLC provider that counts calls. Each call waits until `expect`
    callers arrived (or a short deadline), so a missing guard shows up as more
    than one registration rather than as a lucky interleaving."""

    def __init__(self, *, expect: int = 1, fail_with: BaseException | None = None):
        self.expect = expect
        self.fail_with = fail_with
        self.brand_calls = 0
        self.campaign_calls = 0
        self._arrived = asyncio.Event()

    async def _hold(self, n: int) -> None:
        if n >= self.expect:
            self._arrived.set()
        try:
            await asyncio.wait_for(self._arrived.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        if self.fail_with is not None:
            raise self.fail_with

    async def create_brand(self, fields, *, credentials=None):
        from command_providers import ProviderResult

        self.brand_calls += 1
        n = self.brand_calls
        await self._hold(n)
        return ProviderResult("telnyx_brand", f"brand-{n}", "pending", {})

    async def submit_campaign(self, *, brand_id, description, usecase, credentials=None, **fields):
        from command_providers import ProviderResult

        self.campaign_calls += 1
        n = self.campaign_calls
        await self._hold(n)
        return ProviderResult("telnyx_campaign", f"campaign-{n}", "pending", {})


async def _setup(admin, *, agents: list[str]) -> str:
    tenant = str(uuid.uuid4())
    await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'10DLC race test')",
                        tenant, f"tdlc-race-{tenant[:8]}")
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


def _ctx(tenant: str, agent: str):
    from tenancy import Role, TenantContext

    return TenantContext(agent_id=agent, tenant_id=tenant, role=Role.BROKER_OWNER)


def _brand_body(**overrides):
    from messaging_api import BrandRegister

    fields = dict(display_name="Race Realty", company_name="Race Realty LLC", email="owner@live.test",
                  entity_type="PRIVATE_PROFIT", vertical="REAL_ESTATE", ein="00-0000000")
    fields.update(overrides)
    return BrandRegister(**fields)


def _campaign_body():
    from messaging_api import CampaignRegister

    return CampaignRegister(description="Appointment reminders and replies to clients who texted us.")


def _patch(monkeypatch, adapter) -> None:
    import messaging_api
    import messaging_provider

    async def no_credentials(ctx):
        return {}

    monkeypatch.setattr(messaging_api, "_telnyx_credentials", no_credentials)
    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda name: adapter)


def _status(outcome) -> int:
    from fastapi import HTTPException

    if isinstance(outcome, HTTPException):
        return outcome.status_code
    if isinstance(outcome, BaseException):
        raise outcome
    return 200


async def _ledger(admin, tenant: str):
    return [tuple(r) for r in await admin.fetch(
        "SELECT provider, kind, state, provider_ref FROM provider_purchases WHERE tenant_id=$1 "
        "ORDER BY created_at", uuid.UUID(tenant))]


async def _registered_brand(admin, tenant: str, ref: str = "brand-existing") -> None:
    await admin.execute(
        "INSERT INTO tenant_messaging_brands (tenant_id, provider, provider_brand_id, status, display_name, "
        "company_name, entity_type, vertical, email, country, submitted_at) "
        "VALUES ($1,'telnyx',$2,'verified','Race Realty','Race Realty LLC','PRIVATE_PROFIT','REAL_ESTATE',"
        "'owner@live.test','US',now())", tenant, ref)


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


# ── brand ──────────────────────────────────────────────────────────────────


def test_racing_brand_submissions_register_one_brand(monkeypatch):
    async def scenario(admin):
        import messaging_api

        owners = [f"tdlc-a{i}-{uuid.uuid4().hex[:6]}@live.test" for i in range(2)]
        tenant = await _setup(admin, agents=owners)
        try:
            adapter = CountingAdapter(expect=RACERS)
            _patch(monkeypatch, adapter)
            # Two broker owners of one brokerage, double-clicking.
            outcomes = await asyncio.gather(*(
                messaging_api.register_brand(_brand_body(), _ctx(tenant, owners[i % 2]))
                for i in range(RACERS)), return_exceptions=True)

            assert adapter.brand_calls == 1, f"{adapter.brand_calls} brands registered for one brokerage"
            statuses = sorted(_status(o) for o in outcomes)
            assert statuses == [200] + [409] * (RACERS - 1), statuses
            rows = await admin.fetch(
                "SELECT provider_brand_id, status FROM tenant_messaging_brands WHERE tenant_id=$1",
                uuid.UUID(tenant))
            assert [tuple(r) for r in rows] == [("brand-1", "pending")]
            assert await _ledger(admin, tenant) == [("telnyx", "10dlc_brand", "attached", "brand-1")]

            # A later correction does not register a second brand.
            with pytest.raises(Exception) as excinfo:
                await messaging_api.register_brand(_brand_body(display_name="Renamed"), _ctx(tenant, owners[0]))
            assert _status(excinfo.value) == 409
            assert excinfo.value.detail == (
                "A brand is already registered for this number; contact support to change it.")
            assert adapter.brand_calls == 1
            assert await admin.fetchval(
                "SELECT provider_brand_id FROM tenant_messaging_brands WHERE tenant_id=$1",
                uuid.UUID(tenant)) == "brand-1"
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_an_unconfirmed_brand_blocks_resubmission(monkeypatch):
    async def scenario(admin):
        import messaging_api

        owner = f"tdlc-b-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[owner])
        try:
            # The provider may have created the brand: the answer never came.
            adapter = CountingAdapter(fail_with=asyncio.TimeoutError())
            _patch(monkeypatch, adapter)
            with pytest.raises(Exception) as first:
                await messaging_api.register_brand(_brand_body(), _ctx(tenant, owner))
            assert _status(first.value) == 502
            assert await _ledger(admin, tenant) == [("telnyx", "10dlc_brand", "unknown", None)]
            with pytest.raises(Exception) as again:
                await messaging_api.register_brand(_brand_body(), _ctx(tenant, owner))
            assert _status(again.value) == 409
            assert adapter.brand_calls == 1, "a possibly-created brand was submitted again"
            assert await admin.fetchval(
                "SELECT provider_brand_id FROM tenant_messaging_brands WHERE tenant_id=$1",
                uuid.UUID(tenant)) is None
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_a_rejected_brand_can_be_corrected_and_retried(monkeypatch):
    async def scenario(admin):
        import messaging_api
        from command_providers import ProviderRejectedError

        owner = f"tdlc-c-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[owner])
        try:
            _patch(monkeypatch, CountingAdapter(fail_with=ProviderRejectedError("EIN does not match")))
            with pytest.raises(Exception) as first:
                await messaging_api.register_brand(_brand_body(), _ctx(tenant, owner))
            assert _status(first.value) == 502
            assert await admin.fetchval(
                "SELECT status FROM tenant_messaging_brands WHERE tenant_id=$1", uuid.UUID(tenant)) == "failed"

            accepting = CountingAdapter()
            _patch(monkeypatch, accepting)
            result = await messaging_api.register_brand(_brand_body(ein="11-1111111"), _ctx(tenant, owner))
            assert accepting.brand_calls == 1 and result["provider_brand_id"] == "brand-1"
            row = await admin.fetchrow(
                "SELECT provider_brand_id, ein, failure_reason FROM tenant_messaging_brands WHERE tenant_id=$1",
                uuid.UUID(tenant))
            assert tuple(row) == ("brand-1", "11-1111111", None)
            assert [s for _, _, s, _ in await _ledger(admin, tenant)] == ["failed", "attached"]
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_a_confirmed_but_unrecorded_brand_is_reused(monkeypatch):
    async def scenario(admin):
        import messaging_api

        owner = f"tdlc-d-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[owner])
        try:
            # A crash after the provider answered, before the brand row was updated.
            await admin.execute(
                "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, provider_ref) "
                "VALUES ($1,$2,'telnyx','10dlc_brand','confirmed','brand-earlier')", tenant, owner)
            adapter = CountingAdapter()
            _patch(monkeypatch, adapter)
            result = await messaging_api.register_brand(_brand_body(), _ctx(tenant, owner))
            assert adapter.brand_calls == 0
            assert result["provider_brand_id"] == "brand-earlier"
            assert await _ledger(admin, tenant) == [("telnyx", "10dlc_brand", "attached", "brand-earlier")]
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


# ── campaign ───────────────────────────────────────────────────────────────


def test_racing_campaign_submissions_register_one_campaign(monkeypatch):
    async def scenario(admin):
        import messaging_api

        owners = [f"tdlc-e{i}-{uuid.uuid4().hex[:6]}@live.test" for i in range(2)]
        tenant = await _setup(admin, agents=owners)
        try:
            await _registered_brand(admin, tenant)
            adapter = CountingAdapter(expect=RACERS)
            _patch(monkeypatch, adapter)
            outcomes = await asyncio.gather(*(
                messaging_api.register_campaign(_campaign_body(), _ctx(tenant, owners[i % 2]))
                for i in range(RACERS)), return_exceptions=True)

            assert adapter.campaign_calls == 1, f"{adapter.campaign_calls} campaigns submitted for one brand"
            statuses = sorted(_status(o) for o in outcomes)
            assert statuses == [200] + [409] * (RACERS - 1), statuses
            rows = await admin.fetch(
                "SELECT provider_campaign_id, status FROM tenant_messaging_campaigns WHERE tenant_id=$1",
                uuid.UUID(tenant))
            assert [tuple(r) for r in rows] == [("campaign-1", "pending")]
            assert await _ledger(admin, tenant) == [("telnyx", "10dlc_campaign", "attached", "campaign-1")]

            with pytest.raises(Exception) as excinfo:
                await messaging_api.register_campaign(_campaign_body(), _ctx(tenant, owners[1]))
            assert _status(excinfo.value) == 409
            assert excinfo.value.detail == (
                "A campaign is already registered for this number; contact support to change it.")
            assert adapter.campaign_calls == 1
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_an_unconfirmed_campaign_blocks_resubmission(monkeypatch):
    async def scenario(admin):
        import messaging_api

        owner = f"tdlc-f-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[owner])
        try:
            await _registered_brand(admin, tenant)
            adapter = CountingAdapter(fail_with=asyncio.TimeoutError())
            _patch(monkeypatch, adapter)
            with pytest.raises(Exception) as first:
                await messaging_api.register_campaign(_campaign_body(), _ctx(tenant, owner))
            assert _status(first.value) == 502
            with pytest.raises(Exception) as again:
                await messaging_api.register_campaign(_campaign_body(), _ctx(tenant, owner))
            assert _status(again.value) == 409
            assert adapter.campaign_calls == 1, "a possibly-created campaign was submitted again"
            assert await _ledger(admin, tenant) == [("telnyx", "10dlc_campaign", "unknown", None)]
            assert await admin.fetchval(
                "SELECT count(*) FROM tenant_messaging_campaigns WHERE tenant_id=$1", uuid.UUID(tenant)) == 0
        finally:
            await _teardown(admin, tenant)

    _run(scenario)


def test_a_rejected_campaign_can_be_retried(monkeypatch):
    async def scenario(admin):
        import messaging_api
        from command_providers import ProviderRejectedError

        owner = f"tdlc-g-{uuid.uuid4().hex[:8]}@live.test"
        tenant = await _setup(admin, agents=[owner])
        try:
            await _registered_brand(admin, tenant)
            _patch(monkeypatch, CountingAdapter(fail_with=ProviderRejectedError("sample message missing")))
            with pytest.raises(Exception) as first:
                await messaging_api.register_campaign(_campaign_body(), _ctx(tenant, owner))
            assert _status(first.value) == 502
            accepting = CountingAdapter()
            _patch(monkeypatch, accepting)
            result = await messaging_api.register_campaign(_campaign_body(), _ctx(tenant, owner))
            assert accepting.campaign_calls == 1 and result["provider_campaign_id"] == "campaign-1"
            assert [s for _, _, s, _ in await _ledger(admin, tenant)] == ["failed", "attached"]
        finally:
            await _teardown(admin, tenant)

    _run(scenario)
