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
  GET  /api/data/regrid/parcel          — Regrid nationwide parcel facts by address

Round 1 (geocode/fema/epa) is fully keyless. Round 2 adds Eviction Lab (keyless,
ODC-BY), BLS LAUS (keyless v1), FBI CDE (DEMO_KEY-keyless) and CourtListener
(free-key; dormant-safe without COURTLISTENER_TOKEN).

All endpoints are authenticated (Depends(require_context)) but return only
public, non-tenant data. The lead wires include_router(router) in server.py.

Sources + the L2 cache are built lazily on first use. External requests fail
closed with 503 if the durable cache is unavailable; no connector has an
uncached quota/privacy bypass.
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
_MARKET_METRIC_RE = re.compile(r"^[a-z0-9_]{1,80}$")

# Lazily-built singletons (one per process).
_cache = None
_geocoder = None
_fema = None
_epa = None
_eviction = None
_fbi = None
_courtlistener = None
_regrid = None


async def _cache_layer():
    """Build the mandatory IntegrationCache once for this process."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        from data_integrations.cache import get_integration_cache

        _cache = await get_integration_cache()
    except Exception as e:  # noqa: BLE001 - normalize dependency failures
        log.error("mandatory data-sources cache unavailable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External data cache is unavailable; upstream request was not attempted.",
        ) from e
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


async def _get_regrid():
    global _regrid
    if _regrid is None:
        from data_integrations.regrid import RegridParcelSource

        _regrid = RegridParcelSource(cache=await _cache_layer())
    return _regrid


class BatchGeocodeRequest(BaseModel):
    addresses: list[str] = Field(..., min_length=1, max_length=10_000)


async def _regrid_health() -> dict:
    """Regrid health including credential expiry.

    This previously reported `configured: bool(token)`, so a token that had
    lapsed a week earlier still read as configured — the one health signal that
    existed actively asserted the wrong thing.
    """
    source = await _get_regrid()
    payload: dict = {
        **source.metrics(),
        "configured": source.configured,
        "nationwide": True,
        "model_training_prohibited": True,
        "credential_expired": source.token_expired,
    }
    expires = source.token_expires_at
    if expires is not None:
        payload["credential_expires_at"] = expires.isoformat()
        payload["credential_days_remaining"] = source.days_until_expiry
    note = source.token_expiry_note()
    if note:
        payload["credential_note"] = note
    return payload


@router.get("/health")
async def health(ctx: TenantContext = Depends(require_context)) -> dict:
    cache = await _cache_layer()
    return {
        "ok": True,
        "cache_enabled": True,
        "cache_required": True,
        "sources": {
            "census_geocoder": (await _get_geocoder()).metrics(),
            "openfema": (await _get_fema()).metrics(),
            "epa_envirofacts": (await _get_epa()).metrics(),
            "eviction_lab": (await _get_eviction()).metrics(),
            "fbi_crime": (await _get_fbi()).metrics(),
            "courtlistener": (await _get_courtlistener()).metrics(),
            "regrid_parcel": await _regrid_health(),
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


_REGRID_PATH_RE = re.compile(
    r"^/us/[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*){0,4}/?$", re.IGNORECASE
)


@router.get("/regrid/parcel")
async def regrid_parcel(
    address: str = Query(..., min_length=3, max_length=400),
    path: Optional[str] = Query(
        None,
        min_length=4,
        max_length=300,
        description="Optional Regrid /us/state/county/city path to disambiguate an address",
    ),
    limit: int = Query(1, ge=1, le=5, description="Maximum matched parcels (1–5)"),
    include_geometry: bool = Query(
        False,
        description="Include parcel GeoJSON only when a map needs it",
    ),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Return allow-listed nationwide public parcel facts for an address.

    The Regrid credential stays server-side.  Enhanced ownership, matched
    secondary addresses, building footprints, custom fields, and mailing
    addresses are not returned through this surface.
    """
    path_normalized = (path or "").strip() or None
    if path_normalized and not _REGRID_PATH_RE.fullmatch(path_normalized):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "path must be a Regrid US path such as /us/de/new-castle/wilmington",
        )

    source = await _get_regrid()
    if not source.configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Regrid parcel lookup is not configured.",
        )

    try:
        return await source.lookup_address(
            address,
            path=path_normalized,
            limit=limit,
            include_geometry=include_geometry,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider details may include a URL
        from data_integrations.cache import IntegrationCacheUnavailable
        from data_integrations.regrid import (
            RegridAuthError,
            RegridConfigurationError,
            RegridCoverageError,
            RegridUpstreamError,
        )

        if isinstance(exc, IntegrationCacheUnavailable):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "External data cache is unavailable; upstream request was not attempted.",
            ) from exc
        if isinstance(exc, RegridConfigurationError):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Regrid parcel lookup is not configured.",
            ) from exc
        if isinstance(exc, RegridCoverageError):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "The configured Regrid account does not include this location.",
            ) from exc
        if isinstance(exc, RegridAuthError):
            # 503, not 502 "temporarily unavailable": an expired or revoked token
            # is permanent until it is rotated, and calling that temporary invites
            # a retry that can never succeed while hiding a one-line fix.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                str(exc),
            ) from exc
        if isinstance(exc, RegridUpstreamError):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Regrid parcel lookup is temporarily unavailable.",
            ) from exc
        log.exception("unexpected Regrid parcel lookup failure")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Regrid parcel lookup is temporarily unavailable.",
        ) from exc


@router.get("/market-research")
async def market_research(
    state: Optional[str] = Query(None, min_length=2, max_length=2),
    metric: Optional[str] = Query(None, min_length=1, max_length=80),
    limit: int = Query(250, ge=1, le=1000),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Latest source-attributed aggregate housing metrics.

    These rows describe state markets and are never individual property
    listings. Zillow/Redfin attribution remains attached to each observation.
    """
    normalized_state = state.strip().upper() if state else None
    if normalized_state and not _STATE_RE.fullmatch(normalized_state):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "state must be two letters")
    normalized_metric = metric.strip().lower() if metric else None
    if normalized_metric and not _MARKET_METRIC_RE.fullmatch(normalized_metric):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid metric")

    conditions = []
    args: list[object] = []
    if normalized_state:
        args.append(normalized_state)
        conditions.append(f"state_code = ${len(args)}")
    if normalized_metric:
        args.append(normalized_metric)
        conditions.append(f"metric_key = ${len(args)}")
    where = " AND ".join(conditions) if conditions else "TRUE"
    args.append(limit)

    from db.connection import tenant_tx

    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (source_key,metric_key,state_code)
                           source_key,metric_key,state_code,geography_name,
                           period_end,value,unit,source_url,dataset_sha256,
                           dataset_updated_at,retrieved_at,metadata
                      FROM public_market_metrics
                     WHERE {where}
                     ORDER BY source_key,metric_key,state_code,period_end DESC
                ) latest
                ORDER BY state_code,metric_key,source_key
                LIMIT ${len(args)}
                """,
                *args,
            )
    except Exception as exc:  # noqa: BLE001
        log.error("market research query failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Market research catalog is unavailable.",
        ) from exc
    return {
        "metrics": [
            {
                **dict(row),
                "period_end": row["period_end"].isoformat(),
                "dataset_updated_at": (
                    row["dataset_updated_at"].isoformat()
                    if row["dataset_updated_at"] else None
                ),
                "retrieved_at": row["retrieved_at"].isoformat(),
                "value": float(row["value"]),
            }
            for row in rows
        ],
        "count": len(rows),
        "state": normalized_state,
        "metric": normalized_metric,
        "data_classification": "aggregate_market_research",
        "individual_listings": False,
    }
