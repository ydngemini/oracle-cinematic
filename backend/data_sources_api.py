"""
data_sources_api.py — thin read API over the KEYLESS data integrations.

Exposes the commercial-safe, no-key sources wired in the keyless-data-sources
program:

  GET  /api/data/health                 — source metrics + cache availability
  GET  /api/data/geocode                — Census geocode (address -> lat/lng+FIPS)
  POST /api/data/geocode/batch          — Census batch geocode (<=10k addresses)
  GET  /api/data/fema/disasters         — OpenFEMA declarations by state[/county]
  GET  /api/data/epa/sites              — EPA Envirofacts FRS facilities by ZIP
  GET  /api/data/eviction               — Eviction Lab eviction-rate overlay by FIPS
  GET  /api/data/bls/unemployment       — BLS LAUS local unemployment (state/metro)
  GET  /api/data/fbi/crime              — FBI CDE agencies-by-county / agency series
  GET  /api/data/bankruptcy             — CourtListener federal bankruptcy dockets

Round 1 (geocode/fema/epa) is fully keyless. Round 2 adds Eviction Lab (keyless,
ODC-BY), BLS LAUS (keyless v1), FBI CDE (DEMO_KEY-keyless) and CourtListener
(free-key; dormant-safe without COURTLISTENER_TOKEN).

All endpoints are authenticated (Depends(require_context)) but return only
public, non-tenant data. The lead wires include_router(router) in server.py.

Sources + the L2 cache are built lazily on first use (cache degrades to None if
the pool/di_cache is unavailable — every source still works, just uncached).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from tenancy import TenantContext, require_context

log = logging.getLogger("oracle.data_sources_api")

router = APIRouter(prefix="/api/data", tags=["data-sources"])

_ZIP_RE = re.compile(r"^\d{5}$")
_STATE_RE = re.compile(r"^[A-Za-z]{2}$")

# Lazily-built singletons (one per process).
_cache = None
_cache_tried = False
_geocoder = None
_fema = None
_epa = None
_eviction = None
_fbi = None
_courtlistener = None


async def _cache_layer():
    """Build the IntegrationCache once; tolerate its absence (uncached mode)."""
    global _cache, _cache_tried
    if _cache_tried:
        return _cache
    _cache_tried = True
    try:
        from db.connection import get_pool
        from data_integrations.cache import IntegrationCache
        pool = get_pool()
        if pool is not None:
            _cache = await IntegrationCache.create(pool)
    except Exception as e:  # noqa: BLE001 — cache is optional, never block a read
        log.warning("data-sources cache unavailable (uncached mode): %s", e)
        _cache = None
    return _cache


async def _get_geocoder():
    global _geocoder
    if _geocoder is None:
        from data_integrations.census_geocoder import CensusGeocoder
        _geocoder = CensusGeocoder(cache=await _cache_layer())
    return _geocoder


async def _get_fema():
    global _fema
    if _fema is None:
        from data_integrations.openfema import OpenFEMASource
        _fema = OpenFEMASource(cache=await _cache_layer())
    return _fema


async def _get_epa():
    global _epa
    if _epa is None:
        from data_integrations.epa_envirofacts import EPAEnvirofactsSource
        _epa = EPAEnvirofactsSource(cache=await _cache_layer())
    return _epa


async def _get_eviction():
    global _eviction
    if _eviction is None:
        from data_integrations.eviction_lab import EvictionLabSource
        _eviction = EvictionLabSource(cache=await _cache_layer())
    return _eviction


async def _get_fbi():
    global _fbi
    if _fbi is None:
        from data_integrations.fbi_crime import FBICrimeSource
        _fbi = FBICrimeSource(cache=await _cache_layer())
    return _fbi


async def _get_courtlistener():
    global _courtlistener
    if _courtlistener is None:
        from data_integrations.courtlistener import CourtListenerSource
        _courtlistener = CourtListenerSource(cache=await _cache_layer())
    return _courtlistener


class BatchGeocodeRequest(BaseModel):
    addresses: list[str] = Field(..., min_length=1, max_length=10_000)


@router.get("/health")
async def health(ctx: TenantContext = Depends(require_context)) -> dict:
    cache = await _cache_layer()
    return {
        "ok": True,
        "cache_enabled": cache is not None,
        "sources": {
            "census_geocoder": (await _get_geocoder()).metrics(),
            "openfema": (await _get_fema()).metrics(),
            "epa_envirofacts": (await _get_epa()).metrics(),
            "eviction_lab": (await _get_eviction()).metrics(),
            "fbi_crime": (await _get_fbi()).metrics(),
            "courtlistener": (await _get_courtlistener()).metrics(),
            "bls_laus": {"source": "bls_laus", "keyless_v1": True},
        },
    }


@router.get("/geocode")
async def geocode(
    address: str = Query(..., min_length=3, max_length=400),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Single-address geocode → lat/lng + state/county/tract/block (Census)."""
    result = await (await _get_geocoder()).geocode(address)
    if result is None:
        # No match (or upstream hiccup) — return a clean unmatched envelope.
        return {"matched": False, "input_address": address, "source": "census_geocoder"}
    return result


@router.post("/geocode/batch")
async def geocode_batch(
    body: BatchGeocodeRequest,
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Batch geocode up to 10,000 addresses in one upstream POST."""
    try:
        results = await (await _get_geocoder()).geocode_batch(body.addresses)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    matched = sum(1 for r in results if r.get("matched"))
    return {"requested": len(body.addresses), "matched": matched, "results": results}


@router.get("/fema/disasters")
async def fema_disasters(
    state: str = Query(..., description="2-letter state code, e.g. DE"),
    county_fips: Optional[str] = Query(None, description="3-digit county FIPS, e.g. 005"),
    top: int = Query(200, ge=1, le=1000),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """FEMA disaster declarations by state (optionally narrowed to a county)."""
    if not _STATE_RE.match(state or ""):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "state must be a 2-letter code")
    fema = await _get_fema()
    if county_fips:
        data = await fema.by_county(state, county_fips, top=top)
    else:
        data = await fema.by_state(state, top=top)
    if data is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OpenFEMA upstream unavailable")
    return data


@router.get("/epa/sites")
async def epa_sites(
    zip: str = Query(..., description="5-digit ZIP code"),
    rows: int = Query(250, ge=1, le=1000),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """EPA Envirofacts FRS facilities near a ZIP, tagged Superfund/brownfield/etc."""
    if not _ZIP_RE.match(zip or ""):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "zip must be 5 digits")
    data = await (await _get_epa()).by_zip(zip, rows=rows)
    if data is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "EPA Envirofacts upstream unavailable")
    return data


@router.get("/eviction")
async def eviction(
    fips: str = Query(..., description="state(2) / county(5) / tract(11) / block-group(12) FIPS"),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Eviction Lab eviction-rate distress OVERLAY for a FIPS area.

    Neighborhood aggregate (ODC-BY, Eviction Lab @ Princeton) — NOT a parcel lead.
    """
    digits = re.sub(r"\D", "", fips or "")
    if len(digits) not in (2, 5, 11, 12):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "fips must be 2 (state), 5 (county), 11 (tract) or 12 (block-group) digits",
        )
    data = await (await _get_eviction()).by_fips(digits)
    if data is None:
        return {
            "matched": False,
            "fips": digits,
            "overlay": True,
            "is_parcel_lead": False,
            "note": "no Eviction Lab slice for this FIPS (unknown state or upstream unavailable)",
            "source": "eviction_lab_legacy",
        }
    return data


@router.get("/bls/unemployment")
async def bls_unemployment(
    area: str = Query(..., description="2-letter state, metro slug, or BLS LAUS series id"),
    start_year: Optional[str] = Query(None, description="e.g. 2024"),
    end_year: Optional[str] = Query(None, description="e.g. 2025"),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Local-area unemployment rate (BLS LAUS). Keyless v1; BLS_API_KEY → v2."""
    from apis.market_data import get_local_unemployment
    return await get_local_unemployment(area, start_year=start_year, end_year=end_year)


@router.get("/fbi/crime")
async def fbi_crime(
    state: str = Query(..., description="2-letter state code, e.g. DE"),
    ori: Optional[str] = Query(None, description="agency ORI for a summarized series"),
    offense: str = Query("violent-crime", description="violent-crime, property-crime, homicide, ..."),
    frm: Optional[str] = Query(None, alias="from", description="MM-YYYY"),
    to: Optional[str] = Query(None, description="MM-YYYY"),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """FBI Crime Data Explorer. No ORI → agencies grouped by county for the state;
    with ORI → that agency's monthly offense/clearance rate series. Works keyless
    via DEMO_KEY (low limits) unless DATA_GOV_API_KEY is set."""
    if not _STATE_RE.match(state or ""):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "state must be a 2-letter code")
    fbi = await _get_fbi()
    if ori:
        last_full_year = date.today().year - 1
        _from = frm or f"01-{last_full_year - 1}"
        _to = to or f"12-{last_full_year}"
        data = await fbi.summary_by_agency(ori, offense, _from, _to)
    else:
        data = await fbi.agencies_by_state(state)
    if data is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "FBI CDE upstream unavailable")
    return data


@router.get("/bankruptcy")
async def bankruptcy(
    court: str = Query(..., description="federal bankruptcy court id, e.g. 'deb', 'nysb'"),
    since: Optional[str] = Query(None, description="only dockets filed on/after YYYY-MM-DD"),
    limit: int = Query(20, ge=1, le=100),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Federal BANKRUPTCY dockets (CourtListener/RECAP). Foreclosure & probate are
    state courts and are NOT covered. Dormant (returns {"skipped": ...}) until
    COURTLISTENER_TOKEN is set."""
    return await (await _get_courtlistener()).bankruptcy_dockets(
        court, date_filed_after=since, page_size=limit
    )
