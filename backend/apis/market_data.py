"""
Free Market Data APIs — mortgage rates, economic indicators, and housing metrics.

Sources (all free, no key required):
  1. FRED (Federal Reserve) — mortgage rates, CPI, unemployment
  2. HUD USPS Crosswalk — ZIP-to-FIPS mapping for census lookups
  3. Treasury Rates — current treasury yields (risk-free rate for cap rates)

Sources (free key):
  4. Quandl/Nasdaq — Zillow Home Value Index (free tier)

Used by:
  - Underwriter: current mortgage rate for payment calculations
  - Deal pipeline: market heat signals
  - Tour HUD: market context chips
"""

import asyncio
import json
import logging
import os
import urllib.request
from typing import Optional

logger = logging.getLogger("oracle.apis.market_data")

_FRED_API_KEY = os.environ.get("FRED_API_KEY", "")  # https://fred.stlouisfed.org/docs/api/api_key.html
_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Key FRED series
SERIES = {
    "mortgage_30yr": "MORTGAGE30US",       # 30-year fixed rate
    "mortgage_15yr": "MORTGAGE15US",       # 15-year fixed rate
    "median_home_price": "MSPUS",          # Median sales price
    "housing_starts": "HOUST",             # New housing starts
    "case_shiller": "CSUSHPINSA",          # Case-Shiller index
    "unemployment": "UNRATE",              # National unemployment
    "cpi": "CPIAUCSL",                     # Consumer Price Index
    "fed_funds_rate": "FEDFUNDS",          # Federal funds rate
}


async def get_fred_latest(series_id: str) -> Optional[dict]:
    """Get the most recent observation from a FRED series.

    Free tier: 120 req/min with API key.
    Without key: works but may be rate-limited aggressively.
    """
    if not _FRED_API_KEY:
        logger.debug("FRED_API_KEY not set — skipping %s", series_id)
        return None

    params = (
        f"?series_id={series_id}"
        f"&api_key={_FRED_API_KEY}"
        f"&file_type=json"
        f"&sort_order=desc"
        f"&limit=1"
    )
    url = f"{_FRED_BASE}{params}"

    try:
        req = urllib.request.Request(url)
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read().decode()
        )
        data = json.loads(raw)
        obs = data.get("observations", [])
        if not obs:
            return None
        return {
            "series": series_id,
            "date": obs[0]["date"],
            "value": float(obs[0]["value"]) if obs[0]["value"] != "." else None,
        }
    except Exception as e:
        logger.warning("FRED API failed for %s: %s", series_id, e)
        return None


async def get_current_mortgage_rates() -> dict:
    """Get current 30yr and 15yr mortgage rates from FRED."""
    rate_30, rate_15 = await asyncio.gather(
        get_fred_latest(SERIES["mortgage_30yr"]),
        get_fred_latest(SERIES["mortgage_15yr"]),
        return_exceptions=True,
    )

    return {
        "mortgage_30yr": rate_30.get("value") if isinstance(rate_30, dict) else None,
        "mortgage_15yr": rate_15.get("value") if isinstance(rate_15, dict) else None,
        "as_of": rate_30.get("date") if isinstance(rate_30, dict) else None,
        "source": "fred",
    }


# ---------------------------------------------------------------------------
# BLS — Bureau of Labor Statistics (Local Area Unemployment Statistics / LAUS)
#
# v1 is KEYLESS and public-domain (25 queries/day per IP). Setting BLS_API_KEY
# (free, instant registration) upgrades to v2 (500 queries/day). v1 MUST work
# without a key — get_local_unemployment degrades to v1 when no key is set.
# ---------------------------------------------------------------------------
_BLS_API_KEY = os.environ.get("BLS_API_KEY", "")  # https://data.bls.gov/registrationEngine/
_BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
_BLS_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# USPS postal -> 2-digit state FIPS (for statewide LAUS series construction).
_STATE_POSTAL_TO_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}

# A few verified metro LAUS unemployment-rate series (not seasonally adjusted,
# measure code 03). Callers can also pass any raw LAUS series id directly.
_KNOWN_METROS = {
    "new-york": "LAUMT363562000000003",      # New York-Newark-Jersey City
    "los-angeles": "LAUMT063108000000003",   # Los Angeles-Long Beach-Anaheim
    "chicago": "LAUMT171698000000003",       # Chicago-Naperville-Elgin
}


def _bls_resolve_series(area: str) -> Optional[str]:
    """Resolve an area spec to a BLS LAUS unemployment-rate series id.

    Accepts (in priority order):
      - a raw LAUS series id (e.g. 'LASST060000000000003', 'LAUMT...')
      - a 2-letter state postal code -> statewide series (seasonally adjusted)
      - a known metro slug ('new-york', 'los-angeles', 'chicago')
    """
    a = (area or "").strip()
    if not a:
        return None
    if a[:2].upper() == "LA" and len(a) >= 18:
        return a.upper()
    if len(a) == 2 and a.isalpha():
        fips = _STATE_POSTAL_TO_FIPS.get(a.upper())
        if fips:
            return f"LASST{fips}0000000000003"  # statewide unemployment rate (SA)
    return _KNOWN_METROS.get(a.lower())


async def _bls_post(payload: dict) -> Optional[dict]:
    url = _BLS_V2 if _BLS_API_KEY else _BLS_V1
    if _BLS_API_KEY:
        payload = {**payload, "registrationkey": _BLS_API_KEY}
    body = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=15).read().decode()
        )
        return json.loads(raw)
    except Exception as e:
        logger.warning("BLS API request failed: %s", e)
        return None


async def get_local_unemployment(
    area: str,
    *,
    start_year: Optional[str] = None,
    end_year: Optional[str] = None,
) -> dict:
    """Local unemployment rate (BLS LAUS) for a state, metro, or raw series id.

    Keyless v1 (25 q/day); BLS_API_KEY upgrades to v2 (500 q/day). Returns the
    latest observation plus a short trailing history, or a clean unresolved/
    upstream envelope — never raises.
    """
    series_id = _bls_resolve_series(area)
    tier = "v2" if _BLS_API_KEY else "v1"
    limit_note = (
        "BLS v1 keyless: 25 queries/day" if not _BLS_API_KEY
        else "BLS v2 keyed: 500 queries/day"
    )
    if not series_id:
        return {
            "area": area,
            "resolved": False,
            "error": "unknown area — pass a 2-letter state code, a known metro "
                     "slug (new-york/los-angeles/chicago), or a raw BLS LAUS series id",
            "source": "bls_laus",
        }

    payload = {"seriesid": [series_id]}
    if start_year:
        payload["startyear"] = str(start_year)
    if end_year:
        payload["endyear"] = str(end_year)

    data = await _bls_post(payload)
    if not data:
        return {"area": area, "resolved": True, "series_id": series_id,
                "value": None, "error": "BLS upstream unavailable",
                "api_tier": tier, "rate_limit_note": limit_note, "source": "bls_laus"}

    series = (data.get("Results") or {}).get("series") or []
    points = series[0].get("data") if series else None
    if not points:
        return {"area": area, "resolved": True, "series_id": series_id, "value": None,
                "note": data.get("message") or "no observations for this series",
                "api_tier": tier, "rate_limit_note": limit_note, "source": "bls_laus"}

    latest = points[0]  # BLS returns most-recent-first
    return {
        "area": area,
        "resolved": True,
        "series_id": series_id,
        "measure": "unemployment_rate_pct",
        "value": _safe_float(latest.get("value")),
        "period": latest.get("periodName"),
        "year": latest.get("year"),
        "history": [
            {"year": p.get("year"), "period": p.get("periodName"),
             "value": _safe_float(p.get("value"))}
            for p in points[:13]
        ],
        "api_tier": tier,
        "rate_limit_note": limit_note,
        "source": "bls_laus",
    }


async def get_treasury_rates() -> Optional[dict]:
    """Get current US Treasury rates from treasury.gov XML feed (no key needed)."""
    url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all/2026?type=daily_treasury_yield_curve&field_tdr_date_value=2026&page&_format=json"

    try:
        req = urllib.request.Request(url)
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read().decode()
        )
        data = json.loads(raw)
        if not data:
            return None
        latest = data[-1] if isinstance(data, list) else None
        if not latest:
            return None
        return {
            "date": latest.get("field_tdr_date_value", ""),
            "1mo": _safe_float(latest.get("field_bc_1month")),
            "1yr": _safe_float(latest.get("field_bc_1year")),
            "5yr": _safe_float(latest.get("field_bc_5year")),
            "10yr": _safe_float(latest.get("field_bc_10year")),
            "30yr": _safe_float(latest.get("field_bc_30year")),
            "source": "treasury.gov",
        }
    except Exception as e:
        logger.warning("Treasury rates fetch failed: %s", e)
        return None


async def get_market_snapshot() -> dict:
    """Aggregate market data from all free sources."""
    mortgage, treasury = await asyncio.gather(
        get_current_mortgage_rates(),
        get_treasury_rates(),
        return_exceptions=True,
    )

    return {
        "mortgage_rates": mortgage if isinstance(mortgage, dict) else None,
        "treasury_rates": treasury if isinstance(treasury, dict) else None,
    }


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
