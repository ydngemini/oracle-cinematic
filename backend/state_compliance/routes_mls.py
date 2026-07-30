"""MLS board registry, sync health, normalized search, and listing detail."""
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
    "/api/mls/regions",
    response_model=list[MLSRegion],
    summary="All MLS boards with state and county coverage",
)
async def list_mls_regions(
    state_code: Optional[str] = Query(default=None, description="Filter by state"),
    ctx: TenantContext = Depends(require_context),
) -> list[MLSRegion]:
    """Return all known MLS boards.  Optionally filter by state code."""
    query = "SELECT * FROM mls_boards"
    args: list[Any] = []
    if state_code:
        code = _require_state(state_code)
        query += " WHERE $1 = ANY(states)"
        args.append(code)
    query += " ORDER BY mls_name"

    rows = await _fetch(ctx, query, *args)
    return [
        MLSRegion(
            mls_id=str(r.get("id", uuid.uuid4())),
            mls_name=r["mls_name"],
            states=r.get("states") or [],
            counties=r.get("counties") or [],
            member_count=r.get("member_count"),
            listing_count=r.get("listing_count"),
            feed_type=r.get("feed_type", "RESO_Web_API"),
            data_sharing=r.get("data_sharing", "IDX_only"),
            website=r.get("website"),
        )
        for r in rows
    ]


@router.get(
    "/api/mls/regions/{mls_id}/status",
    response_model=MLSSyncStatus,
    summary="Sync health for an MLS feed",
)
async def get_mls_sync_status(
    mls_id: str,
    ctx: TenantContext = Depends(require_context),
) -> MLSSyncStatus:
    """Return feed synchronisation health for the specified MLS board.

    The ``health`` field summarises: ``healthy`` (lag < 60 min, no errors),
    ``degraded`` (lag 60–240 min or minor errors), ``offline`` (no sync > 4h).
    """
    row = await _fetchrow(
        ctx,
        "SELECT * FROM mls_sync_status WHERE mls_id = $1",
        mls_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MLS region {mls_id!r} not found.",
        )

    lag = row.get("sync_lag_minutes")
    errors = row.get("errors_last_24h", 0)
    if lag is None or lag > 240 or errors > 50:
        health = "offline"
    elif lag > 60 or errors > 5:
        health = "degraded"
    else:
        health = "healthy"

    return MLSSyncStatus(
        mls_id=mls_id,
        mls_name=row.get("mls_name", ""),
        feed_type=row.get("feed_type", "RESO_Web_API"),
        last_sync_at=row.get("last_sync_at"),
        listings_synced=row.get("listings_synced", 0),
        errors_last_24h=errors,
        sync_lag_minutes=lag,
        health=health,
        notes=row.get("notes"),
    )


@router.post(
    "/api/mls/search",
    response_model=MLSSearchResponse,
    summary="Normalized property search across one or more MLSs",
)
async def mls_search(
    body: MLSSearchBody,
    ctx: TenantContext = Depends(require_context),
) -> MLSSearchResponse:
    """Execute a normalized property search against the oracle_mls_listings view.

    The view unions listings from all configured MLS feeds into a single
    schema.  Filters include price range, beds/baths, sqft, property type,
    status, and optional radius search when ``lat``/``lng`` are provided.
    """
    conditions: list[str] = ["mls_id <> 'rentcast'"]
    args: list[Any] = []
    idx = 0

    def _arg(v: Any) -> str:
        nonlocal idx
        args.append(v)
        idx += 1
        return f"${idx}"

    if body.mls_ids:
        conditions.append(f"mls_id = ANY({_arg(body.mls_ids)})")
    if body.state_codes:
        conditions.append(f"state_code = ANY({_arg(body.state_codes)})")
    if body.min_price is not None:
        conditions.append(f"list_price >= {_arg(body.min_price)}")
    if body.max_price is not None:
        conditions.append(f"list_price <= {_arg(body.max_price)}")
    if body.min_beds is not None:
        conditions.append(f"beds >= {_arg(body.min_beds)}")
    if body.min_baths is not None:
        conditions.append(f"(baths_full + baths_half * 0.5) >= {_arg(body.min_baths)}")
    if body.min_sqft is not None:
        conditions.append(f"sqft >= {_arg(body.min_sqft)}")
    if body.max_sqft is not None:
        conditions.append(f"sqft <= {_arg(body.max_sqft)}")
    if body.property_types:
        conditions.append(f"property_type = ANY({_arg(body.property_types)})")
    if body.status:
        conditions.append(f"status = {_arg(body.status)}")
    if body.lat is not None and body.lng is not None and body.radius_miles is not None:
        # PostGIS: earth_distance via the earthdistance extension (miles).
        conditions.append(
            f"earth_distance(ll_to_earth(latitude, longitude), "
            f"ll_to_earth({_arg(body.lat)}, {_arg(body.lng)})) "
            f"<= {_arg(body.radius_miles * 1609.34)}"
        )

    where = " AND ".join(conditions)
    count_q = f"SELECT COUNT(*) FROM oracle_mls_listings WHERE {where}"
    data_q = (
        f"SELECT * FROM oracle_mls_listings WHERE {where} "
        f"ORDER BY list_date DESC NULLS LAST "
        f"LIMIT {_arg(body.limit)} OFFSET {_arg(body.offset)}"
    )

    try:
        async with tenant_tx(ctx) as conn:
            count_row = await conn.fetchrow(count_q, *args[: idx - 2])
            total = int(count_row["count"]) if count_row else 0
            rows = [dict(r) for r in await conn.fetch(data_q, *args)]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("MLS search failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Core offline.",
        )

    listings = [
        NormalizedListing(
            listing_id=str(r.get("id", uuid.uuid4())),
            mls_id=r.get("mls_id", ""),
            mls_number=r.get("mls_number", ""),
            address=r.get("address", ""),
            city=r.get("city", ""),
            state_code=r.get("state_code", ""),
            zip_code=r.get("zip_code", ""),
            county=r.get("county", ""),
            latitude=_num(r.get("latitude")),
            longitude=_num(r.get("longitude")),
            list_price=float(r.get("list_price", 0)),
            orig_list_price=_num(r.get("orig_list_price")),
            status=r.get("status", "active"),
            property_type=r.get("property_type", "residential_1_4"),
            beds=r.get("beds"),
            baths_full=r.get("baths_full"),
            baths_half=r.get("baths_half"),
            sqft=r.get("sqft"),
            lot_sqft=r.get("lot_sqft"),
            year_built=r.get("year_built"),
            hoa_monthly=_num(r.get("hoa_monthly")),
            days_on_market=r.get("days_on_market"),
            list_date=r.get("list_date"),
            close_date=r.get("close_date"),
            close_price=_num(r.get("close_price")),
            description=r.get("description"),
            photos=r.get("photos") or [],
            features=r.get("features") or {},
            last_updated=r.get("last_updated"),
        )
        for r in rows
    ]

    return MLSSearchResponse(
        total_count=total,
        offset=body.offset,
        limit=body.limit,
        listings=listings,
    )


@router.get(
    "/api/mls/listing/{listing_id}",
    response_model=NormalizedListing,
    summary="Normalized listing detail",
)
async def get_mls_listing(
    listing_id: str,
    ctx: TenantContext = Depends(require_context),
) -> NormalizedListing:
    """Return the full normalized listing record for the given ID."""
    # Validate before the ::uuid cast — an unparseable ID would otherwise make
    # asyncpg raise mid-query and surface as a misleading 503. A malformed ID
    # is a client error (422), not a backend outage.
    listing_id = _require_uuid(listing_id, "listing_id")
    row = await _fetchrow(
        ctx,
        "SELECT * FROM oracle_mls_listings WHERE id = $1::uuid AND mls_id <> 'rentcast'",
        listing_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Listing {listing_id!r} not found.",
        )
    return NormalizedListing(
        listing_id=str(row.get("id", listing_id)),
        mls_id=row.get("mls_id", ""),
        mls_number=row.get("mls_number", ""),
        address=row.get("address", ""),
        city=row.get("city", ""),
        state_code=row.get("state_code", ""),
        zip_code=row.get("zip_code", ""),
        county=row.get("county", ""),
        latitude=_num(row.get("latitude")),
        longitude=_num(row.get("longitude")),
        list_price=float(row.get("list_price", 0)),
        orig_list_price=_num(row.get("orig_list_price")),
        status=row.get("status", "active"),
        property_type=row.get("property_type", "residential_1_4"),
        beds=row.get("beds"),
        baths_full=row.get("baths_full"),
        baths_half=row.get("baths_half"),
        sqft=row.get("sqft"),
        lot_sqft=row.get("lot_sqft"),
        year_built=row.get("year_built"),
        hoa_monthly=_num(row.get("hoa_monthly")),
        days_on_market=row.get("days_on_market"),
        list_date=row.get("list_date"),
        close_date=row.get("close_date"),
        close_price=_num(row.get("close_price")),
        description=row.get("description"),
        photos=row.get("photos") or [],
        features=row.get("features") or {},
        last_updated=row.get("last_updated"),
    )


# ===========================================================================
# 4. Market Data API
# ===========================================================================
