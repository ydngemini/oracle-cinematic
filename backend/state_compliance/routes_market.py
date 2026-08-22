"""State/county market aggregates, flood zone, school district, and zoning lookups."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, Role, require_context, require_role

# Authoritative attorney-at-closing list — single source of truth shared with
# the compliance engine so the public state-profile API and ComplianceEngine
# never disagree about whether a state requires an attorney at closing.
from compliance_engine.closing import ATTORNEY_CLOSE_STATES

from ._common import (
    router, logger,
    _STATE_RE, _FIPS_RE, _UUID_RE,
    ALL_STATE_CODES, _ATTORNEY_REVIEW_STATES, _MANDATORY_DISCLOSURE_STATES,
    _TDS_STATES, _FEDERAL_LEAD_PAINT_THRESHOLD_YEAR,
    _iso, _num, _require_state, _require_uuid, _fetch, _fetchrow,
    _require_dataset_loaded,
)
from .models import (  # noqa: F401  (re-exported for route handlers)
    StateSummary,
    DisclosureForm,
    ContractTemplate,
    AdvertisingRule,
    StateProfile,
    LicenseRequirements,
    ReciprocityInfo,
    AgentLicense,
    AgentLicenseStatus,
    CECreditBody,
    CECreditResponse,
    MLSRegion,
    MLSSyncStatus,
    MLSSearchBody,
    NormalizedListing,
    MLSSearchResponse,
    StateMarketOverview,
    CountyMarketData,
    FloodZoneResult,
    SchoolDistrict,
    SchoolsResponse,
    ZoningResult,
    TransactionContext,
    RequiredDisclosure,
    ComplianceCheckResponse,
    DisclosureChecklistItem,
    ComplianceChecklist,
    FormValidationBody,
    ValidationError,
    FormValidationResponse,
)
from .engine import _engine  # noqa: F401

# Residential market aggregates older than a quarter should not be priced
# against without the caller knowing.
#
# This used to flag every response, because the table held nothing but
# 2024-10-01 seeds. state_market_projection now refreshes it from the scheduled
# Redfin sync, so the flag means what it was written to mean again. Note the
# publisher lag: Redfin reports a calendar month roughly three months later, so
# a freshly projected row is still ~80 days old — inside this window, but not
# by much.
_MARKET_STALE_AFTER_DAYS = 90


@router.get(
    "/api/market/{state_code}/overview",
    response_model=StateMarketOverview,
    summary="Aggregate market statistics for a state",
)
async def get_state_market_overview(
    state_code: str,
    ctx: TenantContext = Depends(require_context),
) -> StateMarketOverview:
    """Return aggregate market data for the given state.

    ``state_market_stats`` is a plain table, not a materialised view.
    ``state_market_projection`` refreshes it from the scheduled Redfin sync
    each time that task runs; before that existed it served only migration
    0025's 51 static 2024-10-01 seeds.

    ``as_of_date`` is the period the figures DESCRIBE, not when they were
    fetched — the publisher lag is roughly three months, and
    ``source_fetched_at`` carries the retrieval time separately.
    ``verification_status`` distinguishes a machine-harvested row from one a
    person checked, and any row still reading ``migration_0025_seed`` was never
    refreshed.

    Four columns are deliberately NULL on a projected row —
    ``avg_price_per_sqft``, ``closed_sales_last_30d``, ``list_to_sale_ratio``
    and ``yoy_price_change_pct`` — because the harvested source answers a
    different question than the column name asks. See
    ``state_market_projection.UNMAPPED_COLUMNS``.
    """
    code = _require_state(state_code)
    row = await _fetchrow(
        ctx,
        "SELECT * FROM state_market_stats WHERE state_code = $1",
        code,
    )
    if not row:
        await _require_dataset_loaded(ctx, "state_market_stats")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Market data not available for {code}.",
        )
    as_of = row.get("as_of_date")
    vintage_days = (date.today() - as_of).days if isinstance(as_of, date) else None
    return StateMarketOverview(
        state_code=row["state_code"],
        state_name=row.get("state_name", code),
        median_list_price=_num(row.get("median_list_price")),
        median_sale_price=_num(row.get("median_sale_price")),
        median_days_on_market=_num(row.get("median_days_on_market")),
        months_of_supply=_num(row.get("months_of_supply")),
        yoy_price_change_pct=_num(row.get("yoy_price_change_pct")),
        active_listings=row.get("active_listings"),
        closed_sales_last_30d=row.get("closed_sales_last_30d"),
        list_to_sale_ratio=_num(row.get("list_to_sale_ratio")),
        avg_price_per_sqft=_num(row.get("avg_price_per_sqft")),
        as_of_date=as_of,
        data_vintage_days=vintage_days,
        # A quarter is generous for residential market aggregates; past that,
        # a caller pricing against these numbers should know they are old.
        is_stale=vintage_days is not None and vintage_days > _MARKET_STALE_AFTER_DAYS,
    )


@router.get(
    "/api/market/county/{fips_code}",
    response_model=CountyMarketData,
    summary="County-level market and tax data",
)
async def get_county_market_data(
    fips_code: str,
    ctx: TenantContext = Depends(require_context),
) -> CountyMarketData:
    """Return market statistics and property tax rate data for the specified
    county FIPS code (5-digit, e.g. ``13121`` for Fulton County, GA).
    """
    if not _FIPS_RE.match(fips_code):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="fips_code must be a 5-digit string.",
        )
    row = await _fetchrow(
        ctx,
        "SELECT * FROM county_market_stats WHERE fips_code = $1",
        fips_code,
    )
    if not row:
        # Distinguishes "we hold county data, just not this one" from "this
        # table has never been loaded" — the second is not an answer about
        # the county at all.
        await _require_dataset_loaded(ctx, "county_market_stats")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"County data not available for FIPS {fips_code!r}.",
        )
    return CountyMarketData(
        fips_code=row["fips_code"],
        county_name=row.get("county_name", ""),
        state_code=row.get("state_code", ""),
        median_sale_price=_num(row.get("median_sale_price")),
        median_list_price=_num(row.get("median_list_price")),
        median_days_on_market=_num(row.get("median_days_on_market")),
        property_tax_rate_pct=_num(row.get("property_tax_rate_pct")),
        effective_tax_rate_pct=_num(row.get("effective_tax_rate_pct")),
        median_annual_tax=_num(row.get("median_annual_tax")),
        population=row.get("population"),
        households=row.get("households"),
        homeownership_rate_pct=_num(row.get("homeownership_rate_pct")),
        as_of_date=row.get("as_of_date"),
    )


# Federally-backed lenders require flood insurance inside a Special Flood
# Hazard Area. Prefix-matched because the NFHL carries suffixed variants
# ("AE1", "A99"); the live service reports SFHA membership directly and does
# not need this.
_SFHA_ZONE_PREFIXES = ("A", "AE", "AH", "AO", "AR", "A99", "V", "VE")

_ZONE_DESCRIPTIONS: dict[str, str] = {
    "AE": "Special Flood Hazard Area — 1% annual chance flood (detailed study).",
    "AO": "Special Flood Hazard Area — 1% annual chance shallow flooding.",
    "AH": "Special Flood Hazard Area — 1% annual chance shallow flooding (ponds).",
    "AR": "Special Flood Hazard Area — temporarily increased flood risk.",
    "A99": "Special Flood Hazard Area — protected by federal flood control project.",
    "VE": "Coastal Special Flood Hazard Area with wave action (detailed study).",
    "V": "Coastal Special Flood Hazard Area with wave action.",
    "X": "Area of minimal flood hazard (outside 0.2% annual chance floodplain).",
    "0.2PCT": "Moderate flood hazard — 0.2% annual chance (500-year) floodplain.",
}


def _zone_description(zone: str) -> str:
    return _ZONE_DESCRIPTIONS.get(zone, f"Flood zone {zone} — consult FEMA FIRM panel.")


def _state_from_fips(state_fips: Any) -> str:
    """2-digit state FIPS to postal code, or "" when it does not resolve."""
    # Reuses the table in data_integrations.eviction_lab rather than carrying a
    # second copy of the 51-entry mapping. A duplicated static table is how the
    # flood-zone clients drifted apart, and only one of them got fixed.
    from data_integrations.eviction_lab import _FIPS_TO_POSTAL

    return _FIPS_TO_POSTAL.get(str(state_fips or "").zfill(2), "")


_nces_source = None


async def _live_school_district(lat: float, lng: float) -> Optional[dict]:
    """Containing school district from NCES EDGE, or None if it cannot answer.

    Never raises: this is a fallback, and an upstream outage must degrade to
    "we could not determine it" rather than turning a 200 into a 503.
    """
    global _nces_source
    try:
        if _nces_source is None:
            from data_integrations.cache import get_integration_cache
            from data_integrations.school_districts import NCESDistrictSource

            _nces_source = NCESDistrictSource(cache=await get_integration_cache())
        return await _nces_source.lookup(lat, lng)
    except Exception as exc:  # noqa: BLE001 — any upstream/cache failure is "unknown"
        logger.warning("NCES district lookup unavailable for (%s, %s): %s", lat, lng, exc)
        return None


@router.get(
    "/api/market/flood-zone",
    response_model=FloodZoneResult,
    summary="FEMA flood zone lookup by lat/lng",
)
async def get_flood_zone(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lng: float = Query(..., ge=-180.0, le=180.0),
    ctx: TenantContext = Depends(require_context),
) -> FloodZoneResult:
    """Return the FEMA flood zone designation for the given coordinate.

    Two sources, in order: the local ``fema_flood_zones`` spatial table
    (PostGIS), then FEMA's live NFHL map service. Nothing in this repository
    writes the local table today, so in practice the live service answers —
    but a deployment that has loaded NFHL extracts is served locally without a
    round trip, and keeps working when FEMA's service is down.

    Flood insurance is required by federally-backed lenders for parcels in
    Special Flood Hazard Areas (SFHA): zones A, AE, AH, AO, AR, A99, V, VE.
    """
    # The spatial lookup requires PostGIS (geom column + ST_Contains). On
    # deployments where PostGIS is not installed the `fz.geom` column does not
    # exist, so the spatial query is unavailable. Rather than mask that as a
    # 503, degrade to the honest "zone unknown / NFHL data not available"
    # result below — identical to a coordinate that simply isn't in the NFHL.
    try:
        row = await _fetchrow(
            ctx,
            """
            SELECT fz.*, c.community_name, c.community_number
            FROM fema_flood_zones fz
            LEFT JOIN fema_communities c ON c.community_number = fz.community_number
            WHERE ST_Contains(
                fz.geom,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)
            )
            ORDER BY fz.firm_date DESC
            LIMIT 1
            """,
            lng,  # PostGIS: X=longitude, Y=latitude
            lat,
        )
    except HTTPException as exc:
        # _fetch maps DB errors (incl. missing PostGIS geom column / functions)
        # to a 503. For this endpoint a missing spatial capability is not an
        # outage — fall through to the graceful unknown-zone response.
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        logger.warning(
            "Flood-zone spatial lookup unavailable (PostGIS/geom missing) — "
            "returning unknown zone for (%s, %s).", lat, lng,
        )
        row = None

    if not row:
        # Nothing locally. Ask FEMA's live NFHL service before giving up: an
        # answer from the authoritative source beats a guess, and the local
        # table is empty on every deployment that has not loaded NFHL extracts.
        from apis.property_data import get_flood_zone as _live_flood_zone

        live = await _live_flood_zone(lat, lng)
        # `mapped=False` means FEMA answered but holds no NFHL polygon here —
        # no more informative than the service being down, and treated the same.
        if live is None or not live.get("mapped"):
            # Both sources silent. Say so. Returning zone X here would assert
            # "surveyed, outside the Special Flood Hazard Area" on the strength
            # of having no data at all — and a buyer, lender or disclosure form
            # downstream cannot tell that apart from a real survey result.
            logger.warning(
                "Flood-zone unresolved by local NFHL table and live FEMA service "
                "for (%s, %s) — reporting UNKNOWN.", lat, lng,
            )
            return FloodZoneResult(
                latitude=lat,
                longitude=lng,
                fema_zone="UNKNOWN",
                zone_description=(
                    "Flood zone could not be determined. No NFHL coverage was "
                    "available for this location from either the local dataset "
                    "or FEMA's map service — this is not a finding of low risk. "
                    "Consult the FEMA FIRM panel before relying on it."
                ),
                flood_insurance_required=None,
                data_available=False,
            )

        zone = str(live.get("zone") or "X")
        return FloodZoneResult(
            latitude=lat,
            longitude=lng,
            fema_zone=zone,
            zone_description=_zone_description(zone),
            # SFHA_TF from the live service is authoritative; it already
            # accounts for subtypes the prefix test below would misread.
            flood_insurance_required=bool(live.get("in_sfha")),
        )

    zone = row.get("flood_zone", "X")
    insurance_required = any(zone.startswith(z) for z in _SFHA_ZONE_PREFIXES)

    return FloodZoneResult(
        latitude=lat,
        longitude=lng,
        fema_zone=zone,
        zone_description=_zone_description(zone),
        flood_insurance_required=insurance_required,
        firm_panel=row.get("firm_panel"),
        firm_date=row.get("firm_date"),
        community_name=row.get("community_name"),
        community_number=row.get("community_number"),
    )


@router.get(
    "/api/market/schools",
    response_model=SchoolsResponse,
    summary="Nearby school districts within a radius",
)
async def get_nearby_schools(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lng: float = Query(..., ge=-180.0, le=180.0),
    radius: float = Query(default=5.0, ge=0.1, le=50.0, description="Radius in miles"),
    ctx: TenantContext = Depends(require_context),
) -> SchoolsResponse:
    """Return school districts near the given coordinate, nearest-first.

    Two sources, in order: the local ``school_districts`` table (a true radius
    search, and the only one that carries ratings and enrolment), then NCES
    EDGE live. Nothing in this repository writes the local table, so in
    practice the live source answers.

    The live source is a point-in-polygon lookup: it returns the **containing**
    district only and knows nothing about ratings, enrolment or distance. When
    it answers, ``radius_applied`` is false and those fields are null — a single
    district here means "the one you are standing in", not "the only one within
    the radius you asked for".
    """
    meters = radius * 1609.34
    try:
        rows = await _fetch(
            ctx,
            """
            SELECT sd.*,
                   earth_distance(
                       ll_to_earth(sd.centroid_lat, sd.centroid_lng),
                       ll_to_earth($1, $2)
                   ) / 1609.34 AS distance_miles
            FROM school_districts sd
            WHERE earth_distance(
                ll_to_earth(sd.centroid_lat, sd.centroid_lng),
                ll_to_earth($1, $2)
            ) <= $3
            ORDER BY distance_miles ASC
            LIMIT 20
            """,
            lat,
            lng,
            meters,
        )
    except HTTPException as exc:
        # `earthdistance` is created best-effort in 0013 (inside an EXCEPTION
        # block), so ll_to_earth/earth_distance may not exist. _fetch maps that
        # to a blanket 503, but a missing extension is not an outage when a live
        # source can answer — mirror the flood-zone route above.
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        logger.warning(
            "School-district radius search unavailable (earthdistance/cube missing) "
            "— falling back to the live NCES lookup for (%s, %s).", lat, lng,
        )
        rows = []

    if rows:
        return SchoolsResponse(
            latitude=lat, longitude=lng, radius_miles=radius,
            districts=[
                SchoolDistrict(
                    district_id=str(r.get("id", uuid.uuid4())),
                    district_name=r["district_name"],
                    district_type=r.get("district_type", "unified"),
                    state_code=r.get("state_code", ""),
                    county=r.get("county", ""),
                    nces_id=r.get("nces_id"),
                    rating=_num(r.get("rating")),
                    enrollment=r.get("enrollment"),
                    student_teacher_ratio=_num(r.get("student_teacher_ratio")),
                    distance_miles=_num(r.get("distance_miles")),
                    website=r.get("website"),
                )
                for r in rows
            ],
        )

    live = await _live_school_district(lat, lng)
    if not live or not live.get("district_name"):
        logger.warning(
            "School districts unresolved by the local table and NCES EDGE for (%s, %s).",
            lat, lng,
        )
        return SchoolsResponse(
            latitude=lat, longitude=lng, radius_miles=radius, districts=[],
            radius_applied=False, source="none", data_available=False,
        )

    return SchoolsResponse(
        latitude=lat, longitude=lng, radius_miles=radius,
        radius_applied=False,
        source="nces_edge",
        districts=[
            SchoolDistrict(
                district_id=str(live.get("leaid") or uuid.uuid4()),
                district_name=str(live["district_name"]),
                district_type="unified",  # the queried NCES layer is unified districts
                state_code=_state_from_fips(live.get("state_fips")),
                county="",
                nces_id=live.get("leaid") or None,
                # NCES EDGE carries boundaries, not quality or size measures.
                # Synthesising a rating, an enrolment or a distance of 0.0 here
                # would invent data the source does not have.
                rating=None,
                enrollment=None,
                student_teacher_ratio=None,
                distance_miles=None,
                website=None,
            )
        ],
    )


@router.get(
    "/api/market/zoning",
    response_model=ZoningResult,
    summary="Zoning classification for a parcel",
)
async def get_zoning(
    parcel_id: str = Query(..., description="Assessor parcel number (APN)"),
    ctx: TenantContext = Depends(require_context),
) -> ZoningResult:
    """Return the zoning classification and development standards for the
    specified parcel.  Data is sourced from county assessor feeds and
    municipal GIS layers ingested by the harvester pipeline.
    """
    if not parcel_id or len(parcel_id) > 64:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="parcel_id must be 1–64 characters.",
        )
    row = await _fetchrow(
        ctx,
        "SELECT * FROM parcel_zoning WHERE parcel_id = $1",
        parcel_id,
    )
    if not row:
        await _require_dataset_loaded(ctx, "parcel_zoning")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Zoning data not found for parcel {parcel_id!r}.",
        )
    return ZoningResult(
        parcel_id=parcel_id,
        zone_code=row["zone_code"],
        zone_description=row.get("zone_description", ""),
        zone_category=row.get("zone_category", "residential"),
        overlays=row.get("overlays") or [],
        permitted_uses=row.get("permitted_uses") or [],
        conditional_uses=row.get("conditional_uses") or [],
        max_height_ft=_num(row.get("max_height_ft")),
        max_density_units_per_acre=_num(row.get("max_density_units_per_acre")),
        min_lot_sqft=row.get("min_lot_sqft"),
        setback_front_ft=_num(row.get("setback_front_ft")),
        setback_rear_ft=_num(row.get("setback_rear_ft")),
        setback_side_ft=_num(row.get("setback_side_ft")),
        jurisdiction=row.get("jurisdiction"),
    )


# ===========================================================================
# 5. Compliance Engine API
# ===========================================================================

