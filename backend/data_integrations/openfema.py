"""
data_integrations/openfema.py — FEMA disaster declarations (KEYLESS).

OpenFEMA is US-government public-domain: no key, no signup, commercial-OK.
DisasterDeclarationsSummaries gives every federal disaster/emergency
declaration (storms, floods, fires, etc.) down to the county (designatedArea +
fipsStateCode/fipsCountyCode) — a distress/risk overlay that complements the
FEMA NFHL flood layer (fema_flood.py) with *event* history rather than zones.

API: https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries
OData-style query params: $filter, $select, $orderby, $top, $inlinecount.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache

_BASE = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"

# Declarations are appended over time; a week-long TTL keeps fresh events while
# sparing the API on repeated reads of the same state/county.
_TTL = 7 * 86_400

# Cap rows per call so a high-volume state never returns an unbounded payload.
_DEFAULT_TOP = 200
_MAX_TOP = 1000


def _esc(value: str) -> str:
    """OData single-quote escaping (' -> '')."""
    return str(value).replace("'", "''")


class OpenFEMASource(DataSource):
    source_name = "openfema"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.2, jitter=0.1),
            retry_config=RetryConfig(max_attempts=4, base_backoff=3.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return _TTL

    async def fetch(
        self,
        *,
        state: str,
        county_fips: Optional[str] = None,
        incident_type: Optional[str] = None,
        top: int = _DEFAULT_TOP,
    ) -> Optional[dict]:
        filters = [f"state eq '{_esc(state.strip().upper())}'"]
        if county_fips:
            filters.append(f"fipsCountyCode eq '{_esc(county_fips)}'")
        if incident_type:
            filters.append(f"incidentType eq '{_esc(incident_type)}'")
        params = {
            "$filter": " and ".join(filters),
            "$orderby": "declarationDate desc",
            "$top": max(1, min(int(top), _MAX_TOP)),
            "$inlinecount": "allpages",
        }
        url = f"{_BASE}?{urllib.parse.urlencode(params)}"
        try:
            data = await self._get_json(url, timeout=20)
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001 — graceful degradation
            self._log.warning("OpenFEMA fetch failed (state=%s): %s", state, e)
            return None

    def normalize(self, raw: dict) -> dict:
        rows = raw.get("DisasterDeclarationsSummaries", []) or []
        meta = raw.get("metadata", {}) or {}
        declarations = [
            {
                "disaster_number": r.get("disasterNumber"),
                "declaration_string": r.get("femaDeclarationString"),
                "declaration_type": r.get("declarationType"),  # DR / EM / FM
                "declaration_date": r.get("declarationDate"),
                "incident_type": r.get("incidentType"),
                "incident_begin": r.get("incidentBeginDate"),
                "incident_end": r.get("incidentEndDate"),
                "title": r.get("declarationTitle"),
                "state": r.get("state"),
                "designated_area": r.get("designatedArea"),
                "fips_state": r.get("fipsStateCode"),
                "fips_county": r.get("fipsCountyCode"),
                "programs": {
                    "individual_assistance": bool(r.get("iaProgramDeclared")),
                    "individual_households": bool(r.get("ihProgramDeclared")),
                    "public_assistance": bool(r.get("paProgramDeclared")),
                    "hazard_mitigation": bool(r.get("hmProgramDeclared")),
                },
            }
            for r in rows
        ]
        return {
            "count": meta.get("count", len(declarations)),
            "returned": len(declarations),
            "declarations": declarations,
            "source": "openfema_disaster_declarations",
        }

    async def by_state(self, state: str, *, top: int = _DEFAULT_TOP) -> Optional[dict]:
        st = (state or "").strip().upper()
        if not st:
            return None
        return await self.get(f"openfema:state={st}:top={top}", state=st, top=top)

    async def by_county(
        self, state: str, county_fips: str, *, top: int = _DEFAULT_TOP
    ) -> Optional[dict]:
        st = (state or "").strip().upper()
        cty = (county_fips or "").strip()
        return await self.get(
            f"openfema:state={st}:county={cty}:top={top}",
            state=st, county_fips=cty, top=top,
        )
