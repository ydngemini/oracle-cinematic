"""Property View address resolve + subject creation stay inside the acting tenant.

Found by the Mission 3 browser journey (scripts/test-mission3-journeys-playwright.py):
every address lookup sat on "Locating…" for 30 s and the resolve call ended in a
500. Under the `app_is_platform_admin() OR tenant_id = app_current_tenant()`
policy the queries had no narrowing predicate, so `address ILIKE '%…%' ORDER BY
created_at DESC LIMIT 10` walked every brokerage's leads (millions on the dev
database) until command_timeout. Media attaches to the record of the tenant
being worked in, so the explicit tenant scope is the intended semantics, not a
duplicate of the RLS predicate.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import property_view_api
from tenancy import Role, TenantContext


class _Conn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return []

    async def fetchval(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return None if query.lstrip().startswith("SELECT") else uuid.uuid4()


def _install(monkeypatch):
    conn = _Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(property_view_api, "tenant_tx", tx)
    return conn


def _ctx() -> TenantContext:
    return TenantContext(agent_id="owner@example.test", tenant_id=str(uuid.uuid4()), role=Role.BROKER_OWNER)


def test_resolve_scopes_leads_and_listings_to_the_acting_tenant(monkeypatch):
    conn = _install(monkeypatch)
    ctx = _ctx()
    out = asyncio.run(property_view_api.resolve_subject(address="100 W 10th St", ctx=ctx))
    assert out == {"leads": [], "listings": []}
    tables = {"leads": False, "listings": False}
    for query, args in conn.calls:
        for table in tables:
            if f"FROM {table}" in query:
                assert "tenant_id = $2::uuid AND address ILIKE $1" in query, query
                assert args == ("%100 W 10th St%", ctx.tenant_id)
                tables[table] = True
    assert all(tables.values()), tables


def test_existing_subject_lookup_uses_the_tenant_parcel_key(monkeypatch):
    conn = _install(monkeypatch)
    ctx = _ctx()
    body = property_view_api.CreateSubject(address="100 W 10th St, Wilmington, DE", state="de")
    out = asyncio.run(property_view_api.create_subject(body=body, ctx=ctx))
    assert out["created"] is True
    lookup, args = conn.calls[0]
    assert lookup.startswith("SELECT id FROM leads WHERE tenant_id = $2::uuid AND parcel_id = $1")
    assert args[1] == ctx.tenant_id and args[0].startswith("pv:")
