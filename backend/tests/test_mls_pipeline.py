"""Regression tests for the shared public-property catalog search path."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import mls_portal
from tenancy import Role, TenantContext


class _FakeConnection:
    def __init__(self) -> None:
        self.count_query = ""
        self.count_args: tuple = ()
        self.data_query = ""
        self.data_args: tuple = ()

    async def fetchrow(self, query: str, *args):
        self.count_query = query
        self.count_args = args
        return {"n": 34_999}

    async def fetch(self, query: str, *args):
        self.data_query = query
        self.data_args = args
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "public_record_id": "11111111-1111-1111-1111-111111111111",
                "parcel_id": "safe-parcel",
                "state": "DE",
                "source_key": "firehose:DE",
                "source_name": "New Castle County parcels + ownership",
                "coverage_scope": "county:New Castle",
                "detail_level": "standard",
                "observed_fields": ["parcel_id", "state"],
                "verification_required": True,
                "record_refreshed_at": datetime.now(timezone.utc),
                "match_score": 0,
                "match_type": "browse",
                "sqft": 1400,
                "address": "",
                "city": "",
                "zip": "",
                "county": "",
                "owner_name": "",
                "owner_type": "",
                "last_sale_date": None,
                "is_absentee": False,
                "price": None,
                "equity_percent": None,
                "distress_flags": [],
            }
        ]


def _run_search(monkeypatch, ctx: TenantContext, **kwargs):
    conn = _FakeConnection()
    params = {
        "city": None,
        "state": None,
        "zip": None,
        "min_price": None,
        "max_price": None,
        "beds": None,
        "q": None,
        "page": 1,
    }
    params.update(kwargs)

    @asynccontextmanager
    async def fake_tenant_tx(received_ctx):
        assert received_ctx == ctx
        yield conn

    monkeypatch.setattr(mls_portal, "tenant_tx", fake_tenant_tx)
    result = asyncio.run(mls_portal.mls_pipeline_search(ctx=ctx, **params))
    return result, conn


def test_pipeline_default_count_uses_exact_summary(monkeypatch):
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    result, conn = _run_search(monkeypatch, ctx)

    assert "FROM public_property_records" in conn.count_query
    assert conn.count_args == ()
    assert "ORDER BY match_score DESC, record_refreshed_at DESC, id ASC" in conn.data_query
    assert conn.data_args == (mls_portal.PAGE_SIZE, 0)
    assert result["total"] == 34_999
    assert result["has_more"] is True
    assert result["source"] == "shared public property catalog"
    assert result["coverage"]["jurisdictions_live"] == 51
    assert result["listings"][0]["fact_coverage"] == {
        "observed": ["square_feet"],
        "missing": [
            "assessor_value",
            "last_recorded_sale",
            "bedrooms",
            "bathrooms",
            "lot_square_feet",
            "year_built",
            "total_rooms",
        ],
        "observed_count": 1,
        "required_count": 8,
        "complete": False,
        "source_fields": {},
        "policy": "source_published_only",
    }


def test_public_catalog_state_search_is_normalized_and_not_tenant_scoped(monkeypatch):
    tenant_id = "22222222-2222-2222-2222-222222222222"
    ctx = TenantContext(agent_id="agent", tenant_id=tenant_id, role=Role.AGENT)
    _result, conn = _run_search(monkeypatch, ctx, state="de", page=2)

    assert "FROM public_property_records" in conn.count_query
    assert "tenant_id" not in conn.count_query
    assert "state = $1" in conn.count_query
    assert conn.count_args == ("DE",)
    assert "tenant_id" not in conn.data_query
    assert "state = $1" in conn.data_query
    assert "ORDER BY match_score DESC, record_refreshed_at DESC, id ASC" in conn.data_query
    assert conn.data_args == ("DE", mls_portal.PAGE_SIZE, mls_portal.PAGE_SIZE)


def test_public_catalog_complex_filter_keeps_exact_catalog_count(monkeypatch):
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    _result, conn = _run_search(monkeypatch, ctx, city="Milford", min_price=100_000)

    assert "FROM public_property_records" in conn.count_query
    assert "lead_pipeline_counts" not in conn.count_query
    assert "city ILIKE $1" in conn.count_query
    assert conn.count_args == ("Milford", 100_000)
    assert conn.data_args == ("Milford", 100_000, mls_portal.PAGE_SIZE, 0)


def test_public_catalog_search_normalizes_exact_address_and_ranks_matches(monkeypatch):
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    result, conn = _run_search(
        monkeypatch,
        ctx,
        q="  15 Main St., Dover  ",
        state="de",
    )

    assert "search_document ILIKE $2" in conn.count_query
    assert "regexp_replace(lower(parcel_id)" in conn.count_query
    assert conn.count_args == ("DE", "%15 Main St., Dover%", "15mainstdover")
    assert "THEN 100" in conn.data_query
    assert "'parcel_exact'" in conn.data_query
    assert conn.data_args == (
        "DE",
        "%15 Main St., Dover%",
        "15mainstdover",
        mls_portal.PAGE_SIZE,
        0,
    )
    assert result["accuracy"]["ranked_exact_matches"] is True
