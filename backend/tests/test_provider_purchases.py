"""Billable purchases: intent first, one in flight, reuse before re-buy."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest

import provider_purchases as pp
from command_providers import ProviderRejectedError, ProviderResult
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="a@b.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)


class Conn:
    def __init__(self, confirmed=None, in_flight=False):
        self.confirmed = confirmed
        self.in_flight = in_flight
        self.updates = []

    async def fetchrow(self, sql, *a):
        return self.confirmed

    async def fetchval(self, sql, *a):
        if self.in_flight:
            raise asyncpg.exceptions.UniqueViolationError("dup")
        return "p1"

    async def execute(self, sql, *a):
        self.updates.append(a[1])


@pytest.fixture
def conn(monkeypatch):
    c = Conn()

    @asynccontextmanager
    async def tx(ctx):
        yield c

    monkeypatch.setattr(pp, "tenant_tx", tx)
    return c


def _buy(fn):
    return asyncio.run(pp.buy(CTX, "twilio", fn))


def test_success_records_the_number_the_moment_it_is_bought(conn):
    async def ok():
        return ProviderResult("twilio_number", "PN1", "purchased", {"phone_number": "+13025550100"})

    assert _buy(ok) == ("+13025550100", "PN1", "p1", True)
    assert conn.updates == ["confirmed"]


def test_a_timeout_is_unknown_and_never_reported_as_failure(conn):
    async def hang():
        raise asyncio.TimeoutError()

    with pytest.raises(pp.PurchaseUnconfirmed):
        _buy(hang)
    assert conn.updates == ["unknown"]


def test_a_definite_refusal_is_failed(conn):
    async def no():
        raise ProviderRejectedError("no numbers available")

    with pytest.raises(ProviderRejectedError):
        _buy(no)
    assert conn.updates == ["failed"]


def test_a_second_purchase_while_one_is_in_flight_is_refused(conn):
    conn.in_flight = True
    bought = []

    async def ok():
        bought.append(1)
        return ProviderResult("twilio_number", "PN2", "purchased", {"phone_number": "+1"})

    with pytest.raises(pp.PurchaseInFlight):
        _buy(ok)
    assert bought == []


def test_a_bought_but_unattached_number_is_reused_not_rebought(conn):
    conn.confirmed = {"id": "p0", "provider_ref": "PN0", "detail": {"phone_number": "+13025550199"}}
    bought = []

    async def ok():
        bought.append(1)
        return ProviderResult("twilio_number", "PN9", "purchased", {"phone_number": "+1"})

    assert _buy(ok) == ("+13025550199", "PN0", "p0", True)
    assert bought == []
