"""
data_integrations/geocoder.py — Cascading geocoder (Census → Nominatim).

Census Geocoder returns authoritative FIPS codes. Nominatim is the fallback.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache, TTL


class _CensusGeocoder(DataSource):
    source_name = "census_geocoder"

    _URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.05, jitter=0.02),
            retry_config=RetryConfig(max_attempts=3, base_backoff=2.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return TTL["geocode"]

    async def fetch(self, *, address: str) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "address": address,
            "benchmark": "2020",
            "vintage": "Census2020_Census2020",
            "layers": "all",
            "format": "json",
        })
        try:
            data = await self._get_json(f"{self._URL}?{params}", timeout=12)
            matches = data.get("result", {}).get("addressMatches", [])
            return matches[0] if matches else None
        except Exception as e:
            self._log.debug("Census geocoder failed for '%s': %s", address, e)
            return None

    def normalize(self, raw: dict) -> dict:
        coords = raw.get("coordinates", {})
        addr = raw.get("addressComponents", {})
        geo = raw.get("geographies", {})
        tracts = geo.get("Census Tracts", [{}])
        blocks = geo.get("2020 Census Blocks", [{}])
        return {
            "lat": float(coords.get("y", 0)),
            "lng": float(coords.get("x", 0)),
            "display_name": raw.get("matchedAddress", ""),
            "address_normalized": raw.get("matchedAddress", ""),
            "state_fips": addr.get("state", ""),
            "county_fips": addr.get("county", ""),
            "zip": addr.get("zip", ""),
            "tract": (tracts[0] if tracts else {}).get("TRACT", ""),
            "block": (blocks[0] if blocks else {}).get("BLOCK", ""),
            "source": "census_geocoder",
        }


class _NominatimGeocoder(DataSource):
    source_name = "nominatim"

    _URL = "https://nominatim.openstreetmap.org/search"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=1.1, jitter=0.3),
            retry_config=RetryConfig(max_attempts=3, base_backoff=3.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return TTL["geocode"]

    async def fetch(self, *, address: str) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "q": address,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
            "countrycodes": "us",
        })
        try:
            results = await self._get_json(f"{self._URL}?{params}", timeout=10)
            return results[0] if results else None
        except Exception as e:
            self._log.debug("Nominatim failed for '%s': %s", address, e)
            return None

    def normalize(self, raw: dict) -> dict:
        parts = raw.get("address", {})
        return {
            "lat": float(raw.get("lat", 0)),
            "lng": float(raw.get("lon", 0)),
            "display_name": raw.get("display_name", ""),
            "address_normalized": raw.get("display_name", ""),
            "state_fips": "",
            "county_fips": "",
            "zip": parts.get("postcode", ""),
            "tract": "",
            "block": "",
            "source": "nominatim",
        }


class CascadingGeocoder:
    def __init__(self, cache: Optional[IntegrationCache] = None):
        self._census = _CensusGeocoder(cache=cache)
        self._nominatim = _NominatimGeocoder(cache=cache)

    @staticmethod
    def _cache_key(address: str) -> str:
        normalized = address.strip().upper()
        return f"geocode:{urllib.parse.quote(normalized)}"

    async def geocode(self, address: str) -> Optional[dict]:
        key = self._cache_key(address)
        result = await self._census.get(key, address=address)
        if result:
            return result
        return await self._nominatim.get(key, address=address)

    def metrics(self) -> dict:
        return {
            "census_geocoder": self._census.metrics(),
            "nominatim": self._nominatim.metrics(),
        }
