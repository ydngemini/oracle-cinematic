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


def _upstream(monkeypatch, *, body=None, error=None):
    """Census at the socket: urlopen answers `body` or raises `error`. The cache
    is bypassed (it calls the fetcher), so the module's own handling is tested."""
    async def _no_cache(source, request, fetcher, **_kw):
        return await fetcher()

    monkeypatch.setattr(integration_cache, "cached_external", _no_cache)

    def _urlopen(_req, timeout=None):
        if error is not None:
            raise error
        return io.BytesIO(body)

    monkeypatch.setattr(census.urllib.request, "urlopen", _urlopen)


def test_an_outage_raises_rather_than_reading_as_no_data(monkeypatch):
    _upstream(monkeypatch, error=urllib.error.URLError("Name or service not known"))
    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_an_http_error_from_census_is_an_outage(monkeypatch):
    _upstream(monkeypatch, error=urllib.error.HTTPError(
        "https://api.census.gov/data", 503, "Service Unavailable", {}, None))
    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_a_204_with_no_body_is_an_answer_of_no_data(monkeypatch):
    """Census answers a geography it has no data for with 204 No Content."""
    _upstream(monkeypatch, body=b"")
    assert asyncio.run(census.get_demographics_by_zip("00000")) is None


def test_a_header_with_no_row_is_no_data(monkeypatch):
    _upstream(monkeypatch, body=json.dumps([_HEADER]).encode())
    assert asyncio.run(census.get_demographics_by_zip("00000")) is None


def test_a_non_json_answer_is_an_outage_not_no_data(monkeypatch):
    _upstream(monkeypatch, body=b"<html>maintenance</html>")
    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_a_ragged_row_is_an_outage_not_a_half_filled_answer(monkeypatch):
    _upstream(monkeypatch, body=json.dumps([_HEADER, _row()[:-3]]).encode())
    with pytest.raises(census.CensusUnavailable):
        asyncio.run(census.get_demographics_by_zip("19901"))


def test_a_real_row_is_read(monkeypatch):
    code = census._VARIABLES
    row = _row(**{code["total_housing_units"]: "200", code["owner_occupied"]: "150",
                  code["vacant_units"]: "10", code["median_rent"]: "-666666666"})
    _upstream(monkeypatch, body=json.dumps([_HEADER, row]).encode())

    result = asyncio.run(census.get_demographics_by_zip("19901"))

    assert result["zip"] == "19901" and result["name"] == "ZCTA5 19901"
    assert result["owner_occupied_pct"] == 75.0 and result["vacancy_rate_pct"] == 5.0
    assert result["median_rent"] is None          # the ACS "not available" sentinel


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
