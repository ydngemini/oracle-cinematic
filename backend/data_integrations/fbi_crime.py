"""
data_integrations/fbi_crime.py — FBI Crime Data Explorer (CDE) (DEMO_KEY-keyless).

The FBI's Crime Data Explorer API is fronted by api.data.gov, so it works with
the literal shared `DEMO_KEY` when no `DATA_GOV_API_KEY` is set — i.e. it
functions out of the box, just at api.data.gov's DEMO_KEY rate limits
(~30 req/hour, 50 req/day per IP). Setting DATA_GOV_API_KEY (free, instant)
lifts that to ~1,000 req/hour. We surface which mode is active and the limit so
callers are never surprised by a sudden 429.

Two read shapes (both real-data verified):

  agency/byStateAbbr/{ST}                 -> agencies grouped by county (ORI,
                                             name, type, NIBRS flag, lat/lng)
  summarized/agency/{ORI}/{offense}       -> monthly offense + clearance rate
  summarized/state/{ST}/{offense}         -> state-level monthly rate series

Base: https://api.usa.gov/crime/fbi/cde   (key passed as ?API_KEY=...)
Offense slugs: violent-crime, property-crime, homicide, rape, robbery,
aggravated-assault, burglary, larceny, motor-vehicle-theft, arson.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache

_BASE = "https://api.usa.gov/crime/fbi/cde"

# CDE is updated annually; a week-long TTL is plenty and spares the DEMO_KEY quota.
_TTL = 7 * 86_400


class FBICrimeSource(DataSource):
    source_name = "fbi_crime"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.5, jitter=0.2),
            retry_config=RetryConfig(max_attempts=3, base_backoff=2.0),
            cache=cache,
        )
        self._api_key = (os.environ.get("DATA_GOV_API_KEY") or "DEMO_KEY").strip()
        self._demo = self._api_key == "DEMO_KEY"

    def _cache_ttl(self) -> int:
        return _TTL

    def _key_note(self) -> dict:
        return {
            "api_key_mode": "demo" if self._demo else "keyed",
            "rate_limit_note": (
                "api.data.gov DEMO_KEY: ~30 req/hour, 50 req/day — set "
                "DATA_GOV_API_KEY for ~1,000 req/hour"
            ) if self._demo else "api.data.gov keyed: ~1,000 req/hour",
        }

    async def fetch(self, *, path: str, query: dict, mode: str, ctx: dict) -> Optional[dict]:
        params = {**(query or {}), "API_KEY": self._api_key}
        url = f"{_BASE}/{path}?{urllib.parse.urlencode(params)}"
        try:
            data = await self._get_json(url, timeout=30)
            return {"mode": mode, "ctx": ctx, "data": data, "error": None}
        except Exception as e:  # noqa: BLE001 — surface upstream issues, never crash
            self._log.warning("FBI CDE fetch failed (%s): %s", path, e)
            return {"mode": mode, "ctx": ctx, "data": None, "error": str(e)}

    def normalize(self, raw: dict) -> dict:
        mode = raw.get("mode")
        ctx = raw.get("ctx") or {}
        data = raw.get("data")
        base = {**ctx, **self._key_note(), "source": f"fbi_cde_{mode}"}

        if data is None:
            return {**base, "error": raw.get("error") or "upstream unavailable"}

        if mode == "agencies":
            return {**base, **self._normalize_agencies(data)}
        if mode == "summary":
            return {**base, **self._normalize_summary(data)}
        return {**base, "data": data}

    @staticmethod
    def _normalize_agencies(data: dict) -> dict:
        counties: dict[str, dict] = {}
        total = 0
        nibrs = 0
        for county, agencies in (data or {}).items():
            lst = []
            for a in agencies or []:
                is_nibrs = bool(a.get("is_nibrs"))
                nibrs += int(is_nibrs)
                lst.append({
                    "ori": a.get("ori"),
                    "name": a.get("agency_name"),
                    "type": a.get("agency_type_name"),
                    "is_nibrs": is_nibrs,
                    "latitude": a.get("latitude"),
                    "longitude": a.get("longitude"),
                })
            total += len(lst)
            counties[county] = {"agency_count": len(lst), "agencies": lst}
        return {
            "county_count": len(counties),
            "agency_count": total,
            "nibrs_agency_count": nibrs,
            "counties": counties,
        }

    @staticmethod
    def _normalize_summary(data: dict) -> dict:
        rates = ((data or {}).get("offenses") or {}).get("rates") or {}
        off_key = next((k for k in rates if k.lower().endswith("offenses")), None)
        clr_key = next((k for k in rates if k.lower().endswith("clearances")), None)
        offense_series = rates.get(off_key, {}) if off_key else {}
        clearance_series = rates.get(clr_key, {}) if clr_key else {}

        def _months(series: dict) -> list[str]:
            # keys are "MM-YYYY"; sort chronologically by (year, month)
            def _k(m: str):
                p = m.split("-")
                return (p[-1], p[0]) if len(p) == 2 else (m, "")
            return sorted(series.keys(), key=_k)

        months = _months(offense_series)
        latest_month = months[-1] if months else None
        return {
            "offense_label": off_key,
            "latest_month": latest_month,
            "latest_offense_rate": offense_series.get(latest_month) if latest_month else None,
            "offense_rate_series": offense_series,
            "clearance_rate_series": clearance_series,
            "populations": (data or {}).get("populations"),
            "value_note": "rates are offenses per 100,000 population (CDE summarized)",
        }

    async def agencies_by_state(self, state: str) -> Optional[dict]:
        st = (state or "").strip().upper()
        if len(st) != 2:
            return None
        return await self.get(
            f"fbi:agencies:{st}",
            path=f"agency/byStateAbbr/{st}", query={}, mode="agencies",
            ctx={"state": st},
        )

    async def summary_by_agency(
        self, ori: str, offense: str, frm: str, to: str
    ) -> Optional[dict]:
        ori = (ori or "").strip().upper()
        off = (offense or "violent-crime").strip().lower()
        return await self.get(
            f"fbi:agency:{ori}:{off}:{frm}:{to}",
            path=f"summarized/agency/{ori}/{off}",
            query={"from": frm, "to": to}, mode="summary",
            ctx={"ori": ori, "offense": off, "from": frm, "to": to},
        )

    async def summary_by_state(
        self, state: str, offense: str, frm: str, to: str
    ) -> Optional[dict]:
        st = (state or "").strip().upper()
        off = (offense or "violent-crime").strip().lower()
        return await self.get(
            f"fbi:state:{st}:{off}:{frm}:{to}",
            path=f"summarized/state/{st}/{off}",
            query={"from": frm, "to": to}, mode="summary",
            ctx={"state": st, "offense": off, "from": frm, "to": to},
        )
