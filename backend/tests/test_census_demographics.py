"""/api/demographics tells "Census has no row for this ZIP" from "Census did not answer".

Both used to come back as None from apis.census, and the route turned None into
404 "no data for this zip" — so every Census outage was reported as a fact about
the ZIP code. An outage is now a 502 (the /api/flood-zone shape); only an
answer with no row is a 404.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest

import data_integrations.cache as integration_cache
from apis import census
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="agent@tenant.test",
                    tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)

_HEADER = ["NAME", *census._VARIABLES.values(), "zip code tabulation area"]


def _row(**overrides):
    values = {code: "100" for code in census._VARIABLES.values()}
    values.update(overrides)
    return ["ZCTA5 19901", *values.values(), "19901"]


def _upstream(monkeypatch, *, body=None, error=None, bodies=None):
    """Census at the socket: urlopen answers `body` (or each of `bodies` in
    turn) or raises `error`. The cache stand-in stores what the fetcher
    returns and serves it after, as the real one does, and records it."""
    stored: dict = {}
    calls = {"urlopen": 0}

    async def _cache(source, request, fetcher, **_kw):
        key = repr(sorted(request.items()))
        if key not in stored:
            stored[key] = await fetcher()
        return stored[key]

    monkeypatch.setattr(integration_cache, "cached_external", _cache)
    queue = list(bodies) if bodies is not None else None

    def _urlopen(_req, timeout=None):
        calls["urlopen"] += 1
        if error is not None:
            raise error
        return io.BytesIO(queue.pop(0) if queue is not None else body)

    monkeypatch.setattr(census.urllib.request, "urlopen", _urlopen)
    return stored, calls


def test_an_outage_raises_rather_than_reading_as_no_data(monkeypatch):
    _upstream(monkeypatch, error=urllib.error.URLError("Name or service not known"))
    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_an_http_error_from_census_is_an_outage(monkeypatch):
    _upstream(monkeypatch, error=urllib.error.HTTPError(
        "https://api.census.gov/data", 503, "Service Unavailable", {}, None))
    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_a_204_with_no_body_is_an_answer_of_no_data_cached_briefly(monkeypatch):
    """Census answers a geography it has no data for with 204 No Content. Empty
    results are marked for the cache's short TTL, not the source's 365 days."""
    stored, _ = _upstream(monkeypatch, body=b"")
    assert asyncio.run(census.get_demographics_by_zip("00000")) is None
    assert [v.get("_cacheable_empty") for v in stored.values()] == [True]


def test_a_header_with_no_row_is_no_data_cached_briefly(monkeypatch):
    stored, _ = _upstream(monkeypatch, body=json.dumps([_HEADER]).encode())
    assert asyncio.run(census.get_demographics_by_zip("00000")) is None
    assert [v.get("_cacheable_empty") for v in stored.values()] == [True]


@pytest.mark.parametrize("body", [
    b"<html>maintenance</html>",
    b'{"error": "x", "detail": "y"}',
    b'"just a string"',
    json.dumps([_HEADER, _row()[:-3]]).encode(),            # a ragged row
])
def test_an_unreadable_answer_is_an_outage_and_is_not_cached(monkeypatch, body):
    """Checked inside the fetch: a malformed answer must not sit in the cache
    for 365 days answering 'unavailable'. The next request asks Census again."""
    stored, calls = _upstream(monkeypatch, bodies=[body, json.dumps([_HEADER, _row()]).encode()])

    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))
    assert stored == {}

    assert asyncio.run(census.get_demographics_by_zip("19901"))["zip"] == "19901"
    assert calls["urlopen"] == 2


def test_a_failing_cache_is_the_caches_503_not_a_census_outage(monkeypatch):
    """Census was never asked; the app's IntegrationCacheUnavailable handler
    answers 503 INTEGRATION_CACHE_UNAVAILABLE and logs the cause."""
    async def _broken(*_a, **_kw):
        raise integration_cache.IntegrationCacheUnavailable("no database pool")

    monkeypatch.setattr(integration_cache, "cached_external", _broken)
    with pytest.raises(integration_cache.IntegrationCacheUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_a_real_row_is_read(monkeypatch):
    """As the ACS sends it: every value a string, median rooms to one decimal,
    and negative annotation codes meaning 'not available'."""
    code = census._VARIABLES
    row = _row(**{code["total_housing_units"]: "200", code["owner_occupied"]: "150",
                  code["vacant_units"]: "10", code["median_rooms"]: "5.6",
                  code["median_home_value"]: "312500",
                  code["median_rent"]: "-666666666",
                  code["median_household_income"]: "-999999999"})
    _upstream(monkeypatch, body=json.dumps([_HEADER, row]).encode())

    result = asyncio.run(census.get_demographics_by_zip("19901"))

    assert result["zip"] == "19901" and result["name"] == "ZCTA5 19901"
    assert result["median_rooms"] == 5.6
    assert result["median_home_value"] == 312500 and isinstance(result["median_home_value"], int)
    assert result["median_rent"] is None and result["median_household_income"] is None
    assert result["owner_occupied_pct"] == 75.0 and result["vacancy_rate_pct"] == 5.0


def test_one_unreadable_value_is_none_not_an_outage(monkeypatch, caplog):
    code = census._VARIABLES
    _upstream(monkeypatch, body=json.dumps(
        [_HEADER, _row(**{code["median_rent"]: "N"})]).encode())

    with caplog.at_level("WARNING", logger="oracle.apis.census"):
        result = asyncio.run(census.get_demographics_by_zip("19901"))

    assert result["median_rent"] is None
    assert result["total_population"] == 100
    assert "median_rent" in caplog.text


# --- the route ---------------------------------------------------------------

def _route(monkeypatch, outcome):
    import server

    async def _demographics(_zip):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(server, "get_demographics_by_zip", _demographics)
    response = asyncio.run(server.api_demographics("19901", ctx=CTX))
    return response.status_code, json.loads(response.body)


def test_the_route_answers_an_outage_with_502(monkeypatch):
    status, body = _route(monkeypatch, census.CensusUnavailable("the Census API did not answer"))
    assert status == 502
    assert body == {"error": "census data unavailable"}


def test_the_route_keeps_404_for_a_zip_with_no_data(monkeypatch):
    status, body = _route(monkeypatch, None)
    assert status == 404
    assert body == {"error": "no data for this zip"}


def test_the_route_returns_a_real_answer(monkeypatch):
    status, body = _route(monkeypatch, {"zip": "19901", "name": "ZCTA5 19901"})
    assert status == 200 and body["zip"] == "19901"
