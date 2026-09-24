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

@router.get(
    "/api/mls/regions",
    response_model=list[MLSRegion],
    summary="All MLS boards with state and county coverage",
)
async def list_mls_regions(
    state_code: Optional[str] = Query(default=None, description="Filter by state"),
    ctx: TenantContext = Depends(require_context),
) -> list[MLSRegion]:
    """Return all known MLS boards.  Optionally filter by state code.

    ``mls_boards`` is the curated coverage catalogue (which board covers which
    states/counties). It can be empty on a deployment that has feeds wired but
    no catalogue loaded — so when it is, fall back to ``mls_sync_status``,
    which lists the feeds actually configured and syncing. That keeps the
    board-coverage strip in the browse UI honest: a configured feed that has
    pulled nothing yet is not the same as "no board coverage at all".
    """
    query = "SELECT * FROM mls_boards"
    args: list[Any] = []
    if state_code:
        code = _require_state(state_code)
        query += " WHERE $1 = ANY(states)"
        args.append(code)
    query += " ORDER BY mls_name"

    rows = await _fetch(ctx, query, *args)
    if rows:
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

    # No catalogue — report the configured feeds instead. A state filter can't
    # be honoured here (sync status carries no coverage geography), so it
    # returns every configured feed.
    # Narrowed to entitled feeds: this listed every configured board — name,
    # type and listing count — to every tenant, which leaks both the existence
    # and the size of another brokerage's licensed feed.
    from mls_health import visible_feeds_with
    _allowed, _ = await visible_feeds_with(lambda q, *a: _fetch(ctx, q, *a), ctx)
    feeds = await _fetch(
        ctx,
        "SELECT mls_id, mls_name, feed_type, listings_synced "
        "FROM mls_sync_status WHERE mls_id = ANY($1::text[]) "
        "ORDER BY mls_name, mls_id",
        _allowed,
    )
    if not feeds:
        # Nothing catalogued and nothing configured — the dataset was never
        # loaded, which is a different answer from "this state has no boards".
        await _require_dataset_loaded(ctx, "mls_boards")
    return [
        MLSRegion(
            mls_id=f["mls_id"],
            mls_name=f.get("mls_name") or f["mls_id"],
            states=[],
            counties=[],
            member_count=None,
            listing_count=f.get("listings_synced"),
            feed_type=f.get("feed_type") or "RESO_Web_API",
            data_sharing="IDX_only",
            website=None,
        )
        for f in feeds
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

    ``health`` uses the shared feed vocabulary from ``mls_health`` —
    ``not_configured``, ``configured``, ``backfilling``, ``ready``, ``stale``,
    ``degraded``, ``error``, ``auth_error``, ``rate_limited`` — so this endpoint
    and the feed list can never disagree about the same feed.

    ``sync_lag_minutes`` is derived from the last successful sync, not read from
    the column of that name: every sink writes that column as a literal 0.
    """
    # An unentitled tenant gets the same 404 as a nonexistent feed — asking
    # about another brokerage's board must not confirm that it exists.
    from mls_health import visible_feeds_with
    _allowed, _ = await visible_feeds_with(lambda q, *a: _fetch(ctx, q, *a), ctx)
    row = await _fetchrow(
        ctx,
        "SELECT * FROM mls_sync_status WHERE mls_id = $1 AND mls_id = ANY($2::text[])",
        mls_id,
        _allowed,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MLS region {mls_id!r} not found.",
        )

    # Health comes from mls_health.compute_health and nowhere else.
    #
    # This route used to derive its own from `sync_lag_minutes` and
    # `errors_last_24h`. Both are written as a literal 0 by every sink and by
    # nothing else, so the branch could only ever land on "healthy" — this
    # endpoint reported a feed that had not synced in a year as healthy, which
    # is the single worst thing a health endpoint can do. It also meant two
    # health vocabularies in one service, disagreeing about the same feed.
    from mls_health import compute_health, feed_age_seconds

    health = compute_health(dict(row))
    age = feed_age_seconds(dict(row))
    lag = int(age // 60) if age is not None else None

    return MLSSyncStatus(
        mls_id=mls_id,
        mls_name=row.get("mls_name", ""),
        feed_type=row.get("feed_type", "RESO_Web_API"),
        last_sync_at=row.get("last_sync_at"),
        listings_synced=row.get("listings_synced", 0),
        errors_last_24h=row.get("errors_last_24h", 0),
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
    # Which feeds this caller may read at all, intersected with what they
    # asked for. Resolved before the query is composed so the bound parameter
    # is a server-side value, not a request one.
    # This route runs its queries through the module-level `tenant_tx`, so the
    # entitlement read uses the same seam — otherwise it reaches past whatever
    # the caller (or a test) has substituted for the database.
    from mls_health import visible_feeds
    async with tenant_tx(ctx) as _conn:
        _allowed, _ = await visible_feeds(_conn, ctx)
    requested = {str(m).strip() for m in (body.mls_ids or []) if str(m).strip()}
    effective_feeds = sorted(set(_allowed) & requested) if requested else sorted(_allowed)
    if not effective_feeds:
        # No usable coverage is a different answer from no matching listings.
        return MLSSearchResponse(listings=[], total=0,
                                 limit=body.limit, offset=body.offset)

    def _build(include_radius: bool) -> tuple[str, str, list[Any], list[Any]]:
        """Compose the count and page queries; returns (count_q, data_q, count_args, data_args)."""
        conditions: list[str] = ["mls_id <> 'rentcast'"]
        args: list[Any] = []
        idx = 0

        def _arg(v: Any) -> str:
            nonlocal idx
            args.append(v)
            idx += 1
            return f"${idx}"

        # Entitlement, resolved server-side. `body.mls_ids` arrives from the
        # browser: it may NARROW this set and can never widen it. Without this
        # line a caller named any feed it liked and got the whole board — and
        # this route shares its URL prefix with /api/mls/search in mls_portal,
        # which was narrowed, so the leak sat one HTTP verb away from the fix.
        conditions.append(f"mls_id = ANY({_arg(effective_feeds)})")
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
        if include_radius:
            # earth_distance/ll_to_earth come from the `earthdistance` extension,
            # which 0013 creates best-effort — see the retry below.
            conditions.append(
                f"earth_distance(ll_to_earth(latitude, longitude), "
                f"ll_to_earth({_arg(body.lat)}, {_arg(body.lng)})) "
                f"<= {_arg(body.radius_miles * 1609.34)}"
            )

        where = " AND ".join(conditions)
        count_args = list(args)  # everything bound so far — no LIMIT/OFFSET
        count_q = f"SELECT COUNT(*) FROM oracle_mls_listings WHERE {where}"
        data_q = (
            f"SELECT * FROM oracle_mls_listings WHERE {where} "
            f"ORDER BY list_date DESC NULLS LAST "
            f"LIMIT {_arg(body.limit)} OFFSET {_arg(body.offset)}"
        )
        return count_q, data_q, count_args, args

    async def _run(count_q: str, data_q: str, count_args: list, data_args: list):
        async with tenant_tx(ctx) as conn:
            count_row = await conn.fetchrow(count_q, *count_args)
            total = int(count_row["count"]) if count_row else 0
            rows = [dict(r) for r in await conn.fetch(data_q, *data_args)]
            return total, rows

    wants_radius = (
        body.lat is not None and body.lng is not None and body.radius_miles is not None
    )
    radius_applied = wants_radius

    try:
        try:
            total, rows = await _run(*_build(include_radius=wants_radius))
        except Exception as exc:
            # 42883 = undefined_function: the earthdistance extension is absent.
            # Retry without the distance predicate and say so on the response —
            # dropping the filter silently would present listings from anywhere
            # in the dataset as being within the caller's radius.
            if not wants_radius or getattr(exc, "sqlstate", None) != "42883":
                raise
            logger.warning(
                "MLS radius search unavailable (earthdistance/cube missing) — "
                "returning distance-unfiltered results with radius_applied=false."
            )
            radius_applied = False
            total, rows = await _run(*_build(include_radius=False))
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
        radius_applied=radius_applied,
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
    # Narrowed like every other reader. This returned the full listing for any
    # id to any tenant; it is one character away from /api/mls/listings/{id},
    # which was fixed, so an unentitled caller could simply use the other URL.
    from mls_health import visible_feeds_with
    _allowed, _ = await visible_feeds_with(lambda q, *a: _fetch(ctx, q, *a), ctx)
    row = await _fetchrow(
        ctx,
        "SELECT * FROM oracle_mls_listings "
        " WHERE id = $1::uuid AND mls_id <> 'rentcast' AND mls_id = ANY($2::text[])",
        listing_id,
        _allowed,
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
