"""
mls_portal.py — direct MLS browse portal (router prefix /api/mls).

The browse surface reads only normalized rows from authorized direct MLS/RESO
board feeds. It never calls a listing aggregator, scrapes a member/consumer
portal, or returns quarantined historical ``mls_id='rentcast'`` rows.

Routes:
  GET /api/mls/search          — paged, filtered browse (GET; the existing
                                 routes_mls POST /api/mls/search is separate).
  GET /api/mls/listings/{id}   — single listing detail (plural path avoids the
                                 existing GET /api/mls/listing/{id} in
                                 state_compliance/routes_mls.py, so routing is
                                 deterministic regardless of include order).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from db.connection import tenant_tx
from tenancy import TenantContext, require_context
from marketplace_engine import rank_buyer_request
from data_coverage import summary as data_coverage_summary

logger = logging.getLogger("oracle.mls_portal")

router = APIRouter(prefix="/api/mls", tags=["MLS Portal"])

LISTING_FRESH_SECONDS = 24 * 3600
LISTING_EXPIRED_SECONDS = 72 * 3600

PAGE_SIZE = 24
THIRD_PARTY_LISTING_SOURCE_IDS = frozenset({"rentcast"})
PUBLIC_PROPERTY_COVERAGE = data_coverage_summary()["property"]

_STATE_RE = re.compile(r"^[A-Za-z]{2}$")


# ── small coercers (mirror data_integrations/listings_feed.py) ────────────────

def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime,)):
        return v.isoformat()
    return str(v)


def _json_object(value: Any) -> dict[str, Any]:
    """Normalize asyncpg JSON/JSONB values across codec configurations."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# ── serializer ────────────────────────────────────────────────────────────────

def _listing_json(r: dict) -> dict:
    bf = r.get("baths_full")
    bh = r.get("baths_half")
    baths = None
    if bf is not None or bh is not None:
        baths = (bf or 0) + 0.5 * (bh or 0)
    photos = r.get("photos") or []
    updated_at = r.get("last_updated")
    age_seconds: Optional[int] = None
    if isinstance(updated_at, datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
    if age_seconds is None:
        freshness = "unknown"
    elif age_seconds <= LISTING_FRESH_SECONDS:
        freshness = "fresh"
    elif age_seconds <= LISTING_EXPIRED_SECONDS:
        freshness = "stale"
    else:
        freshness = "expired"
    return {
        "id": str(r.get("id")),
        "mls_number": r.get("mls_number") or "",
        "address": r.get("address") or "",
        "city": r.get("city") or "",
        "state": r.get("state_code") or "",
        "zip": r.get("zip_code") or "",
        "county": r.get("county") or "",
        "latitude": _num(r.get("latitude")),
        "longitude": _num(r.get("longitude")),
        "price": _num(r.get("list_price")),
        "orig_price": _num(r.get("orig_list_price")),
        "status": r.get("status") or "active",
        "property_type": r.get("property_type") or "",
        "beds": r.get("beds"),
        "baths": baths,
        "baths_full": bf,
        "baths_half": bh,
        "sqft": r.get("sqft"),
        "lot_sqft": r.get("lot_sqft"),
        "year_built": r.get("year_built"),
        "hoa_monthly": _num(r.get("hoa_monthly")),
        "days_on_market": r.get("days_on_market"),
        "list_date": _iso(r.get("list_date")),
        "description": r.get("description"),
        "photos": photos,
        "cover_url": photos[0] if photos else None,
        "source": r.get("mls_id") or "direct_mls",
        "last_updated": _iso(updated_at),
        "freshness": {
            "status": freshness,
            "age_seconds": age_seconds,
            "alert": (
                None
                if freshness == "fresh"
                else "Listing data should be verified with the originating MLS before relying on it."
            ),
        },
    }


def _freshness_summary(listings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"fresh": 0, "stale": 0, "expired": 0, "unknown": 0}
    for listing in listings:
        status_name = listing.get("freshness", {}).get("status", "unknown")
        counts[status_name if status_name in counts else "unknown"] += 1
    return {
        "counts": counts,
        "requires_verification": bool(counts["stale"] or counts["expired"] or counts["unknown"]),
        "fresh_threshold_seconds": LISTING_FRESH_SECONDS,
        "expired_threshold_seconds": LISTING_EXPIRED_SECONDS,
    }


async def _buyer_matches(conn: Any, listing: dict[str, Any]) -> list[dict[str, Any]]:
    """Rank explicit active buy boxes without exposing client contact data."""
    requests = await conn.fetch(
        """
        SELECT r.id,r.request_name,r.criteria,r.expires_at,
               p.states,p.counties,p.property_types,p.min_price,p.max_price,
               p.min_beds,p.min_sqft,p.max_rehab,p.strategies,
               p.verification_status,p.acquisition_history_verified
          FROM buyer_requests r
          JOIN buyer_profiles p ON p.id=r.buyer_profile_id
         WHERE r.status='active' AND p.active=true
           AND (r.expires_at IS NULL OR r.expires_at>now())
        """
    )
    facts = {
        "state": listing.get("state"),
        "county": listing.get("county"),
        "property_type": listing.get("property_type"),
        "asking_price": listing.get("price"),
        "beds": listing.get("beds"),
        "sqft": listing.get("sqft"),
    }
    matches: list[dict[str, Any]] = []
    for request in requests:
        request_data = dict(request)
        criteria = request_data.get("criteria") or {}
        if isinstance(criteria, str):
            try:
                import json

                criteria = json.loads(criteria)
            except ValueError:
                criteria = {}
        ranked = rank_buyer_request(facts, request_data, criteria)
        matches.append(
            {
                "buyer_request_id": str(request["id"]),
                "request_name": request["request_name"],
                **ranked,
            }
        )
    matches.sort(key=lambda item: item["match_score"], reverse=True)
    return matches[:10]


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/search", summary="Retail MLS browse — cached, quota-safe")
async def mls_portal_search(
    city: Optional[str] = Query(default=None, max_length=120),
    state: Optional[str] = Query(default=None, max_length=2),
    zip: Optional[str] = Query(default=None, max_length=10),  # noqa: A002 — public query name
    min_price: Optional[float] = Query(default=None, ge=0),
    max_price: Optional[float] = Query(default=None, ge=0),
    beds: Optional[int] = Query(default=None, ge=0, le=20),
    property_type: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1, le=1000),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Paged browse over direct, authorized MLS/RESO rows already ingested."""
    conditions: list[str] = ["mls_id <> ALL($1::text[])"]
    args: list[Any] = [list(THIRD_PARTY_LISTING_SOURCE_IDS)]

    def _arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    if city and city.strip():
        conditions.append(f"city ILIKE {_arg(city.strip())}")
    if state and _STATE_RE.match(state.strip()):
        conditions.append(f"state_code = {_arg(state.strip().upper())}")
    if zip and zip.strip():
        conditions.append(f"zip_code = {_arg(zip.strip())}")
    if min_price is not None:
        conditions.append(f"list_price >= {_arg(min_price)}")
    if max_price is not None:
        conditions.append(f"list_price <= {_arg(max_price)}")
    if beds is not None:
        conditions.append(f"beds >= {_arg(beds)}")
    if property_type and property_type.strip():
        conditions.append(f"property_type ILIKE {_arg(property_type.strip())}")

    where = " AND ".join(conditions)
    offset = (page - 1) * PAGE_SIZE

    count_q = f"SELECT COUNT(*) AS n FROM oracle_mls_listings WHERE {where}"
    data_q = (
        f"SELECT * FROM oracle_mls_listings WHERE {where} "
        f"ORDER BY last_updated DESC NULLS LAST, list_price DESC NULLS LAST, "
        f"mls_id ASC, mls_number ASC "
        f"LIMIT {_arg(PAGE_SIZE)} OFFSET {_arg(offset)}"
    )

    try:
        async with tenant_tx(ctx) as conn:
            count_row = await conn.fetchrow(count_q, *args[:-2])
            total = int(count_row["n"]) if count_row else 0
            rows = [dict(r) for r in await conn.fetch(data_q, *args)]
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("MLS portal search failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    listings = [_listing_json(r) for r in rows]
    sources = sorted({item["source"] for item in listings if item.get("source")})
    return {
        "listings": listings,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_more": offset + len(listings) < total,
        "degraded": False,
        "source": "combined authorized listing cache",
        "sources": sources,
        "notice": None,
        "freshness": _freshness_summary(listings),
    }


@router.get("/listings/{listing_id}", summary="Single MLS listing detail")
async def mls_portal_listing(
    listing_id: str,
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Full detail for one cached listing. 422 on a malformed id, 404 if absent."""
    try:
        uuid.UUID(listing_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "listing_id must be a UUID")

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM oracle_mls_listings
                 WHERE id = $1::uuid AND mls_id <> ALL($2::text[])
                """,
                listing_id,
                list(THIRD_PARTY_LISTING_SOURCE_IDS),
            )
            listing = _listing_json(dict(row)) if row else None
            matches = await _buyer_matches(conn, listing) if listing else []
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("MLS portal detail failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Listing {listing_id!r} not found.")
    return {**listing, "buyer_matches": matches}


@router.get("/health", summary="MLS source health and freshness")
async def mls_portal_health(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Report live-feed age without making a provider request."""
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT mls_id,COUNT(*) AS listing_count,MAX(last_updated) AS last_updated,
                       COUNT(*) FILTER (WHERE last_updated >= now()-interval '24 hours') AS fresh_count,
                       COUNT(*) FILTER (WHERE last_updated < now()-interval '24 hours'
                                         AND last_updated >= now()-interval '72 hours') AS stale_count,
                       COUNT(*) FILTER (WHERE last_updated < now()-interval '72 hours') AS expired_count
                  FROM oracle_mls_listings
                 WHERE mls_id <> ALL($1::text[])
                 GROUP BY mls_id ORDER BY mls_id
                """,
                list(THIRD_PARTY_LISTING_SOURCE_IDS),
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("MLS health failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")
    sources: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        last_updated = row.get("last_updated")
        if isinstance(last_updated, datetime) and last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)
        age_seconds = (
            max(0, int((datetime.now(timezone.utc) - last_updated).total_seconds()))
            if isinstance(last_updated, datetime)
            else None
        )
        health = (
            "unknown"
            if age_seconds is None
            else "healthy"
            if age_seconds <= LISTING_FRESH_SECONDS
            else "degraded"
            if age_seconds <= LISTING_EXPIRED_SECONDS
            else "unhealthy"
        )
        sources.append(
            {
                "source": row["mls_id"],
                "health": health,
                "last_updated": _iso(last_updated),
                "age_seconds": age_seconds,
                "listing_count": int(row["listing_count"]),
                "fresh_count": int(row["fresh_count"]),
                "stale_count": int(row["stale_count"]),
                "expired_count": int(row["expired_count"]),
            }
        )
    overall = (
        "empty"
        if not sources
        else "unhealthy"
        if any(item["health"] == "unhealthy" for item in sources)
        else "degraded"
        if any(item["health"] in {"degraded", "unknown"} for item in sources)
        else "healthy"
    )
    return {
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "live_provider_called": False,
    }


# ── Shared public-property catalog ───────────────────────────────────────────
#
# Public assessor/parcel facts live in `public_property_records`, separate from
# tenant-private CRM leads.  The catalog contains only an explicit field
# allow-list and is read-only for ordinary users.  Linking a catalog record to a
# client creates a private tenant lead in crm.py; browsing never exposes another
# tenant's notes, contacts, underwriting, motivation, or deal state.

_PUBLIC_RECORD_SELECT = """
    id::text                                   AS id,
    id::text                                   AS public_record_id,
    parcel_id,
    state,
    COALESCE(
        county,
        CASE
            WHEN coverage_scope LIKE 'county:%'
            THEN substring(coverage_scope FROM 8)
        END
    )                                           AS county,
    city,
    zip_code                                   AS zip,
    address,
    owner_name,
    owner_type,
    public_record_value                        AS price,
    last_sale_price,
    reported_record_date                       AS last_sale_date,
    bedrooms                                   AS beds,
    bathrooms                                  AS baths,
    rooms,
    year_built,
    property_class,
    zoning_district,
    land_use,
    lot_area_sqft,
    building_area_sqft                         AS sqft,
    latitude,
    longitude,
    source_key,
    source_name,
    coverage_scope,
    detail_level,
    observed_fields,
    verification_required,
    record_refreshed_at,
    dataset_version,
    source_metadata
"""


async def _lead_ids_for_records(conn, rows: list[dict]) -> dict[tuple[str, str], str]:
    """Map (parcel_id, state) -> this tenant's lead id, for a page of records.

    The public catalog is shared across tenants; a lead is the tenant's own row
    for the same parcel. Everything downstream of the property card is keyed on
    that lead id — the tour resolver, the media list, the tier badge — so a page
    served without it strands every interior affordance regardless of what has
    actually been captured.

    One query per page, not per row. Called inside an existing tenant_tx, so RLS
    scopes the result to the caller's tenant with no predicate needed here.
    Matches the (parcel_id, state) lookup crm.link_client_house already relies on.
    """
    keys = {
        (str(r["parcel_id"]), str(r["state"]))
        for r in rows
        if r.get("parcel_id") and r.get("state")
    }
    if not keys:
        return {}

    parcels = [k[0] for k in keys]
    states = [k[1] for k in keys]
    found = await conn.fetch(
        """
        SELECT DISTINCT ON (parcel_id, state) parcel_id, state, id
          FROM leads
         WHERE (parcel_id, state) = ANY(
                   SELECT * FROM unnest($1::text[], $2::text[])
               )
         ORDER BY parcel_id, state, updated_at DESC, id ASC
        """,
        parcels,
        states,
    )
    return {(str(r["parcel_id"]), str(r["state"])): str(r["id"]) for r in found}


def _public_record_json(r: dict, lead_ids: Optional[dict] = None) -> dict:
    """Map one allow-listed catalog row to the existing property-card contract.

    `lead_ids` comes from _lead_ids_for_records. Absent (or no match) leaves
    lead_id None, which is correct: the tenant has no lead for this parcel, so
    there is nothing captured against it and the tour stays at exterior tier.
    """
    parcel_key = (str(r.get("parcel_id") or ""), str(r.get("state") or ""))
    record = {
        "id": r.get("id"),
        "public_record_id": r.get("public_record_id") or r.get("id"),
        "lead_id": (lead_ids or {}).get(parcel_key),
        "mls_number": r.get("parcel_id") or "",
        "parcel_id": r.get("parcel_id") or "",
        "address": r.get("address") or "",
        "city": r.get("city") or "",
        "state": r.get("state") or "",
        "zip": r.get("zip") or "",
        "county": r.get("county") or "",
        "latitude": _num(r.get("latitude")),
        "longitude": _num(r.get("longitude")),
        "price": _num(r.get("price")),
        "orig_price": None,
        "status": "public_record",
        "property_type": r.get("land_use") or r.get("property_class"),
        "beds": _num(r.get("beds")),
        "baths": _num(r.get("baths")),
        "sqft": _num(r.get("sqft")),
        "lot_sqft": _num(r.get("lot_area_sqft")),
        "year_built": int(r["year_built"]) if r.get("year_built") is not None else None,
        "rooms": _num(r.get("rooms")),
        "property_class": r.get("property_class"),
        "last_sale_price": _num(r.get("last_sale_price")),
        "hoa_monthly": None,
        "days_on_market": None,
        "list_date": None,
        "description": None,
        "photos": [],
        "cover_url": None,
        "source": r.get("source_name") or r.get("source_key") or "public property record",
        "source_key": r.get("source_key"),
        "last_updated": _iso(r.get("record_refreshed_at")),
        "owner_name": r.get("owner_name") or "",
        "owner_type": r.get("owner_type") or "",
        "last_sale_date": _iso(r.get("last_sale_date")),
        "zoning_district": r.get("zoning_district"),
        "land_use": r.get("land_use"),
        "detail_level": r.get("detail_level") or "limited",
        "observed_fields": list(r.get("observed_fields") or []),
        "verification_required": r.get("verification_required") is not False,
        "coverage_scope": r.get("coverage_scope") or "source scope not declared",
        "dataset_version": r.get("dataset_version"),
        "match_type": r.get("match_type") or "browse",
        "match_score": int(r.get("match_score") or 0),
    }
    required_facts = {
        "assessor_value": record["price"],
        "last_recorded_sale": record["last_sale_price"],
        "bedrooms": record["beds"],
        "bathrooms": record["baths"],
        "square_feet": record["sqft"],
        "lot_square_feet": record["lot_sqft"],
        "year_built": record["year_built"],
        "total_rooms": record["rooms"],
    }
    observed = [key for key, value in required_facts.items() if value is not None]
    missing = [key for key, value in required_facts.items() if value is None]
    metadata = _json_object(r.get("source_metadata"))
    field_sources = (
        metadata.get("published_field_sources", {})
        if metadata
        else {}
    )
    record["fact_coverage"] = {
        "observed": observed,
        "missing": missing,
        "observed_count": len(observed),
        "required_count": len(required_facts),
        "complete": not missing,
        "source_fields": field_sources if isinstance(field_sources, dict) else {},
        "policy": "source_published_only",
    }
    return record


def _public_coverage_json() -> dict[str, Any]:
    return {
        "jurisdictions_live": int(PUBLIC_PROPERTY_COVERAGE["live"]),
        "statewide_jurisdictions": int(PUBLIC_PROPERTY_COVERAGE["live_statewide"]),
        "locally_scoped_jurisdictions": int(PUBLIC_PROPERTY_COVERAGE["city_scoped"]),
        "geometry_only_jurisdictions": int(PUBLIC_PROPERTY_COVERAGE["geometry_only"]),
        "nationwide_complete": (
            int(PUBLIC_PROPERTY_COVERAGE["live_statewide"])
            == int(PUBLIC_PROPERTY_COVERAGE["live"])
            and int(PUBLIC_PROPERTY_COVERAGE["geometry_only"]) == 0
        ),
        "notice": (
            "Results include every record currently harvested from configured public sources. "
            "Some states are county- or city-scoped, and source fields vary by jurisdiction."
        ),
    }


async def _reconcile_sparse_cook_record(
    *,
    query_text: str,
    rows: list[dict[str, Any]],
    ctx: TenantContext,
) -> bool:
    """Replace an address-only Chicago violation hit with its assessor PIN."""
    if not re.match(r"^\s*\d{1,7}\s+\S+", query_text):
        return False
    sparse = next(
        (
            row for row in rows
            if row.get("source_key") == "chicago_building_violations"
            and row.get("address")
        ),
        None,
    )
    if not sparse:
        return False
    if any(
        row.get("source_key") == "regional_parcels_il"
        and row.get("address")
        for row in rows
    ):
        return False
    try:
        from harvesters.base import upsert_public_records
        from harvesters.il_cook import IllinoisCookHarvester

        harvester = IllinoisCookHarvester(
            ctx.tenant_id,
            agent_id="cook-address-reconciler",
        )
        records = await asyncio.wait_for(
            harvester.lookup_address(str(sparse["address"])),
            timeout=30,
        )
        if not records:
            return False
        await upsert_public_records(
            ctx.tenant_id,
            "cook-address-reconciler",
            records,
            metrics=harvester.metrics,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - sparse result still remains usable
        logger.warning(
            "Cook County exact-address reconciliation failed for %r: %s",
            str(sparse.get("address"))[:120],
            exc,
        )
        return False


def _has_unchecked_public_facts(row: dict[str, Any]) -> bool:
    """Return true when a supported source can still perform a targeted join."""
    metadata = _json_object(row.get("source_metadata"))
    enrichment = (
        metadata.get("targeted_enrichment", {})
        if isinstance(metadata, dict)
        else {}
    )
    if isinstance(enrichment, dict) and enrichment.get("completed") is True:
        return False
    return any(
        row.get(field) is None
        for field in (
            "county",
            "last_sale_price",
            "beds",
            "baths",
            "rooms",
            "year_built",
            "property_class",
            "land_use",
            "lot_area_sqft",
            "sqft",
        )
    )


async def _reconcile_sparse_public_record(
    *,
    rows: list[dict[str, Any]],
    ctx: TenantContext,
    exact_match_only: bool,
) -> bool:
    """Join an exact sparse catalog row against its official detail sources.

    This remains bounded to one property per request. Batch coverage continues
    through the firehose, while an opened or exact-matched record can be made
    useful immediately without crawling a third-party listing site.
    """
    candidate = next(
        (
            row
            for row in rows[:5]
            if row.get("state") == "FL"
            and row.get("source_key") in {"firehose:FL", "regional_parcels_fl"}
            and row.get("parcel_id")
            and _has_unchecked_public_facts(row)
            and (
                not exact_match_only
                or int(row.get("match_score") or 0) >= 98
            )
        ),
        None,
    )
    if candidate is None:
        return False
    try:
        from harvesters.base import upsert_public_records
        from harvesters.fl_fdor import FloridaFDORHarvester

        harvester = FloridaFDORHarvester(
            ctx.tenant_id,
            agent_id="fl-parcel-reconciler",
        )
        records = await asyncio.wait_for(
            harvester.lookup_parcel(str(candidate["parcel_id"])),
            timeout=30,
        )
        if not records:
            return False
        metrics = {
            **harvester.metrics,
            "source_key": str(candidate.get("source_key") or "firehose:FL"),
        }
        await upsert_public_records(
            ctx.tenant_id,
            "fl-parcel-reconciler",
            records,
            metrics=metrics,
        )

        # Keep the raw observation for this one parcel. /api/intelligence will
        # not score anything without a citable source_record_id, and bulk
        # retention is off for the parcel firehoses because storing raw JSON for
        # every parcel in 51 states is unbounded. A property somebody just
        # opened is precisely the one worth a row.
        try:
            retained = await harvester.retain_observations(
                harvester.last_raw_rows, reason="parcel_reconciliation"
            )
            if retained:
                logger.info(
                    "Retained %d observation(s) for researched parcel %s",
                    retained, str(candidate.get("parcel_id"))[:80],
                )
        except Exception as exc:  # noqa: BLE001 — the reconciliation itself succeeded
            logger.warning(
                "Could not retain observation for parcel %r: %s",
                str(candidate.get("parcel_id"))[:120], exc,
            )

        return True
    except Exception as exc:  # noqa: BLE001 - existing sparse row remains usable
        logger.warning(
            "Florida exact-parcel reconciliation failed for %r: %s",
            str(candidate.get("parcel_id"))[:120],
            exc,
        )
        return False


@router.get("/pipeline", include_in_schema=False)
@router.get("/public-records", summary="Search the shared source-backed property catalog")
async def mls_pipeline_search(
    city: Optional[str] = Query(default=None, max_length=120),
    state: Optional[str] = Query(default=None, max_length=2),
    zip: Optional[str] = Query(default=None, max_length=10),  # noqa: A002 — public query name
    min_price: Optional[float] = Query(default=None, ge=0),
    max_price: Optional[float] = Query(default=None, ge=0),
    beds: Optional[int] = Query(default=None, ge=0, le=20),
    q: Optional[str] = Query(default=None, max_length=160),
    page: int = Query(default=1, ge=1, le=10_000),
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Accurate paged search over shared, allow-listed public property facts."""
    conditions: list[str] = []
    args: list[Any] = []

    def _arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    normalized_state = (
        state.strip().upper()
        if state and _STATE_RE.match(state.strip())
        else None
    )
    if normalized_state:
        conditions.append(f"state = {_arg(normalized_state)}")
    if city and city.strip():
        conditions.append(f"city ILIKE {_arg(city.strip())}")
    if zip and zip.strip():
        conditions.append(f"zip_code = {_arg(zip.strip())}")
    if beds is not None:
        conditions.append(f"bedrooms >= {_arg(beds)}")
    if min_price is not None:
        conditions.append(f"public_record_value >= {_arg(min_price)}")
    if max_price is not None:
        conditions.append(f"public_record_value <= {_arg(max_price)}")

    rank_sql = "0"
    match_type_sql = "'browse'"
    if q and q.strip():
        query_text = " ".join(q.split())
        normalized_query = re.sub(r"[^a-z0-9]", "", query_text.lower())
        contains_arg = _arg(f"%{query_text}%")
        exact_arg = _arg(normalized_query)
        conditions.append(
            f"(search_document ILIKE {contains_arg} "
            f"OR regexp_replace(lower(parcel_id), '[^a-z0-9]', '', 'g') = {exact_arg} "
            f"OR regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9]', '', 'g') = {exact_arg})"
        )
        conditions.append(
            "("
            "source_key <> 'chicago_building_violations' "
            "OR address IS NULL "
            "OR NOT EXISTS ("
            "SELECT 1 FROM public_property_records canonical "
            "WHERE canonical.state = public_property_records.state "
            "AND canonical.source_key = 'regional_parcels_il' "
            "AND canonical.address IS NOT NULL "
            "AND regexp_replace(lower(canonical.address), '[^a-z0-9]', '', 'g') "
            "= regexp_replace(lower(public_property_records.address), '[^a-z0-9]', '', 'g')"
            ")"
            ")"
        )
        rank_sql = (
            f"CASE "
            f"WHEN regexp_replace(lower(parcel_id), '[^a-z0-9]', '', 'g') = {exact_arg} THEN 100 "
            f"WHEN regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9]', '', 'g') = {exact_arg} THEN 98 "
            f"WHEN address ILIKE {contains_arg} THEN 85 "
            f"WHEN owner_name ILIKE {contains_arg} THEN 70 "
            f"WHEN city ILIKE {contains_arg} THEN 55 "
            f"ELSE 40 END"
        )
        match_type_sql = (
            f"CASE "
            f"WHEN regexp_replace(lower(parcel_id), '[^a-z0-9]', '', 'g') = {exact_arg} THEN 'parcel_exact' "
            f"WHEN regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9]', '', 'g') = {exact_arg} THEN 'address_exact' "
            f"WHEN address ILIKE {contains_arg} THEN 'address_partial' "
            f"WHEN owner_name ILIKE {contains_arg} THEN 'owner_partial' "
            f"WHEN city ILIKE {contains_arg} THEN 'city_partial' "
            f"ELSE 'text_partial' END"
        )

    where = " AND ".join(conditions) if conditions else "TRUE"
    is_unfiltered = where == "TRUE"
    offset = (page - 1) * PAGE_SIZE
    count_args = list(args)
    count_q = f"SELECT COUNT(*) AS n FROM public_property_records WHERE {where}"

    # Fetch one row past the page so has_more never depends on `total` — that
    # keeps pagination correct even when total is an estimate (below).
    data_q = (
        f"SELECT {_PUBLIC_RECORD_SELECT}, {rank_sql} AS match_score, "
        f"{match_type_sql} AS match_type "
        f"FROM public_property_records WHERE {where} "
        f"ORDER BY match_score DESC, record_refreshed_at DESC, id ASC "
        f"LIMIT {_arg(PAGE_SIZE + 1)} OFFSET {_arg(offset)}"
    )

    async def _count_total(conn) -> tuple[int, bool]:
        """(total, is_estimate).

        An unfiltered browse has no WHERE to bound it, so COUNT(*) is a full
        scan of the whole catalog on every page load — measured at 6.3M rows /
        4.7GB, expensive on any box and the thing that OOM'd this one. The
        planner's own row-count estimate (`pg_class.reltuples`, refreshed by
        autovacuum/ANALYZE) costs nothing and is accurate enough for a browse
        total; it must never be presented as exact, so callers get the flag
        and the response says so via `total_is_estimate`.
        """
        if not is_unfiltered:
            count_row = await conn.fetchrow(count_q, *count_args)
            return (int(count_row["n"]) if count_row else 0), False
        estimate_row = await conn.fetchrow(
            "SELECT reltuples::bigint AS n FROM pg_class WHERE oid = 'public_property_records'::regclass"
        )
        estimate = int(estimate_row["n"]) if estimate_row and estimate_row["n"] is not None else 0
        return max(estimate, 0), True

    lead_ids: dict = {}
    try:
        async with tenant_tx(ctx) as conn:
            total, total_is_estimate = await _count_total(conn)
            rows = [dict(r) for r in await conn.fetch(data_q, *args)]
        if q and q.strip() and await _reconcile_sparse_cook_record(
            query_text=" ".join(q.split()),
            rows=rows,
            ctx=ctx,
        ):
            async with tenant_tx(ctx) as conn:
                total, total_is_estimate = await _count_total(conn)
                rows = [dict(r) for r in await conn.fetch(data_q, *args)]
        if q and q.strip() and await _reconcile_sparse_public_record(
            rows=rows,
            ctx=ctx,
            exact_match_only=True,
        ):
            async with tenant_tx(ctx) as conn:
                total, total_is_estimate = await _count_total(conn)
                rows = [dict(r) for r in await conn.fetch(data_q, *args)]

        # After every reconciliation path above has settled `rows` — the two
        # re-reads are conditional, so resolving inside one of them would leave
        # the other paths without lead ids.
        has_more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        async with tenant_tx(ctx) as conn:
            lead_ids = await _lead_ids_for_records(conn, rows)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("Public property record search failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    listings = [_public_record_json(r, lead_ids) for r in rows]
    return {
        "listings": listings,
        "total": total,
        "total_is_estimate": total_is_estimate,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_more": has_more,
        "degraded": False,
        "source": "shared public property catalog",
        "notice": _public_coverage_json()["notice"],
        "coverage": _public_coverage_json(),
        "accuracy": {
            "ranked_exact_matches": bool(q and q.strip()),
            "deduplicated_by": "source + state + source record id",
            "verification_required": True,
        },
    }


@router.get("/pipeline/{record_id}", include_in_schema=False)
@router.get("/public-records/{record_id}", summary="Single public property record")
async def mls_pipeline_listing(
    record_id: str,
    ctx: TenantContext = Depends(require_context),
) -> dict:
    """Return one allow-listed shared record; CRM data is never joined."""
    try:
        uuid.UUID(record_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "record_id must be a UUID")

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                f"SELECT {_PUBLIC_RECORD_SELECT}, 0 AS match_score, "
                f"'record_id_exact' AS match_type "
                f"FROM public_property_records WHERE id = $1::uuid",
                record_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("Public property record detail failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Public record {record_id!r} not found.")
    row_dict = dict(row)
    if await _reconcile_sparse_public_record(
        rows=[row_dict],
        ctx=ctx,
        exact_match_only=False,
    ):
        try:
            async with tenant_tx(ctx) as conn:
                refreshed = await conn.fetchrow(
                    f"SELECT {_PUBLIC_RECORD_SELECT}, 0 AS match_score, "
                    f"'record_id_exact' AS match_type "
                    f"FROM public_property_records WHERE id = $1::uuid",
                    record_id,
                )
            if refreshed:
                row_dict = dict(refreshed)
        except Exception as exc:  # noqa: BLE001 - return the first valid row
            logger.warning("Refreshed public record read failed for %s: %s", record_id, exc)

    # Resolve the tenant's lead for this parcel so the detail card can reach the
    # tour, media and tier badge — same reason as the list handler above.
    lead_ids: dict = {}
    try:
        async with tenant_tx(ctx) as conn:
            lead_ids = await _lead_ids_for_records(conn, [row_dict])
    except Exception as exc:  # noqa: BLE001 — the record itself is still valid
        logger.warning("Lead resolution failed for public record %s: %s", record_id, exc)

    return _public_record_json(row_dict, lead_ids)
