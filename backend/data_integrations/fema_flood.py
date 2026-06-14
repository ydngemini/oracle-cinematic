"""
data_integrations/fema_flood.py — FEMA National Flood Hazard Layer lookup.

API: https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query
Cache TTL: 30 days (zones rarely change).
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache, TTL

_NFHL_QUERY_URL = (
    "https://hazards.fema.gov/gis/nfhl/rest/services/public/"
    "NFHL/MapServer/28/query"
)

_RISK_MAP: dict[str, str] = {
    "A": "high", "AE": "high", "AH": "high",
    "AO": "high", "AR": "high", "A99": "high",
    "V": "very_high", "VE": "very_high", "VO": "very_high",
    "B": "moderate", "X500": "moderate",
    "C": "minimal", "X": "minimal",
    "D": "undetermined",
}

_MANDATORY_PURCHASE: frozenset[str] = frozenset({
    "A", "AE", "AH", "AO", "AR", "A99", "V", "VE", "VO",
})


class FemaFloodSource(DataSource):
    source_name = "fema_flood"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.5, jitter=0.2),
            retry_config=RetryConfig(max_attempts=4, base_backoff=3.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return TTL["fema_flood"]

    async def fetch(self, *, lat: float, lng: float) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,FIRM_PAN,EFF_DATE",
            "returnGeometry": "false",
            "f": "json",
        })
        return await self._get_json(f"{_NFHL_QUERY_URL}?{params}", timeout=12)

    def normalize(self, raw: dict) -> dict:
        features = raw.get("features", [])
        if not features:
            return {
                "zone": "X",
                "zone_subtype": "",
                "risk": "minimal",
                "in_sfha": False,
                "mandatory_flood_insurance": False,
                "firm_panel": "",
                "effective_date": "",
                "source": "fema_nfhl",
            }

        attrs = features[0].get("attributes", {})
        zone = (attrs.get("FLD_ZONE") or "X").strip()
        return {
            "zone": zone,
            "zone_subtype": (attrs.get("ZONE_SUBTY") or "").strip(),
            "risk": _RISK_MAP.get(zone, "unknown"),
            "in_sfha": attrs.get("SFHA_TF", "F") == "T",
            "mandatory_flood_insurance": zone in _MANDATORY_PURCHASE,
            "firm_panel": (attrs.get("FIRM_PAN") or "").strip(),
            "effective_date": str(attrs.get("EFF_DATE") or ""),
            "source": "fema_nfhl",
        }

    @staticmethod
    def cache_key(lat: float, lng: float) -> str:
        return f"fema_flood:lat={lat:.4f},lng={lng:.4f}"
