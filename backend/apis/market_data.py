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
