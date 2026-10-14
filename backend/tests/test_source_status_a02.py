"""A source outage is never reported as "no data" (audit A02, ledger L11-03).

L11-02 fixed the client steward's property lookup. These are the other sites
the sweep found, each with the failure injected and the answer checked:

  * Census geocoder, single and batch (/api/data/geocode[/batch]);
  * the Nominatim geocoder behind /api/geocode (Property View);
  * Eviction Lab (/api/data/eviction);
  * flood zone, nearby POIs and Walk Score (/api/enrich-property);
  * FRED mortgage rates and Treasury yields (/api/market-snapshot);
  * the RentCast / ATTOM AVM ("UNAVAILABLE" covered an outage and a miss);
  * building footprints (/crm/floorplan/footprint-candidates, auto-dimensions).

For each: a timeout or error comes back as `timed_out` / `unavailable` with a
stable reason code, the response is `degraded` (or a 502), nothing failed is
cached as a miss, and a real miss still reads as `checked`. No test reaches a
real service; no log line carries the address.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import urllib.error
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

import source_status as ss

ADDRESS = "4821 Quillfeather Lane, Dover, DE 19901"
LAT, LNG = 39.1582, -75.5244


class _MemCache:
    """IntegrationCache semantics that matter here: a fetcher that raises
    stores nothing; a returned value is stored and replayed."""

    def __init__(self):
        self.rows: dict = {}
        self.fetches = 0

    def metrics(self) -> dict:
        return {"hits": 0}

    async def get_or_fetch(self, source, request, fetcher, *, ttl=None, stale_ttl=None):  # noqa: ARG002
        key = (source, json.dumps(request, sort_keys=True, default=str))
        if key in self.rows:
            return self.rows[key]
        self.fetches += 1
        value = await fetcher()
        self.rows[key] = value
        return value


@pytest.fixture
def mem_cache(monkeypatch):
    cache = _MemCache()

    async def _get():
        return cache

    import data_integrations.cache as cache_mod

    monkeypatch.setattr(cache_mod, "get_integration_cache", _get)
    return cache


def _no_address_in(caplog) -> None:
    text = caplog.text.lower()
    assert "quillfeather" not in text, "the address reached a log line (CONTRACT C5.4)"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "x", {}, io.BytesIO(b""))


# --- the vocabulary -----------------------------------------------------------

class TestVocabulary:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (TimeoutError(), (ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT)),
            (asyncio.TimeoutError(), (ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT)),
            (urllib.error.URLError(TimeoutError()), (ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT)),
            (_http_error(429), (ss.UNAVAILABLE, ss.RATE_LIMITED)),
            (_http_error(403), (ss.UNAVAILABLE, ss.CREDENTIAL_REJECTED)),
            (_http_error(500), (ss.UNAVAILABLE, ss.UPSTREAM_HTTP_ERROR)),
            (json.JSONDecodeError("x", "", 0), (ss.UNAVAILABLE, ss.BAD_RESPONSE)),
            (RuntimeError("boom"), (ss.UNAVAILABLE, ss.UPSTREAM_ERROR)),
            (ss.SourceFailure(ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT), (ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT)),
        ],
    )
    def test_classify(self, exc, expected):
        assert ss.classify(exc) == expected

    def test_reason_is_a_code_never_the_exception_text(self):
        status, reason = ss.classify(RuntimeError(f"no match for {ADDRESS}"))
        assert reason in ss.REASONS
        assert "Quillfeather" not in json.dumps(ss.failure(RuntimeError(ADDRESS)))

    def test_degraded_counts_required_failures_only(self):
        assert ss.degraded({"a": ss.checked(), "b": ss.not_applicable(ss.NO_COORDINATES)}) is False
        assert ss.degraded({"a": ss.unavailable(ss.NOT_CONFIGURED, required=False)}) is False
        assert ss.degraded({"a": ss.checked(), "b": ss.entry(ss.TIMED_OUT, reason=ss.UPSTREAM_TIMEOUT)}) is True

    def test_source_failure_cannot_claim_success(self):
        with pytest.raises(ValueError):
            ss.SourceFailure(ss.CHECKED, "x")


# --- Census geocoder (single) -------------------------------------------------

def _census(cache):
    from data_integrations.census_geocoder import CensusGeocoder

    geo = CensusGeocoder(cache=cache)
    geo._retry.max_attempts = 1
    return geo


class TestCensusSingle:
    def test_outage_is_timed_out_and_not_cached_as_a_miss(self, caplog):
        cache = _MemCache()
        geo = _census(cache)
        calls = []

        async def dead(url, **_kw):
            calls.append(url)
            raise TimeoutError()

        geo._get_json = dead
        caplog.set_level(logging.DEBUG)
        result, status = asyncio.run(geo.geocode_checked(ADDRESS))
        assert result is None
        assert status["status"] == ss.TIMED_OUT and status["reason"] == ss.UPSTREAM_TIMEOUT
        # Asked again, it asks Census again: the outage was not stored as "no match".
        asyncio.run(geo.geocode_checked(ADDRESS))
        assert len(calls) == 2
        assert cache.rows == {}
        _no_address_in(caplog)

    def test_verified_miss_is_checked(self):
        geo = _census(_MemCache())

        async def no_match(url, **_kw):
            return {"result": {"addressMatches": []}}

        geo._get_json = no_match
        result, status = asyncio.run(geo.geocode_checked(ADDRESS))
        assert result is None
        assert status["status"] == ss.CHECKED and status["matched"] == 0

    def test_route_reports_an_outage_as_502_not_matched_false(self, monkeypatch, caplog):
        import data_sources_api as api

        geo = _census(_MemCache())

        async def dead(url, **_kw):
            raise _http_error(503)

        geo._get_json = dead

        async def _geo():
            return geo

        monkeypatch.setattr(api, "_get_geocoder", _geo)
        caplog.set_level(logging.DEBUG)
        response = asyncio.run(api.geocode(address=ADDRESS, ctx=None))
        assert response.status_code == 502
        body = json.loads(response.body)
        assert body["matched"] is False
        assert body["lookup_status"] == ss.UNAVAILABLE
        assert body["degraded"] is True
        assert body["source_status"]["census_geocoder"]["reason"] in ss.REASONS
        _no_address_in(caplog)

    def test_route_keeps_a_real_miss_as_a_200(self, monkeypatch):
        import data_sources_api as api

        geo = _census(_MemCache())

        async def no_match(url, **_kw):
            return {"result": {"addressMatches": []}}

        geo._get_json = no_match

        async def _geo():
            return geo

        monkeypatch.setattr(api, "_get_geocoder", _geo)
        body = asyncio.run(api.geocode(address=ADDRESS, ctx=None))
        assert body["matched"] is False
        assert body["lookup_status"] == ss.CHECKED
        assert body["degraded"] is False


# --- Census geocoder (batch) --------------------------------------------------

def _no_match_csv(csv_bytes: bytes) -> str:
    import csv

    rows = list(csv.reader(io.StringIO(csv_bytes.decode())))
    return "\n".join(f'"{r[0]}","in","No_Match"' for r in rows)


class _PassCache:
    async def get_or_fetch(self, _ns, key, fetcher, ttl=None):  # noqa: ARG002
        return await fetcher()


def _batch_geo(post):
    from data_integrations.census_geocoder import CensusGeocoder

    geo = CensusGeocoder()
    geo._cache = _PassCache()
    geo._post_batch = post
    return geo


async def _route_batch(monkeypatch, geo, addresses):
    import data_sources_api as api

    async def _geo():
        return geo

    monkeypatch.setattr(api, "_get_geocoder", _geo)
    return await api.geocode_batch(api.BatchGeocodeRequest(addresses=addresses), ctx=None)


class TestCensusBatch:
    def test_total_outage_is_502_with_every_row_unchecked(self, monkeypatch, caplog):
        async def dead(_csv):
            raise TimeoutError()

        caplog.set_level(logging.DEBUG)
        response = asyncio.run(_route_batch(monkeypatch, _batch_geo(dead), [ADDRESS, "1 B St, X, DE"]))
        assert response.status_code == 502
        body = json.loads(response.body)
        assert body["matched"] == 0 and body["unchecked"] == 2 and body["unmatched_checked"] == 0
        assert {r["lookup_status"] for r in body["results"]} == {ss.TIMED_OUT}
        assert body["degraded"] is True
        _no_address_in(caplog)

    def test_a_failed_retry_pass_leaves_the_row_unverified(self, monkeypatch):
        posts = []

        match_row_1 = (
            '"1","in","Match","exact","1 B ST, X, DE, 19901","-75.5,39.1",'
            '"1","L","10","001","000100","1001"'
        )

        async def city_pass_dies(csv_bytes):
            posts.append(csv_bytes)
            if len(posts) == 1:  # one-line: row 1 matches, row 0 does not
                return '"0","in","No_Match"\n' + match_row_1
            if len(posts) == 2:  # then CITY dies; then zip
                raise _http_error(502)
            return _no_match_csv(csv_bytes)

        body = asyncio.run(_route_batch(monkeypatch, _batch_geo(city_pass_dies), [ADDRESS, "1 B St, X, DE"]))
        assert len(posts) == 3
        row, matched = body["results"]
        assert matched["matched"] is True and matched["lookup_status"] == ss.CHECKED
        assert row["matched"] is False
        assert row["lookup_status"] == ss.UNAVAILABLE, "a skipped retry shape is not a full check"
        assert body["unchecked"] == 1 and body["unmatched_checked"] == 0
        assert body["degraded"] is True

    def test_real_misses_stay_checked(self, monkeypatch):
        async def no_match(csv_bytes):
            return _no_match_csv(csv_bytes)

        body = asyncio.run(_route_batch(monkeypatch, _batch_geo(no_match), [ADDRESS]))
        assert body["results"][0]["lookup_status"] == ss.CHECKED
        assert body["unmatched_checked"] == 1 and body["unchecked"] == 0
        assert body["degraded"] is False

    def test_a_row_missing_from_the_census_answer_is_not_a_miss(self):
        from data_integrations.census_geocoder import CensusGeocoder

        rows = CensusGeocoder._parse_batch_csv('"0","a","No_Match"', ["a", "b"])
        assert rows[0]["lookup_status"] == ss.CHECKED
        assert rows[1]["lookup_status"] == ss.UNAVAILABLE
        assert rows[1]["lookup_reason"] == ss.BAD_RESPONSE


# --- /api/geocode (Property View) --------------------------------------------

class TestPropertyViewGeocode:
    def test_nominatim_outage_is_not_address_not_found(self, monkeypatch, mem_cache, caplog):
        from apis import geocoding

        def dead(*_a, **_k):
            raise urllib.error.URLError(TimeoutError())

        monkeypatch.setattr(geocoding.urllib.request, "urlopen", dead)
        monkeypatch.setattr(geocoding, "_last_request_time", 0.0)
        caplog.set_level(logging.DEBUG)
        result, status = asyncio.run(geocoding.geocode_checked(ADDRESS))
        assert result is None and status["status"] == ss.TIMED_OUT
        assert mem_cache.rows == {}
        _no_address_in(caplog)

    def test_route_502_for_outage_404_for_miss(self, monkeypatch):
        import server

        async def outage(_address):
            return None, ss.entry(ss.UNAVAILABLE, reason=ss.RATE_LIMITED)

        monkeypatch.setattr(server, "geocode_checked", outage)
        response = asyncio.run(server.api_geocode(address=ADDRESS, ctx=None))
        assert response.status_code == 502
        assert json.loads(response.body)["lookup_status"] == ss.UNAVAILABLE

        async def miss(_address):
            return None, ss.checked(matched=0)

        monkeypatch.setattr(server, "geocode_checked", miss)
        response = asyncio.run(server.api_geocode(address=ADDRESS, ctx=None))
        assert response.status_code == 404
        assert json.loads(response.body)["code"] == "ADDRESS_NOT_FOUND"


# --- Eviction Lab -------------------------------------------------------------

def _eviction(cache):
    from data_integrations.eviction_lab import EvictionLabSource

    src = EvictionLabSource(cache=cache)
    src._retry.max_attempts = 1
    return src


class TestEvictionLab:
    def test_outage_is_unavailable_and_not_cached(self):
        cache = _MemCache()
        src = _eviction(cache)
        calls = []

        async def dead(url, timeout=45):  # noqa: ARG001
            calls.append(url)
            raise _http_error(503)

        src._get_csv_rows = dead
        data, status = asyncio.run(src.by_fips_checked("10001"))
        assert data is None and status["status"] == ss.UNAVAILABLE
        asyncio.run(src.by_fips_checked("10001"))
        assert len(calls) == 2 and cache.rows == {}

    def test_route_502_says_not_checked(self, monkeypatch):
        import data_sources_api as api

        src = _eviction(_MemCache())

        async def dead(url, timeout=45):  # noqa: ARG001
            raise TimeoutError()

        src._get_csv_rows = dead

        async def _get():
            return src

        monkeypatch.setattr(api, "_get_eviction", _get)
        response = asyncio.run(api.eviction(fips="10001", ctx=None))
        assert response.status_code == 502
        body = json.loads(response.body)
        assert body["matched"] is False and body["lookup_status"] == ss.TIMED_OUT
        assert "NOT checked" in body["note"] and body["degraded"] is True

    def test_unknown_state_and_real_miss_stay_200(self, monkeypatch):
        import data_sources_api as api

        src = _eviction(_MemCache())

        async def rows(url, timeout=45):  # noqa: ARG001
            return [{"geoid": "10003", "year": "2016", "eviction_rate": "2.1"}]

        src._get_csv_rows = rows

        async def _get():
            return src

        monkeypatch.setattr(api, "_get_eviction", _get)
        unknown = asyncio.run(api.eviction(fips="03", ctx=None))  # no state 03
        assert unknown["lookup_status"] == ss.NOT_APPLICABLE and unknown["degraded"] is False
        miss = asyncio.run(api.eviction(fips="10001", ctx=None))
        assert miss["matched"] is False and miss["lookup_status"] == ss.CHECKED
        hit = asyncio.run(api.eviction(fips="10003", ctx=None))
        assert hit["matched"] is True and hit["record"]["eviction_rate"] == 2.1


# --- /api/enrich-property -----------------------------------------------------

class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self, *_a):
        return self._body


def _urlopen_router(routes):
    def _open(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for fragment, outcome in routes.items():
            if fragment in url:
                if isinstance(outcome, BaseException):
                    raise outcome
                return _Resp(outcome)
        raise AssertionError(f"unexpected outbound request to {url[:40]}")
    return _open


class TestEnrichProperty:
    def test_every_failure_is_reported_and_nothing_reads_as_zero(self, monkeypatch, mem_cache, caplog):
        from apis import property_data as pd

        monkeypatch.setattr(pd, "_WALKSCORE_KEY", "test-key")
        monkeypatch.setattr(pd.urllib.request, "urlopen", _urlopen_router({
            "overpass": {"remark": "runtime error: Query timed out in \"query\" after 10 seconds.",
                         "elements": []},
            "hazards.fema.gov": {"error": {"code": 500, "message": "Unable to complete operation."}},
            "walkscore": {"status": 41},
        }))
        caplog.set_level(logging.DEBUG)
        out = asyncio.run(pd.enrich_property(ADDRESS, LAT, LNG))

        assert out["pois"]["total_pois"] is None, "a failed POI query is not 0 POIs"
        assert out["pois"]["error"] == ss.UPSTREAM_TIMEOUT
        assert out["source_status"]["pois"]["status"] == ss.TIMED_OUT
        assert out["flood"] is None and out["source_status"]["flood"]["status"] == ss.UNAVAILABLE
        assert out["walkscore"] is None
        assert out["source_status"]["walkscore"]["reason"] == ss.RATE_LIMITED
        assert out["degraded"] is True
        assert mem_cache.rows == {}, "no failed answer may be cached as this location's data"
        _no_address_in(caplog)

    def test_verified_answers_are_checked(self, monkeypatch, mem_cache):
        from apis import property_data as pd

        monkeypatch.setattr(pd, "_WALKSCORE_KEY", "")
        monkeypatch.setattr(pd.urllib.request, "urlopen", _urlopen_router({
            "overpass": {"elements": [{"tags": {"total": "0"}}]},
            "hazards.fema.gov": {"features": []},
        }))
        out = asyncio.run(pd.enrich_property(ADDRESS, LAT, LNG))
        assert out["pois"]["total_pois"] == 0 and out["pois"]["lookup_status"] == ss.CHECKED
        assert out["flood"]["mapped"] is False and out["source_status"]["flood"]["status"] == ss.CHECKED
        walk = out["source_status"]["walkscore"]
        assert walk["reason"] == ss.NOT_CONFIGURED and walk["required"] is False
        assert out["degraded"] is False

    def test_get_flood_zone_keeps_its_contract(self, monkeypatch, mem_cache):
        from apis import property_data as pd

        monkeypatch.setattr(pd.urllib.request, "urlopen", _urlopen_router({
            "hazards.fema.gov": {"features": [{"attributes": {"FLD_ZONE": "AE", "SFHA_TF": "T"}}]},
        }))
        zone = asyncio.run(pd.get_flood_zone(LAT, LNG))
        assert zone["zone"] == "AE" and zone["in_sfha"] is True and zone["mapped"] is True


# --- /api/market-snapshot -----------------------------------------------------

class TestMarketSnapshot:
    def test_fred_and_treasury_failures_are_not_null_rates(self, monkeypatch, mem_cache, caplog):
        from apis import market_data as md

        monkeypatch.setattr(md, "_FRED_API_KEY", "fred-secret-key")
        monkeypatch.setattr(md.urllib.request, "urlopen", _urlopen_router({
            "stlouisfed": _http_error(500),
            "treasury.gov": TimeoutError(),
        }))
        caplog.set_level(logging.DEBUG)
        snap = asyncio.run(md.get_market_snapshot())
        assert snap["mortgage_rates"]["mortgage_30yr"] is None
        assert snap["source_status"]["fred_mortgage_30yr"]["status"] == ss.UNAVAILABLE
        assert snap["mortgage_rates"]["lookup_status"] == ss.UNAVAILABLE
        assert snap["treasury_rates"] is None
        assert snap["source_status"]["treasury"]["status"] == ss.TIMED_OUT
        assert snap["degraded"] is True
        assert mem_cache.rows == {}
        assert "fred-secret-key" not in caplog.text

    def test_unconfigured_fred_says_so(self, monkeypatch, mem_cache):
        from apis import market_data as md

        monkeypatch.setattr(md, "_FRED_API_KEY", "")
        monkeypatch.setattr(md.urllib.request, "urlopen", _urlopen_router({
            "treasury.gov": [{"field_tdr_date_value": "2026-10-09", "field_bc_10year": "4.1"}],
        }))
        snap = asyncio.run(md.get_market_snapshot())
        assert snap["source_status"]["fred_mortgage_30yr"]["reason"] == ss.NOT_CONFIGURED
        assert snap["source_status"]["treasury"]["status"] == ss.CHECKED
        assert snap["treasury_rates"]["10yr"] == 4.1
        assert snap["degraded"] is True

    def test_healthy_snapshot_is_not_degraded(self, monkeypatch, mem_cache):
        from apis import market_data as md

        monkeypatch.setattr(md, "_FRED_API_KEY", "k")
        monkeypatch.setattr(md.urllib.request, "urlopen", _urlopen_router({
            "stlouisfed": {"observations": [{"date": "2026-10-09", "value": "6.1"}]},
            "treasury.gov": [{"field_tdr_date_value": "2026-10-09", "field_bc_10year": "4.1"}],
        }))
        snap = asyncio.run(md.get_market_snapshot())
        assert snap["mortgage_rates"]["mortgage_30yr"] == 6.1
        assert snap["degraded"] is False


# --- AVM ----------------------------------------------------------------------

class _AvmResp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = body or {}

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_avm_session(monkeypatch, status=None, body=None, raises=None):
    import avm_client

    calls = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls.append(url)
            if raises is not None:
                raise raises
            return _AvmResp(status, body)

    monkeypatch.setattr(avm_client.aiohttp, "ClientSession", _Session)
    return calls


@pytest.fixture
def avm(monkeypatch, mem_cache):
    import avm_client

    avm_client._INPROC.clear()
    avm_client._credential_down_until.clear()
    monkeypatch.setenv("RENTCAST_API_KEY", "rc-key")
    monkeypatch.delenv("ATTOM_API_KEY", raising=False)
    yield avm_client
    avm_client._INPROC.clear()
    avm_client._credential_down_until.clear()


class TestAvm:
    def test_provider_outage_is_not_no_estimate(self, monkeypatch, avm, caplog):
        calls = _patch_avm_session(monkeypatch, status=503)
        caplog.set_level(logging.DEBUG)
        result = asyncio.run(avm.value_property({"address": ADDRESS}))
        assert result.verdict == "UNAVAILABLE" and result.estimated_value == 0
        assert result.lookup_status == ss.UNAVAILABLE
        assert result.source_status["rentcast"]["reason"] == ss.UPSTREAM_HTTP_ERROR
        assert result.checks["reason"] == "provider unavailable; not checked"
        assert result.degraded is True
        asyncio.run(avm.value_property({"address": ADDRESS}))
        assert len(calls) == 2, "a failed valuation is retried, not cached"
        _no_address_in(caplog)

    def test_timeout_is_timed_out(self, monkeypatch, avm):
        import aiohttp

        _patch_avm_session(monkeypatch, raises=aiohttp.ServerTimeoutError())
        result = asyncio.run(avm.value_property({"address": ADDRESS}))
        assert result.lookup_status == ss.TIMED_OUT

    @pytest.mark.parametrize("status, body", [(200, {"price": 0}), (404, None)])
    def test_provider_with_no_estimate_is_checked(self, monkeypatch, avm, status, body):
        _patch_avm_session(monkeypatch, status=status, body=body)
        result = asyncio.run(avm.value_property({"address": ADDRESS}))
        assert result.verdict == "UNAVAILABLE"
        assert result.lookup_status == ss.CHECKED
        assert result.checks["reason"] == "no provider returned a value"
        assert result.degraded is False

    def test_not_configured_says_so(self, monkeypatch, avm):
        monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
        result = asyncio.run(avm.value_property({"address": ADDRESS}))
        assert result.lookup_status == ss.UNAVAILABLE
        assert result.source_status["rentcast"]["reason"] == ss.NOT_CONFIGURED

    def test_estimate_still_flows(self, monkeypatch, avm):
        _patch_avm_session(monkeypatch, status=200, body={
            "price": 250_000, "priceRangeLow": 240_000, "priceRangeHigh": 260_000, "comparables": [],
        })
        result = asyncio.run(avm.value_property({"address": ADDRESS}))
        assert result.estimated_value == 250_000
        assert result.lookup_status == ss.CHECKED
        assert result.source_status["attom"]["status"] == ss.NOT_APPLICABLE

    def test_old_cached_rows_deserialize(self, avm):
        row = {"estimated_value": 1, "confidence_low": 1, "confidence_high": 1, "price_per_sqft": 0.0,
               "verdict": "UNVERIFIED", "confidence": 0.5, "sources": ["rentcast"], "checks": {},
               "comps_used": 0}
        result = avm._deserialize_result(row)
        assert result.lookup_status == ss.CHECKED and result.source_status == {}


# --- building footprints ------------------------------------------------------

@pytest.fixture
def footprints(monkeypatch):
    from data_integrations import building_footprint as bf

    monkeypatch.delenv("REGRID_API_TOKEN", raising=False)
    return bf


def _expired_regrid(monkeypatch):
    import data_integrations.regrid as regrid

    class _Expired:
        token_expired = True

        def token_expiry_note(self):
            return "token expired"

    monkeypatch.setenv("REGRID_API_TOKEN", "tok")
    monkeypatch.setattr(regrid, "RegridParcelSource", _Expired)


class TestFootprints:
    def test_address_only_without_a_licensed_source_is_not_checked(self, footprints, caplog):
        caplog.set_level(logging.DEBUG)
        candidates, statuses = asyncio.run(footprints.resolve_footprints_checked(address=ADDRESS))
        assert candidates == []
        assert statuses["regrid"]["reason"] == ss.NOT_CONFIGURED
        assert statuses["openstreetmap"]["reason"] == ss.NO_COORDINATES
        assert footprints.footprint_lookup(candidates, statuses) == footprints.LOOKUP_NOT_CHECKED
        _no_address_in(caplog)

    def test_expired_regrid_and_an_empty_osm_is_incomplete(self, footprints, monkeypatch):
        _expired_regrid(monkeypatch)

        async def empty(url, *, data=None):  # noqa: ARG001
            return {"elements": []}

        monkeypatch.setattr(footprints, "_fetch_json", empty)
        candidates, statuses = asyncio.run(
            footprints.resolve_footprints_checked(address=ADDRESS, lat=LAT, lon=LNG))
        assert statuses["regrid"]["reason"] == ss.CREDENTIAL_EXPIRED
        assert statuses["openstreetmap"]["status"] == ss.CHECKED
        assert footprints.footprint_lookup(candidates, statuses) == footprints.LOOKUP_INCOMPLETE
        assert ss.degraded(statuses) is True

    @pytest.mark.parametrize("outcome, status", [
        ({"remark": "runtime error: Query timed out in \"query\" at line 1", "elements": []}, ss.TIMED_OUT),
        (TimeoutError(), ss.TIMED_OUT),
        (_http_error(429), ss.UNAVAILABLE),
    ])
    def test_overpass_failure_is_not_no_building(self, footprints, monkeypatch, outcome, status):
        async def fetch(url, *, data=None):  # noqa: ARG001
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(footprints, "_fetch_json", fetch)
        candidates, statuses = asyncio.run(footprints.resolve_footprints_checked(lat=LAT, lon=LNG))
        assert statuses["openstreetmap"]["status"] == status
        assert footprints.footprint_lookup(candidates, statuses) == footprints.LOOKUP_NOT_CHECKED

    def test_checked_empty_is_none_found(self, footprints, monkeypatch):
        async def empty(url, *, data=None):  # noqa: ARG001
            return {"elements": []}

        monkeypatch.setattr(footprints, "_fetch_json", empty)
        candidates, statuses = asyncio.run(footprints.resolve_footprints_checked(lat=LAT, lon=LNG))
        assert footprints.footprint_lookup(candidates, statuses) == footprints.LOOKUP_NONE_FOUND
        assert ss.degraded(statuses) is False


LEAD = UUID("33333333-3333-4333-8333-333333333333")


class _Conn:
    async def fetchval(self, *_a):
        return 1

    async def fetchrow(self, *_a):
        return {"address": ADDRESS}


def _patch_floorplan(monkeypatch):
    import floorplan_api as fp

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(fp, "tenant_tx", fake_tx)
    return fp


class TestFootprintRoutes:
    def test_candidates_route_says_not_checked_for_an_expired_token(self, monkeypatch, footprints):
        fp = _patch_floorplan(monkeypatch)
        _expired_regrid(monkeypatch)
        body = fp.FootprintCandidatesRequest(address=ADDRESS)
        out = asyncio.run(fp.footprint_candidates(body, lead_id=LEAD, listing_id=None, ctx=None))
        assert out["count"] == 0
        assert out["lookup"] == "not_checked"
        assert out["source_status"]["regrid"]["reason"] == ss.CREDENTIAL_EXPIRED
        assert out["degraded"] is True

    def test_candidates_route_keeps_a_verified_empty(self, monkeypatch, footprints):
        fp = _patch_floorplan(monkeypatch)

        async def empty(url, *, data=None):  # noqa: ARG001
            return {"elements": []}

        monkeypatch.setattr(footprints, "_fetch_json", empty)
        body = fp.FootprintCandidatesRequest(latitude=LAT, longitude=LNG)
        out = asyncio.run(fp.footprint_candidates(body, lead_id=LEAD, listing_id=None, ctx=None))
        assert out["lookup"] == "none_found" and out["degraded"] is False

    def test_auto_dimensions_reports_a_failed_geocode_as_a_failed_osm_lookup(
        self, monkeypatch, footprints, caplog,
    ):
        fp = _patch_floorplan(monkeypatch)
        import data_integrations.cache as cache_mod

        async def no_cache():
            raise cache_mod.IntegrationCacheUnavailable("db down")

        monkeypatch.setattr(cache_mod, "get_integration_cache", no_cache)
        caplog.set_level(logging.DEBUG)
        out = asyncio.run(fp.auto_dimensions(lead_id=LEAD, listing_id=None, wall_height_m=None, ctx=None))
        assert out["geocoded"] is False
        assert out["geocode_status"]["reason"] == ss.CACHE_UNAVAILABLE
        assert out["footprint"]["found"] is False
        assert out["footprint"]["source_status"]["openstreetmap"]["status"] == ss.UNAVAILABLE
        assert out["footprint"]["lookup"] == "not_checked"
        assert out["footprint"]["degraded"] is True
        _no_address_in(caplog)

    def test_auto_dimensions_geocodes_and_asks_osm(self, monkeypatch, footprints):
        fp = _patch_floorplan(monkeypatch)
        from data_integrations.census_geocoder import CensusGeocoder

        async def located(self, address):  # noqa: ARG001
            return {"lat": LAT, "lng": LNG}, ss.checked(matched=1)

        asked = []

        async def osm(url, *, data=None):  # noqa: ARG001
            asked.append(url)
            return {"elements": []}

        monkeypatch.setattr(CensusGeocoder, "geocode_checked", located)
        monkeypatch.setattr(footprints, "_fetch_json", osm)
        import data_integrations.cache as cache_mod

        async def cache():
            return _MemCache()

        monkeypatch.setattr(cache_mod, "get_integration_cache", cache)
        out = asyncio.run(fp.auto_dimensions(lead_id=LEAD, listing_id=None, wall_height_m=None, ctx=None))
        assert out["geocoded"] is True and asked, "OSM must actually be asked once coordinates exist"
        assert out["footprint"]["lookup"] == "none_found"
