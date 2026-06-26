"""
data_integrations/epa_envirofacts.py — EPA Envirofacts FRS (KEYLESS).

EPA's Envirofacts REST service is keyless and public-domain. The Facility
Registry Service (FRS_PROGRAM_FACILITY) lists regulated/contaminated sites and,
crucially, the EPA *program* each is enrolled in (pgm_sys_acrnm) — so a single
ZIP query yields Superfund (SEMS), brownfields (ACRES), hazardous-waste
(RCRAINFO), air, water and storage-tank sites near a parcel. That program tag
is what turns a raw facility list into an environmental-distress overlay for
underwriting (proximity to a Superfund site materially affects value/liability).

Endpoint family:
  https://data.epa.gov/efservice/{TABLE}/{COLUMN}/{VALUE}/ROWS/0:{N}/JSON

Note: county-FIPS columns are null in the upstream FRS data, so ZIP is the
reliable spatial filter (matches the catalog's "by ZIP" guidance).
"""

from __future__ import annotations

from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache

_EFSERVICE = "https://data.epa.gov/efservice"
_TABLE = "FRS_PROGRAM_FACILITY"

# FRS refreshes infrequently; a week-long TTL is plenty and protects the API.
_TTL = 7 * 86_400

# ROWS cap — a single dense ZIP can return hundreds of program rows (~1KB each).
_DEFAULT_ROWS = 250
_MAX_ROWS = 1000

# EPA program acronym (pgm_sys_acrnm) -> human distress category.
_PROGRAM_CATEGORY: dict[str, str] = {
    "SEMS": "superfund",
    "SEMS-LIEN": "superfund",
    "NPL": "superfund",
    "CERCLIS": "superfund",
    "ACRES": "brownfield",
    "RCRAINFO": "hazardous_waste",
    "RCRA": "hazardous_waste",
    "ICIS-AIR": "air",
    "AIRS/AFS": "air",
    "AIR": "air",
    "EIS": "air",
    "ICIS-NPDES": "water_discharge",
    "NPDES": "water_discharge",
    "ICIS": "water_discharge",
    "PCS": "water_discharge",
    "SDWIS": "drinking_water",
    "TRIS": "toxic_release",
    "TRI": "toxic_release",
    "LUST": "storage_tank",
    "UST": "storage_tank",
    "E-GGRT": "greenhouse_gas",
    "EGGRT": "greenhouse_gas",
    "GHG": "greenhouse_gas",
}

# Categories that meaningfully impair value / signal contamination liability.
_HIGH_RISK = {"superfund", "brownfield", "hazardous_waste", "storage_tank", "toxic_release"}


def _category(acronym: Optional[str]) -> str:
    return _PROGRAM_CATEGORY.get((acronym or "").strip().upper(), "other")


class EPAEnvirofactsSource(DataSource):
    source_name = "epa_envirofacts"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.3, jitter=0.1),
            retry_config=RetryConfig(max_attempts=4, base_backoff=3.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return _TTL

    async def fetch(self, *, column: str, value: str, rows: int = _DEFAULT_ROWS) -> Optional[dict]:
        n = max(1, min(int(rows), _MAX_ROWS))
        # value is a ZIP / state code — safe alphanumerics only; quote defensively.
        import urllib.parse
        safe = urllib.parse.quote(str(value).strip(), safe="")
        url = f"{_EFSERVICE}/{_TABLE}/{column}/{safe}/ROWS/0:{n}/JSON"
        try:
            data = await self._get_json(url, timeout=30)
            return {"rows": data} if isinstance(data, list) else None
        except Exception as e:  # noqa: BLE001 — graceful degradation
            self._log.warning("EPA Envirofacts fetch failed (%s=%s): %s", column, value, e)
            return None

    def normalize(self, raw: dict) -> dict:
        rows = raw.get("rows", []) or []
        facilities: list[dict] = []
        seen: set[tuple] = set()
        by_category: dict[str, int] = {}

        for r in rows:
            acronym = (r.get("pgm_sys_acrnm") or "").strip().upper()
            cat = _category(acronym)
            reg = r.get("registry_id")
            # One facility can enrol in many programs; key by (facility, program).
            dedupe = (reg, acronym)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            by_category[cat] = by_category.get(cat, 0) + 1
            facilities.append({
                "registry_id": reg,
                "name": (r.get("primary_name") or "").strip(),
                "address": (r.get("location_address") or "").strip(),
                "city": (r.get("city_name") or "").strip(),
                "state": (r.get("state_code") or "").strip(),
                "zip": (r.get("postal_code") or "").strip(),
                "county": (r.get("county_name") or "").strip(),
                "program": acronym,
                "program_category": cat,
                "site_type": (r.get("site_type_name") or "").strip(),
            })

        return {
            "count": len(facilities),
            "by_category": by_category,
            "has_superfund": by_category.get("superfund", 0) > 0,
            "has_brownfield": by_category.get("brownfield", 0) > 0,
            "high_risk_count": sum(by_category.get(c, 0) for c in _HIGH_RISK),
            "facilities": facilities,
            "source": "epa_envirofacts_frs",
        }

    async def by_zip(self, zip_code: str, *, rows: int = _DEFAULT_ROWS) -> Optional[dict]:
        z = (zip_code or "").strip()[:5]
        if not z:
            return None
        return await self.get(f"epa_frs:zip={z}:rows={rows}", column="POSTAL_CODE", value=z, rows=rows)

    async def by_state(self, state: str, *, rows: int = _DEFAULT_ROWS) -> Optional[dict]:
        st = (state or "").strip().upper()
        if not st:
            return None
        return await self.get(f"epa_frs:state={st}:rows={rows}", column="STATE_CODE", value=st, rows=rows)
