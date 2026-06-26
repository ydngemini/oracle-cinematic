"""
data_integrations/eviction_lab.py — Eviction Lab eviction-rate aggregates (KEYLESS).

The Eviction Lab at Princeton publishes ~99M court records distilled into
state/county/tract/block-group EVICTION-RATE aggregates as flat CSV files in a
public S3 bucket (no key, no signup). Licensed ODC-BY 1.0 — commercial use is
permitted with attribution ("Eviction Lab at Princeton University,
evictionlab.org").

IMPORTANT framing: this is a NEIGHBORHOOD DISTRESS OVERLAY, not a parcel/lead
source. The records are area aggregates keyed by Census FIPS (GEOID); they say
"this tract/county had an N% eviction rate", not "this owner is being evicted".
Use it to score the *area* around a parcel, never as a lead by itself.

Download host (S3 origin behind the data-downloads.evictionlab.org alias):
  https://eviction-lab-data-downloads.s3.amazonaws.com/legacy-data/...

  validated/counties/{ST}_counties.csv          GEOID = 5-digit county FIPS
  validated/tracts/{ST}_tracts.csv              GEOID = 11-digit tract FIPS
  validated/block-groups/{ST}_block-groups.csv  GEOID = 12-digit BG FIPS
  unvalidated/states.csv (all states, one file) GEOID = 2-digit state FIPS

Columns vary slightly between files (validated uses dotted names,
e.g. `eviction.rate`; the states file uses hyphens, e.g. `eviction-rate`); the
header is normalised to snake_case so both parse identically.

The pulled per-(state, geography) slice is cached through IntegrationCache
(Redis L1 / di_cache L2) — one CSV download serves every FIPS lookup in that
state for the TTL window, so no per-lookup table is needed.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import urllib.error
import urllib.request
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig, RetryableError, _parse_retry_after
from .cache import IntegrationCache

_ROOT = "https://eviction-lab-data-downloads.s3.amazonaws.com"

# Legacy estimates are a static, historical dataset — they never change. A long
# TTL keeps the S3 traffic to one pull per state/geography per month.
_TTL = 30 * 86_400

# 2-digit state FIPS -> USPS postal code (the per-state file path uses postal).
_FIPS_TO_POSTAL: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}

# FIPS length -> (geography label, path builder using a postal code).
_GEOG_BY_LEN = {
    2: "states",
    5: "counties",
    11: "tracts",
    12: "block-groups",
}

# Numeric fields lifted out of each CSV row into the compact overlay record.
_FLOAT_FIELDS = (
    "eviction_rate", "eviction_filing_rate", "eviction_filings", "evictions",
    "renter_occupied_households", "pct_renter_occupied", "population",
    "poverty_rate", "median_gross_rent", "median_household_income",
    "median_property_value", "rent_burden",
)


def _norm_key(h: str) -> str:
    return re.sub(r"[.\-\s]+", "_", (h or "").strip().lower())


def _f(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("na", "nan", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class EvictionLabSource(DataSource):
    source_name = "eviction_lab"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.5, jitter=0.2),
            retry_config=RetryConfig(max_attempts=3, base_backoff=2.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return _TTL

    def _url(self, geography: str, postal: Optional[str]) -> str:
        if geography == "states":
            return f"{_ROOT}/legacy-data/unvalidated/states.csv"
        return f"{_ROOT}/legacy-data/validated/{geography}/{postal}_{geography}.csv"

    async def _get_csv_rows(self, url: str, timeout: int = 45) -> list[dict]:
        """Download a CSV and parse it into snake_case-keyed dict rows."""
        async def _do() -> str:
            await self._limiter.acquire()
            self._metrics["requests"] += 1
            req = urllib.request.Request(url, headers={"User-Agent": self._USER_AGENT})

            def _blocking() -> bytes:
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503):
                        ra = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
                        raise RetryableError(f"HTTP {e.code} from {self.source_name}", ra)
                    raise

            body = await asyncio.to_thread(_blocking)
            return body.decode("utf-8", errors="replace")

        text = await self._with_retries(_do, label=f"GET {self.source_name}")
        reader = csv.reader(io.StringIO(text))
        try:
            header = [_norm_key(h) for h in next(reader)]
        except StopIteration:
            return []
        rows = []
        for raw in reader:
            if not raw:
                continue
            rows.append(dict(zip(header, raw)))
        return rows

    async def fetch(self, *, geography: str, postal: Optional[str]) -> Optional[dict]:
        url = self._url(geography, postal)
        try:
            rows = await self._get_csv_rows(url)
        except Exception as e:  # noqa: BLE001 — graceful degradation
            self._log.warning("Eviction Lab fetch failed (%s): %s", url, e)
            return None
        return {"rows": rows, "geography": geography, "postal": postal}

    def normalize(self, raw: dict) -> dict:
        rows = raw.get("rows", []) or []
        geography = raw.get("geography")
        # Collapse to the most recent year per GEOID, preferring the latest year
        # that actually carries an eviction rate (older years are often imputed
        # null), so the overlay value is real wherever the data exists.
        best: dict[str, dict] = {}
        for r in rows:
            geoid = (r.get("geoid") or "").strip()
            if not geoid:
                continue
            year = _f(r.get("year"))
            year_i = int(year) if year is not None else 0
            has_rate = _f(r.get("eviction_rate")) is not None
            cur = best.get(geoid)
            if cur is None:
                best[geoid] = {"_year": year_i, "_has_rate": has_rate, "row": r}
                continue
            # Prefer a year with a real rate; among equals, the most recent year.
            better = (has_rate and not cur["_has_rate"]) or (
                has_rate == cur["_has_rate"] and year_i > cur["_year"]
            )
            if better:
                best[geoid] = {"_year": year_i, "_has_rate": has_rate, "row": r}

        by_geoid: dict[str, dict] = {}
        for geoid, holder in best.items():
            r = holder["row"]
            rec = {
                "geoid": geoid,
                "name": (r.get("name") or "").strip(),
                "parent_location": (r.get("parent_location") or "").strip(),
                "year": holder["_year"] or None,
            }
            for fld in _FLOAT_FIELDS:
                rec[fld] = _f(r.get(fld))
            by_geoid[geoid] = rec

        return {
            "geography": geography,
            "count": len(by_geoid),
            "by_geoid": by_geoid,
            "source": "eviction_lab_legacy",
        }

    async def by_fips(self, fips: str) -> Optional[dict]:
        """Eviction-rate overlay for a state/county/tract/block-group FIPS.

        Returns a matched-envelope dict (or None if the FIPS maps to no known
        state file / the upstream is unavailable). NEVER a parcel lead.
        """
        digits = re.sub(r"\D", "", fips or "")
        geography = _GEOG_BY_LEN.get(len(digits))
        if geography is None:
            return None

        if geography == "states":
            postal = _FIPS_TO_POSTAL.get(digits)
            if postal is None:
                return None
            cache_key = "eviction:states"
        else:
            postal = _FIPS_TO_POSTAL.get(digits[:2])
            if postal is None:
                return None
            cache_key = f"eviction:{geography}:{postal}"

        slice_ = await self.get(cache_key, geography=geography, postal=postal)
        if slice_ is None:
            return None

        rec = slice_.get("by_geoid", {}).get(digits)
        return {
            "matched": rec is not None,
            "fips": digits,
            "geography": geography,
            "state": postal,
            "record": rec,
            "overlay": True,
            "is_parcel_lead": False,
            "note": "Neighborhood eviction-rate distress overlay (area aggregate), "
                    "not a parcel-level lead.",
            "license": "ODC-BY 1.0",
            "attribution": "Eviction Lab at Princeton University, evictionlab.org",
            "source": "eviction_lab_legacy",
        }
