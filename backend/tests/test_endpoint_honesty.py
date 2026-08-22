"""Endpoints must not state facts their data cannot support.

Each case here is a specific claim the API used to make for free:

  * "no results" for a table that was never loaded, which reads as a finding
    about the subject rather than an admission about the deployment;
  * "designated agency is permitted" in all 51 jurisdictions, sourced entirely
    from a column default nobody ever wrote to;
  * one school district presented as the only one within a 5-mile radius, when
    the fallback source does point-in-polygon and knows nothing about radius;
  * listings returned as "within your radius" after the radius filter silently
    failed to apply;
  * 2024-10-01 seed figures served in 2026 with nothing marking them stale.

Modelled on test_flood_zone_honesty.py, which covers the same failure for FEMA
flood zones.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

import state_compliance._common as common
import state_compliance.routes_market as market
import state_compliance.routes_mls as mls
import state_compliance.routes_reference as reference
from state_compliance._common import DATASET_NOT_LOADED
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)

LAT, LNG = 39.7392, -104.9903  # Denver, CO


def _patch_reads(monkeypatch, module, fake):
    """Route every DB read in `module` through `fake`.

    Two bindings are needed. The route modules do `from ._common import _fetch`,
    which binds the function object, so patching `_common` alone leaves their
    direct calls untouched. Conversely `_fetchrow` and `_require_dataset_loaded`
    live in `_common` and call `_common._fetch` internally, so patching the
    route module alone misses those. Patch both and every path is covered.
    """
    monkeypatch.setattr(common, "_fetch", fake)
    monkeypatch.setattr(module, "_fetch", fake)


# ---------------------------------------------------------------------------
# Unloaded datasets say so, instead of reporting an absence of data
# ---------------------------------------------------------------------------

def test_unloaded_county_dataset_is_distinguished_from_an_unknown_county(monkeypatch):
    async def no_rows(*_a, **_k):
        return []

    _patch_reads(monkeypatch, market, no_rows)

    with pytest.raises(market.HTTPException) as excinfo:
        asyncio.run(market.get_county_market_data(fips_code="08031", ctx=CTX))

    assert excinfo.value.status_code == 404
    detail = excinfo.value.detail
    assert isinstance(detail, dict), "the body must be machine-readable, not prose"
    assert detail["code"] == DATASET_NOT_LOADED
    assert detail["dataset"] == "county_market_stats"
    assert "how_to_populate" in detail


def test_a_loaded_county_dataset_still_reports_a_genuine_miss(monkeypatch):
    """The distinction is the entire point — a populated table must 404 normally."""
    async def rows_exist_but_not_this_one(_ctx, query, *_a):
        # The county lookup misses; the emptiness probe finds the table populated.
        return [] if "county_market_stats WHERE" in query else [{"present": 1}]

    _patch_reads(monkeypatch, market, rows_exist_but_not_this_one)

    with pytest.raises(market.HTTPException) as excinfo:
        asyncio.run(market.get_county_market_data(fips_code="08031", ctx=CTX))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail != DATASET_NOT_LOADED
    assert "08031" in str(excinfo.value.detail)


def test_unloaded_advertising_rules_do_not_read_as_no_rules_apply(monkeypatch):
    """The most dangerous empty list in the module — it is a compliance surface."""
    async def no_rows(*_a, **_k):
        return []

    _patch_reads(monkeypatch, reference, no_rows)

    with pytest.raises(reference.HTTPException) as excinfo:
        asyncio.run(reference.list_advertising_rules(state_code="CO", category=None, ctx=CTX))

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == DATASET_NOT_LOADED
    assert excinfo.value.detail["dataset"] == "state_advertising_rules"


# ---------------------------------------------------------------------------
# Agency law is not asserted from a column default
# ---------------------------------------------------------------------------

def test_unresearched_agency_flags_are_unknown_not_permitted(monkeypatch):
    async def profile_row(*_a, **_k):
        # Post-0069 shape: the two columns the seed never populated are NULL.
        return {
            "state_code": "CO", "state_name": "Colorado",
            "attorney_review_required": False, "mandatory_disclosure": True,
            "has_tds": True, "license_authority": "DORA",
            "dual_agency_permitted": False,          # genuinely researched — CO restricts it
            "designated_agency_permitted": None,     # never researched
            "sub_agency_permitted": None,            # never researched
        }

    monkeypatch.setattr(reference, "_fetchrow", profile_row)

    result = asyncio.run(reference.get_state_profile(state_code="CO", ctx=CTX))

    assert result.designated_agency_permitted is None, (
        "True here asserts a form of agency is permitted in a state nobody checked"
    )
    assert result.sub_agency_permitted is None
    # The researched flag must survive untouched — this is not a blanket nulling.
    assert result.dual_agency_permitted is False


# ---------------------------------------------------------------------------
# School districts: the live fallback must not imply a radius it never applied
# ---------------------------------------------------------------------------

def test_nces_fallback_reports_one_containing_district_without_faking_quality(monkeypatch):
    async def no_local_rows(*_a, **_k):
        return []

    async def nces(_lat, _lng):
        return {
            "district_name": "Denver County 1",
            "leaid": "0803360",
            "state_fips": "08",
            "source": "nces_edge",
        }

    _patch_reads(monkeypatch, market, no_local_rows)
    monkeypatch.setattr(market, "_live_school_district", nces)

    result = asyncio.run(market.get_nearby_schools(lat=LAT, lng=LNG, radius=5.0, ctx=CTX))

    assert result.source == "nces_edge"
    assert result.radius_applied is False, (
        "NCES EDGE is point-in-polygon; claiming the radius was honoured would "
        "present the containing district as the only one within 5 miles"
    )
    assert len(result.districts) == 1
    only = result.districts[0]
    assert only.district_name == "Denver County 1"
    assert only.state_code == "CO", "state FIPS 08 should resolve via the shared table"
    # NCES carries boundaries, not quality or size.
    assert only.rating is None
    assert only.enrollment is None
    assert only.student_teacher_ratio is None
    assert only.distance_miles is None, "a synthesised 0.0 would read as 'at this address'"


def test_schools_with_no_source_at_all_says_so(monkeypatch):
    async def no_local_rows(*_a, **_k):
        return []

    async def nces_down(_lat, _lng):
        return None

    _patch_reads(monkeypatch, market, no_local_rows)
    monkeypatch.setattr(market, "_live_school_district", nces_down)

    result = asyncio.run(market.get_nearby_schools(lat=LAT, lng=LNG, radius=5.0, ctx=CTX))

    assert result.districts == []
    assert result.data_available is False, (
        "an empty list alone reads as 'no school districts near here', which is "
        "not true of anywhere in the US"
    )
    assert result.source == "none"


def test_missing_earthdistance_extension_falls_back_instead_of_503(monkeypatch):
    """0013 creates `earthdistance` best-effort, so it may simply not be there."""
    from fastapi import HTTPException, status

    async def extension_missing(*_a, **_k):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Memory Core offline."
        )

    async def nces(_lat, _lng):
        return {"district_name": "Denver County 1", "leaid": "0803360", "state_fips": "08"}

    _patch_reads(monkeypatch, market, extension_missing)
    monkeypatch.setattr(market, "_live_school_district", nces)

    result = asyncio.run(market.get_nearby_schools(lat=LAT, lng=LNG, radius=5.0, ctx=CTX))

    assert result.source == "nces_edge"
    assert result.radius_applied is False


# ---------------------------------------------------------------------------
# Market vintage
# ---------------------------------------------------------------------------

def test_stale_seed_market_data_is_flagged_stale(monkeypatch):
    stale_day = date.today() - timedelta(days=600)

    async def seed_row(_ctx, _query, *_a):
        return [{
            "state_code": "CO", "state_name": "Colorado",
            "median_sale_price": 785000, "median_list_price": 785000,
            "as_of_date": stale_day,
        }]

    _patch_reads(monkeypatch, market, seed_row)

    result = asyncio.run(market.get_state_market_overview(state_code="CO", ctx=CTX))

    assert result.as_of_date == stale_day
    assert result.data_vintage_days >= 600
    assert result.is_stale is True, "a 2024 seed served today must not look current"


def test_fresh_market_data_is_not_flagged_stale(monkeypatch):
    async def fresh_row(_ctx, _query, *_a):
        return [{
            "state_code": "CO", "state_name": "Colorado",
            "median_sale_price": 800000,
            "as_of_date": date.today() - timedelta(days=3),
        }]

    _patch_reads(monkeypatch, market, fresh_row)

    result = asyncio.run(market.get_state_market_overview(state_code="CO", ctx=CTX))

    assert result.is_stale is False
    assert result.data_vintage_days == 3


# ---------------------------------------------------------------------------
# MLS radius search must not present unfiltered results as "within your radius"
# ---------------------------------------------------------------------------

class _FakeConn:
    """Fails the radius-filtered query the way Postgres does without earthdistance."""

    def __init__(self, fail_on_earthdistance: bool):
        self._fail = fail_on_earthdistance
        self.queries: list[str] = []

    def _guard(self, query: str):
        self.queries.append(query)
        if self._fail and "earth_distance" in query:
            exc = Exception('function ll_to_earth(numeric, numeric) does not exist')
            exc.sqlstate = "42883"  # undefined_function
            raise exc

    async def fetchrow(self, query, *_args):
        self._guard(query)
        return {"count": 2}

    async def fetch(self, query, *_args):
        self._guard(query)
        return [
            {"id": "11111111-1111-1111-1111-111111111111", "mls_id": "m1",
             "list_price": 500000, "address": "1 Main St"},
            {"id": "22222222-2222-2222-2222-222222222222", "mls_id": "m1",
             "list_price": 600000, "address": "2 Main St"},
        ]


def _fake_tenant_tx(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tx(_ctx):
        yield conn

    return _tx


def test_mls_radius_search_flags_when_the_filter_could_not_be_applied(monkeypatch):
    conn = _FakeConn(fail_on_earthdistance=True)
    monkeypatch.setattr(mls, "tenant_tx", _fake_tenant_tx(conn))

    body = mls.MLSSearchBody(lat=LAT, lng=LNG, radius_miles=5.0, limit=20, offset=0)
    result = asyncio.run(mls.mls_search(body=body, ctx=CTX))

    assert result.radius_applied is False, (
        "the distance filter was dropped; saying nothing would present listings "
        "from anywhere in the dataset as being within 5 miles"
    )
    assert result.total_count == 2
    assert any("earth_distance" in q for q in conn.queries), "should have tried the filter first"
    assert any("earth_distance" not in q for q in conn.queries), "should have retried without it"


def test_mls_radius_search_reports_applied_when_it_works(monkeypatch):
    conn = _FakeConn(fail_on_earthdistance=False)
    monkeypatch.setattr(mls, "tenant_tx", _fake_tenant_tx(conn))

    body = mls.MLSSearchBody(lat=LAT, lng=LNG, radius_miles=5.0, limit=20, offset=0)
    result = asyncio.run(mls.mls_search(body=body, ctx=CTX))

    assert result.radius_applied is True
    assert all("earth_distance" in q for q in conn.queries)


# ---------------------------------------------------------------------------
# A client must be able to ask what this deployment actually mounted
# ---------------------------------------------------------------------------

def test_capabilities_reports_unmounted_optional_routers_as_absent():
    """VideoStudioPanel shipped against a router that defaults to unmounted.

    It had no way to ask, so it fired eight requests and rendered eight 404s as
    though the studio were broken. The default test environment does not set
    ORACLE_FEATURE_VIDEO_STUDIO, so this is that exact deployment.
    """
    import json
    import server

    payload = json.loads(asyncio.run(server.platform_capabilities(ctx=CTX)).body)

    assert payload["video_studio"] is False
    assert set(payload) == {"video_studio", "aws_observability", "chaos_c2"}
    # Answered from the live route table, so it cannot drift from what is mounted.
    mounted = [getattr(r, "path", "") for r in server.app.routes]
    assert not any(p.startswith("/api/video-studio") for p in mounted)
