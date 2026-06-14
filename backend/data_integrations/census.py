"""
data_integrations/census.py — US Census Bureau ACS 5-yr demographics.

API: https://api.census.gov/data/{year}/acs/acs5
Cache TTL: 365 days (annual dataset).
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache, TTL

_BASE = "https://api.census.gov/data"
_ACS_YEAR = "2022"
_ACS_DATASET = f"{_ACS_YEAR}/acs/acs5"
_API_KEY = os.environ.get("CENSUS_API_KEY", "")

_ACS_VARS: dict[str, str] = {
    "median_home_value": "B25077_001E",
    "median_household_income": "B19013_001E",
    "total_population": "B01003_001E",
    "total_housing_units": "B25001_001E",
    "owner_occupied": "B25003_002E",
    "renter_occupied": "B25003_003E",
    "vacant_units": "B25002_003E",
    "median_rent": "B25064_001E",
    "built_2000_or_later": "B25034_002E",
    "bachelors_or_higher": "B15003_022E",
    "employed_population": "B23025_004E",
    "commute_total": "B08301_001E",
    "commute_public_transit": "B08301_010E",
}

_NULL_SENTINEL = "-666666666"


def _parse_int(val) -> Optional[int]:
    if val is None or str(val) == _NULL_SENTINEL:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


class CensusACSSource(DataSource):
    source_name = "census_acs"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.05, jitter=0.02),
            retry_config=RetryConfig(max_attempts=4, base_backoff=5.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return TTL["census_acs"]

    def _build_url(self, for_clause: str, in_clause: Optional[str] = None) -> str:
        var_list = "NAME," + ",".join(_ACS_VARS.values())
        params: dict = {"get": var_list, "for": for_clause}
        if in_clause:
            params["in"] = in_clause
        if _API_KEY:
            params["key"] = _API_KEY
        return f"{_BASE}/{_ACS_DATASET}?{urllib.parse.urlencode(params)}"

    async def fetch(self, *, geo_type: str, geo_id: str, in_clause: Optional[str] = None) -> Optional[dict]:
        url = self._build_url(f"{geo_type}:{geo_id}", in_clause)
        try:
            data = await self._get_json(url, timeout=15)
            if not isinstance(data, list) or len(data) < 2:
                return None
            return {"headers": data[0], "values": data[1]}
        except Exception as e:
            self._log.warning("ACS fetch failed (%s %s): %s", geo_type, geo_id, e)
            return None

    def normalize(self, raw: dict) -> dict:
        row = dict(zip(raw["headers"], raw["values"]))
        result: dict = {"name": row.get("NAME", "")}
        for name, code in _ACS_VARS.items():
            result[name] = _parse_int(row.get(code))

        total = result.get("total_housing_units") or 0
        if total > 0:
            owner = result.get("owner_occupied") or 0
            vacant = result.get("vacant_units") or 0
            result["owner_occupied_pct"] = round(owner / total * 100, 1)
            result["vacancy_rate_pct"] = round(vacant / total * 100, 1)

        result["source"] = "census_acs_5yr"
        result["dataset_year"] = _ACS_YEAR
        return result

    async def get_by_zip(self, zip_code: str) -> Optional[dict]:
        key = f"census_acs:zip={zip_code}"
        return await self.get(key, geo_type="zip code tabulation area", geo_id=zip_code)

    async def get_by_county(self, state_fips: str, county_fips: str) -> Optional[dict]:
        key = f"census_acs:county={state_fips}{county_fips}"
        return await self.get(key, geo_type="county", geo_id=county_fips, in_clause=f"state:{state_fips}")

    async def get_by_tract(self, state_fips: str, county_fips: str, tract: str) -> Optional[dict]:
        key = f"census_acs:tract={state_fips}{county_fips}{tract}"
        return await self.get(
            key, geo_type="tract", geo_id=tract,
            in_clause=f"state:{state_fips} county:{county_fips}",
        )
