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

    Data is sourced from the ``state_market_stats`` materialised view, which
    the harvester pipeline refreshes nightly from public data feeds.
    """
    code = _require_state(state_code)
    row = await _fetchrow(
        ctx,
        "SELECT * FROM state_market_stats WHERE state_code = $1",
        code,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Market data not available for {code}.",
        )
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
        as_of_date=row.get("as_of_date"),
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

    The lookup queries the ``fema_flood_zones`` spatial table (PostGIS).  Data
    is sourced from the FEMA National Flood Hazard Layer (NFHL).

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
        # Coordinate not found in NFHL — return unknown zone X (outside SFHA).
        return FloodZoneResult(
            latitude=lat,
            longitude=lng,
            fema_zone="X",
            zone_description="Area of minimal flood hazard (outside SFHA). NFHL data not available for this location.",
            flood_insurance_required=False,
        )

    zone = row.get("flood_zone", "X")
    sfha_zones = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
    insurance_required = any(zone.startswith(z) for z in sfha_zones)

    zone_descriptions: dict[str, str] = {
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
    description = zone_descriptions.get(zone, f"Flood zone {zone} — consult FEMA FIRM panel.")

    return FloodZoneResult(
        latitude=lat,
        longitude=lng,
        fema_zone=zone,
        zone_description=description,
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
    """Return school districts whose centroid falls within ``radius`` miles of
    the given coordinate.  Results are ordered nearest-first.
    """
    meters = radius * 1609.34
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
    districts = [
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
    ]
    return SchoolsResponse(latitude=lat, longitude=lng, radius_miles=radius, districts=districts)


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

