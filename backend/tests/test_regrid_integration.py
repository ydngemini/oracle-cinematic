"""Unit coverage for the server-side Regrid parcel connector."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse

import pytest
from fastapi import HTTPException

import data_sources_api
from data_integrations.cache import canonical_request_hash
from data_integrations.regrid import (
    RegridConfigurationError,
    RegridCoverageError,
    RegridParcelSource,
)


class _FixtureCache:
    def __init__(self):
        self.values: dict[str, dict] = {}
        self.requests: list[tuple[str, dict]] = []
        self._metrics = {"hits": 0, "misses": 0}

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    async def get_or_fetch(self, source, request, fetcher, **_kwargs):
        self.requests.append((source, request))
        key = canonical_request_hash(source, request)
        if key in self.values:
            self._metrics["hits"] += 1
            return self.values[key]
        self._metrics["misses"] += 1
        self.values[key] = await fetcher()
        return self.values[key]


_RAW_RESPONSE = {
    "count": 1,
    "parcels": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": 364491,
                "geometry": {"type": "Polygon", "coordinates": [[[-83.0, 42.0]]]},
                "properties": {
                    "headline": "440 Burroughs St",
                    "path": "/us/mi/wayne/detroit/364491",
                    "ll_uuid": "test-regrid-uuid",
                    "fields": {
                        "ogc_fid": 364491,
                        "parcelnumb": "02001069-71",
                        "owner": "PUBLIC RECORD OWNER",
                        "mailadd": "This must never leave the connector",
                        "address": "440 BURROUGHS ST",
                        "state2": "MI",
                        "county": "Wayne",
                        "city": "Detroit",
                        "lat": "42.3651",
                        "lon": "-83.0734",
                        "usecode": "22320",
                        "usedesc": "Office",
                        "zoning": "SD2",
                        "yearbuilt": 1926,
                        "parval": 100000,
                        "saleprice": 0,
                        "fema_flood_zone": "X",
                        "ll_last_refresh": "2026-05-22",
                    },
                },
            }
        ],
    },
}


def test_regrid_lookup_is_cached_credential_free_and_allow_listed(monkeypatch):
    async def exercise() -> None:
        monkeypatch.setenv("REGRID_API_TOKEN", "test-token")
        cache = _FixtureCache()
        source = RegridParcelSource(cache=cache)
        captured: dict[str, str] = {}

        async def fake_get_json(url, **_kwargs):
            captured["url"] = url
            return json.loads(json.dumps(_RAW_RESPONSE))

        source._get_json = fake_get_json  # type: ignore[method-assign]
        result = await source.lookup_address(
            "440 Burroughs St, Detroit, MI",
            path="/us/mi/wayne/detroit",
            limit=2,
        )
        repeated = await source.lookup_address(
            "440 Burroughs St, Detroit, MI",
            path="/us/mi/wayne/detroit",
            limit=2,
        )

        params = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        assert params["query"] == ["440 Burroughs St, Detroit, MI"]
        assert params["path"] == ["/us/mi/wayne/detroit"]
        assert params["limit"] == ["2"]
        assert params["return_enhanced_ownership"] == ["false"]
        assert params["return_matched_addresses"] == ["false"]
        assert params["return_geometry"] == ["false"]

        assert len(cache.requests) == 2
        cached_request = json.dumps(cache.requests[0][1])
        assert "test-token" not in cached_request
        assert result == repeated
        assert result["matched"] is True
        assert result["model_training_prohibited"] is True
        assert result["parcels"][0]["parcel_number"] == "02001069-71"
        assert result["parcels"][0]["owner_name"] == "PUBLIC RECORD OWNER"
        assert "geometry" not in result["parcels"][0]
        assert "mailadd" not in json.dumps(result)
        assert "This must never leave the connector" not in json.dumps(result)

    asyncio.run(exercise())


def test_regrid_requires_a_server_side_token_before_cache_or_network(monkeypatch):
    async def exercise() -> None:
        monkeypatch.delenv("REGRID_API_TOKEN", raising=False)
        cache = _FixtureCache()
        source = RegridParcelSource(cache=cache)
        with pytest.raises(RegridConfigurationError):
            await source.lookup_address("1600 Pennsylvania Avenue NW, Washington, DC")
        assert cache.requests == []

    asyncio.run(exercise())


def test_regrid_can_return_geojson_only_when_explicitly_requested(monkeypatch):
    async def exercise() -> None:
        monkeypatch.setenv("REGRID_API_TOKEN", "test-token")
        source = RegridParcelSource(cache=_FixtureCache())

        async def fake_get_json(_url, **_kwargs):
            return json.loads(json.dumps(_RAW_RESPONSE))

        source._get_json = fake_get_json  # type: ignore[method-assign]
        result = await source.lookup_address(
            "440 Burroughs St, Detroit, MI", include_geometry=True
        )
        assert result["parcels"][0]["geometry"] == _RAW_RESPONSE["parcels"]["features"][0]["geometry"]

    asyncio.run(exercise())


def test_regrid_reports_provider_coverage_denial_without_exposing_url_or_token(monkeypatch):
    async def exercise() -> None:
        monkeypatch.setenv("REGRID_API_TOKEN", "test-token")
        source = RegridParcelSource(cache=_FixtureCache())

        async def denied(_url, **_kwargs):
            raise urllib.error.HTTPError(
                "https://provider.example/hidden", 403, "Forbidden", None, None
            )

        source._get_json = denied  # type: ignore[method-assign]
        with pytest.raises(RegridCoverageError) as error:
            await source.lookup_address("440 Burroughs St, Detroit, MI")
        assert "hidden" not in str(error.value)
        assert "test-token" not in str(error.value)

    asyncio.run(exercise())


def test_regrid_route_preserves_provider_coverage_denial(monkeypatch):
    class CoverageDeniedSource:
        configured = True

        async def lookup_address(self, *_args, **_kwargs):
            raise RegridCoverageError("coverage denied")

    async def exercise() -> None:
        original = data_sources_api._regrid
        monkeypatch.setattr(data_sources_api, "_regrid", CoverageDeniedSource())
        try:
            with pytest.raises(HTTPException) as error:
                await data_sources_api.regrid_parcel(
                    address="440 Burroughs St, Detroit, MI",
                    path=None,
                    limit=1,
                    include_geometry=False,
                    ctx=object(),
                )
        finally:
            monkeypatch.setattr(data_sources_api, "_regrid", original)
        assert error.value.status_code == 403
        assert "does not include this location" in error.value.detail

    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# Credential expiry — an expired token must never read as a transient outage.
# ---------------------------------------------------------------------------
import base64 as _b64
import json as _json
import time as _time
import urllib.error as _urlerr

from data_integrations.regrid import RegridAuthError, RegridUpstreamError


def _token(exp_offset_seconds: int) -> str:
    """A Regrid-shaped JWT. Unsigned — nothing verifies it, we only read `exp`."""
    header = _b64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = _b64.urlsafe_b64encode(
        _json.dumps({"iss": "regrid.com", "exp": int(_time.time()) + exp_offset_seconds}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class TestTokenExpiry:
    def test_an_expired_token_is_not_reported_as_configured(self, monkeypatch):
        # The live defect: /api/data/health answered `configured: true` for a
        # token that had lapsed a week earlier.
        monkeypatch.setenv("REGRID_API_TOKEN", _token(-86_400))
        source = RegridParcelSource()
        assert source.token_expired is True
        assert source.configured is False

    def test_a_valid_token_is_configured(self, monkeypatch):
        monkeypatch.setenv("REGRID_API_TOKEN", _token(+20 * 86_400))
        source = RegridParcelSource()
        assert source.token_expired is False
        assert source.configured is True
        assert source.token_expiry_note() == ""   # outside the warning window

    def test_warns_inside_the_window_without_failing(self, monkeypatch):
        # Regrid issues 30-day tokens, so a warning has to arrive before the cliff.
        monkeypatch.setenv("REGRID_API_TOKEN", _token(+3 * 86_400))
        source = RegridParcelSource()
        assert source.configured is True
        assert "expires" in source.token_expiry_note()

    def test_an_opaque_token_is_not_assumed_expired(self, monkeypatch):
        # Not every credential is a JWT. Absence of an exp claim must not be
        # read as "expired" — that would break a working non-JWT token.
        monkeypatch.setenv("REGRID_API_TOKEN", "plain-opaque-token")
        source = RegridParcelSource()
        assert source.token_expires_at is None
        assert source.token_expired is False
        assert source.configured is True

    def test_expired_token_refuses_before_spending_a_request(self, monkeypatch):
        """No network call: the token already says it cannot succeed, and the
        retry ladder would otherwise hammer a credential that never will."""
        monkeypatch.setenv("REGRID_API_TOKEN", _token(-3600))
        source = RegridParcelSource()

        async def _must_not_call(*_a, **_k):
            raise AssertionError("network request attempted with an expired token")

        monkeypatch.setattr(source, "_get_json", _must_not_call)
        with pytest.raises(RegridAuthError, match="expired"):
            asyncio.run(source.fetch(address="1 Main St"))


class TestAuthFailureIsNotAnOutage:
    def test_401_raises_auth_error_not_upstream_error(self, monkeypatch):
        """The live defect: 401 fell through to RegridUpstreamError, which the
        API rendered as 502 'temporarily unavailable' — an affirmatively false
        claim, since an expired credential is permanent until rotated."""
        monkeypatch.setenv("REGRID_API_TOKEN", "plain-opaque-token")
        source = RegridParcelSource()

        async def _401(*_a, **_k):
            raise _urlerr.HTTPError("https://app.regrid.com", 401, "Unauthorized", {}, None)

        monkeypatch.setattr(source, "_get_json", _401)
        with pytest.raises(RegridAuthError) as error:
            asyncio.run(source.fetch(address="1 Main St"))
        # The message must name the credential to rotate.
        assert "REGRID_API_TOKEN" in str(error.value)

    def test_403_still_means_coverage_not_credentials(self, monkeypatch):
        # Regrid uses 403 for "valid token, no access to this geography". Folding
        # it into the auth branch would make a specific false claim about the
        # credential when the real issue is the location.
        monkeypatch.setenv("REGRID_API_TOKEN", "plain-opaque-token")
        source = RegridParcelSource()

        async def _403(*_a, **_k):
            raise _urlerr.HTTPError("https://app.regrid.com", 403, "Forbidden", {}, None)

        monkeypatch.setattr(source, "_get_json", _403)
        with pytest.raises(RegridCoverageError):
            asyncio.run(source.fetch(address="1 Main St"))

    def test_500_is_still_a_transient_outage(self, monkeypatch):
        monkeypatch.setenv("REGRID_API_TOKEN", "plain-opaque-token")
        source = RegridParcelSource()

        async def _500(*_a, **_k):
            raise _urlerr.HTTPError("https://app.regrid.com", 500, "Server Error", {}, None)

        monkeypatch.setattr(source, "_get_json", _500)
        with pytest.raises(RegridUpstreamError):
            asyncio.run(source.fetch(address="1 Main St"))
