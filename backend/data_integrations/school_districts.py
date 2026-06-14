"""
data_integrations/school_districts.py — NCES EDGE school district lookup.

Uses the NCES ArcGIS FeatureServer for authoritative boundary queries.
Cache TTL: 180 days.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache, TTL


class NCESDistrictSource(DataSource):
    source_name = "nces_districts"

    _FEATURE_URL = (
        "https://services1.arcgis.com/Ua5sjt3LWTPigjyD/arcgis/rest/"
        "services/EDGE_GEOCODE_UNIFIEDSCHDIST_TL23/FeatureServer/0/query"
    )

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.5, jitter=0.2),
            retry_config=RetryConfig(max_attempts=4, base_backoff=5.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return TTL["school_district"]

    async def fetch(self, *, lat: float, lng: float) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelWithin",
            "outFields": "LEAID,NAME,STATEFP,COUNTYFP,LOGRADE,HIGRADE,UNSDLEA",
            "returnGeometry": "false",
            "f": "json",
        })
        try:
            return await self._get_json(f"{self._FEATURE_URL}?{params}", timeout=15)
        except Exception as e:
            self._log.warning("NCES district lookup failed (%f, %f): %s", lat, lng, e)
            return None

    def normalize(self, raw: dict) -> dict:
        features = raw.get("features", [])
        if not features:
            return {"district_name": None, "leaid": None, "source": "nces_edge"}

        attrs = features[0].get("attributes", {})
        return {
            "district_name": attrs.get("NAME", ""),
            "leaid": attrs.get("LEAID", ""),
            "state_fips": str(attrs.get("STATEFP", "") or ""),
            "county_fips": str(attrs.get("COUNTYFP", "") or ""),
            "low_grade": attrs.get("LOGRADE", ""),
            "high_grade": attrs.get("HIGRADE", ""),
            "source": "nces_edge",
        }

    @staticmethod
    def cache_key(lat: float, lng: float) -> str:
        return f"school_district:lat={lat:.4f},lng={lng:.4f}"

    async def lookup(self, lat: float, lng: float) -> Optional[dict]:
        key = self.cache_key(lat, lng)
        return await self.get(key, lat=lat, lng=lng)
