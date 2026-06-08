"""
US Census Bureau API — free neighborhood demographics and housing data.

API Key: Optional but recommended for higher rate limits.
Get one free at: https://api.census.gov/data/key_signup.html
Set env: CENSUS_API_KEY

Provides:
  - Median home value by tract/zip
  - Median household income
  - Population demographics
  - Housing unit counts (owner-occupied vs renter)
  - Vacancy rates

Used by:
  - Deal pipeline: ARV comps enrichment
  - Underwriter: market context for the 70% rule
  - Tour generator: neighborhood stats for narration
"""

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger("oracle.apis.census")

_BASE_URL = "https://api.census.gov/data"
_API_KEY = os.environ.get("CENSUS_API_KEY", "")

# ACS 5-Year Estimates — most reliable for small geographies
_ACS_YEAR = "2022"
_ACS_DATASET = f"{_ACS_YEAR}/acs/acs5"

# Key variable codes
_VARIABLES = {
    "median_home_value": "B25077_001E",
    "median_household_income": "B19013_001E",
    "total_population": "B01003_001E",
    "total_housing_units": "B25001_001E",
    "owner_occupied": "B25003_002E",
    "renter_occupied": "B25003_003E",
    "vacant_units": "B25002_003E",
    "median_rent": "B25064_001E",
    "median_rooms": "B25018_001E",
    "built_2000_or_later": "B25034_002E",
}


async def get_demographics_by_zip(zip_code: str) -> Optional[dict]:
    """Fetch key housing and demographic stats for a ZIP code."""
    var_list = ",".join(_VARIABLES.values())
    params = {
        "get": f"NAME,{var_list}",
        "for": f"zip code tabulation area:{zip_code}",
    }
    if _API_KEY:
        params["key"] = _API_KEY

    url = f"{_BASE_URL}/{_ACS_DATASET}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url)
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=15).read().decode()
        )
        data = json.loads(raw)
        if len(data) < 2:
            return None

        headers = data[0]
        values = data[1]
        row = dict(zip(headers, values))

        result = {"zip": zip_code, "name": row.get("NAME", "")}
        for name, code in _VARIABLES.items():
            val = row.get(code)
            result[name] = int(val) if val and val != "-666666666" else None

        # Compute derived metrics
        total_housing = result.get("total_housing_units") or 0
        if total_housing > 0:
            owner = result.get("owner_occupied") or 0
            vacant = result.get("vacant_units") or 0
            result["owner_occupied_pct"] = round(owner / total_housing * 100, 1)
            result["vacancy_rate_pct"] = round(vacant / total_housing * 100, 1)

        return result
    except Exception as e:
        logger.warning("Census API failed for zip %s: %s", zip_code, e)
        return None


async def get_demographics_by_state_county(state_fips: str, county_fips: str) -> Optional[dict]:
    """Fetch county-level demographics."""
    var_list = ",".join(_VARIABLES.values())
    params = {
        "get": f"NAME,{var_list}",
        "for": f"county:{county_fips}",
        "in": f"state:{state_fips}",
    }
    if _API_KEY:
        params["key"] = _API_KEY

    url = f"{_BASE_URL}/{_ACS_DATASET}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url)
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=15).read().decode()
        )
        data = json.loads(raw)
        if len(data) < 2:
            return None

        headers = data[0]
        values = data[1]
        row = dict(zip(headers, values))

        result = {"state_fips": state_fips, "county_fips": county_fips, "name": row.get("NAME", "")}
        for name, code in _VARIABLES.items():
            val = row.get(code)
            result[name] = int(val) if val and val != "-666666666" else None

        return result
    except Exception as e:
        logger.warning("Census API failed for %s/%s: %s", state_fips, county_fips, e)
        return None


# Delaware FIPS for the primary market
DE_STATE_FIPS = "10"
DE_COUNTIES = {
    "New Castle": "003",
    "Kent": "001",
    "Sussex": "005",
}
