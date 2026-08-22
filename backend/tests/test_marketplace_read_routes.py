"""The two marketplace read routes that make the write routes usable.

`from-contract` creates a publication in state 'draft', and `publish` acts on
one — but `GET /api/marketplace` (browse) deliberately returns only
published/under-offer inventory, because it is the SHARED marketplace every
brokerage sees. That left a draft invisible to its own author and the publish
step with nothing to act on. Likewise `POST /buyers/requests` takes a
`buyer_profile_id` that nothing could enumerate.

The property these tests exist to protect is the one the RLS policy in
migration 0027 states as "drafts never cross the wall": browse is shared,
`/publications` is mine. If `/publications` ever stops scoping to the caller's
tenant, the read policy alone would still admit other tenants' published rows
and this route would quietly become a second, wrongly-shaped browse.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import marketplace_api
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)


class _Conn:
    """Records the SQL and args so the scoping can be asserted directly."""

    def __init__(self, rows=None):
        self.query = ""
        self.args: tuple = ()
        self._rows = rows or []

    async def fetch(self, query, *args):
        self.query = " ".join(query.split())
        self.args = args
        return self._rows


def _patch(monkeypatch, conn):
    @asynccontextmanager
    async def _tx(_ctx):
        yield conn

    monkeypatch.setattr(marketplace_api, "tenant_tx", _tx)
    return conn


# ---------------------------------------------------------------------------
# /publications — mine, every state
# ---------------------------------------------------------------------------

def test_own_publications_are_scoped_to_the_calling_tenant(monkeypatch):
    conn = _patch(monkeypatch, _Conn())

    asyncio.run(marketplace_api.list_own_publications(limit=100, ctx=CTX))

    assert "FROM marketplace_publications" in conn.query
    assert "WHERE tenant_id=$1::uuid" in conn.query, (
        "without an explicit tenant filter the RLS read policy still admits "
        "other tenants' published rows — this route would become a second browse"
    )
    assert conn.args == (TENANT_ID, 100)


def test_own_publications_does_not_filter_by_state(monkeypatch):
    """The whole point: a draft must be visible to its author, or it can never
    be published. browse filters state; this must not."""
    conn = _patch(monkeypatch, _Conn())

    asyncio.run(marketplace_api.list_own_publications(limit=100, ctx=CTX))

    assert "state IN" not in conn.query
    assert "'draft'" not in conn.query  # not filtered *to* drafts either


def test_browse_still_only_shows_published_inventory(monkeypatch):
    """The counterpart assertion — the shared marketplace must not start
    leaking drafts because a sibling route now returns them."""
    conn = _patch(monkeypatch, _Conn())

    asyncio.run(marketplace_api.browse_marketplace(state_code=None, limit=100, ctx=CTX))

    assert "state IN ('published','under_offer')" in conn.query


def test_own_publications_returns_serialised_rows(monkeypatch):
    """_row() stringifies UUIDs/datetimes and decodes the jsonb summary; a raw
    asyncpg Record would not survive JSON encoding."""
    conn = _patch(monkeypatch, _Conn(rows=[{
        "id": "pub-1",
        "state": "draft",
        "truthful_summary": '{"address": "15 Main St"}',
        "asking_price": 210000,
    }]))

    result = asyncio.run(marketplace_api.list_own_publications(limit=100, ctx=CTX))

    assert result["publications"][0]["truthful_summary"] == {"address": "15 Main St"}
    assert result["publications"][0]["state"] == "draft"


# ---------------------------------------------------------------------------
# /buyers/profiles — findable ids for POST /buyers/requests
# ---------------------------------------------------------------------------

def test_buyer_profiles_are_scoped_and_limited_to_active(monkeypatch):
    conn = _patch(monkeypatch, _Conn())

    asyncio.run(marketplace_api.list_buyer_profiles(limit=50, ctx=CTX))

    assert "FROM buyer_profiles" in conn.query
    assert "WHERE p.tenant_id=$1::uuid" in conn.query
    assert "p.active=true" in conn.query
    assert conn.args == (TENANT_ID, 50)


def test_buyer_profiles_count_only_live_requests(monkeypatch):
    """A profile whose only request expired participates in no matching. The
    count has to reflect that or it explains nothing."""
    conn = _patch(monkeypatch, _Conn())

    asyncio.run(marketplace_api.list_buyer_profiles(limit=100, ctx=CTX))

    assert "r.status='active'" in conn.query
    assert "r.expires_at IS NULL OR r.expires_at > now()" in conn.query


def test_buyer_profiles_join_is_outer_so_a_missing_client_still_lists(monkeypatch):
    """An inner join would silently drop a profile whose client row was
    deleted — the profile would vanish from the picker while still being a
    valid FK target for a request."""
    conn = _patch(monkeypatch, _Conn())

    asyncio.run(marketplace_api.list_buyer_profiles(limit=100, ctx=CTX))

    assert "LEFT JOIN clients" in conn.query


@pytest.mark.parametrize(
    "route",
    ["list_own_publications", "list_buyer_profiles"],
)
def test_both_routes_are_behind_the_marketplace_feature_gate(monkeypatch, route):
    """Every other route in this module gates on Feature.MARKETPLACE; a read
    route that skipped it would advertise a disabled capability."""
    conn = _patch(monkeypatch, _Conn())
    called = {}

    def _require(feature):
        called["feature"] = feature

    monkeypatch.setattr(marketplace_api, "require_feature", _require)
    asyncio.run(getattr(marketplace_api, route)(limit=10, ctx=CTX))

    assert called["feature"] is marketplace_api.Feature.MARKETPLACE
