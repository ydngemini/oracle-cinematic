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
from mls_switch import require_mls_enabled
from property_match_api import can_decide as can_decide_record_match, confirmation_state
from tenancy import TenantContext, require_context
from marketplace_engine import rank_buyer_request
from data_coverage import summary as data_coverage_summary

logger = logging.getLogger("oracle.mls_portal")

router = APIRouter(prefix="/api/mls", tags=["MLS Portal"])
# The public-records catalog is not MLS: it lived under /api/mls only by
# history. Its canonical home is /api/property-records; the /api/mls/
# public-records paths stay as aliases and keep working while MLS is off
# (D-2026-10-12-01, prompt 11 T11.01).
records_router = APIRouter(prefix="/api/property-records", tags=["Property records"])

LISTING_FRESH_SECONDS = 24 * 3600
LISTING_EXPIRED_SECONDS = 72 * 3600

PAGE_SIZE = 24
# Beyond this the search reports "10,000+" (total_is_exact=false) rather than
# counting every matching listing on every request.
_SEARCH_COUNT_CAP = 10_000
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
        # Key names mirror the oracle_mls_listings columns so the browse UI
        # (MlsSearch.jsx) reads list_price/state_code/zip_code directly — a
        # rename here silently blanks price and location in every result row.
        "state_code": r.get("state_code") or "",
        "zip_code": r.get("zip_code") or "",
        "county": r.get("county") or "",
        "latitude": _num(r.get("latitude")),
        "longitude": _num(r.get("longitude")),
        "list_price": _num(r.get("list_price")),
        "orig_list_price": _num(r.get("orig_list_price")),
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
        "state": listing.get("state_code"),
        "county": listing.get("county"),
        "property_type": listing.get("property_type"),
        "asking_price": listing.get("list_price"),
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

@router.get("/search", summary="Retail MLS browse — cached, quota-safe", dependencies=[Depends(require_mls_enabled)])
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
    # Entitlement is resolved inside the transaction below and appended as a
    # bound parameter. A browser filter may narrow the feed set; nothing it
    # sends can widen it.

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

    offset = (page - 1) * PAGE_SIZE
    # The filter args are shared; LIMIT/OFFSET and the entitlement array are
    # bound separately per query, because count and data bind different
    # numbers of parameters and a shared $N cannot be right for both.
    filter_args = list(args)

    try:
        async with tenant_tx(ctx) as conn:
            from mls_health import coverage_note, visible_feeds
            allowed, feeds = await visible_feeds(conn, ctx)
            if not allowed:
                # No usable MLS data is a different answer from no matching
                # listings, and returning [] for both is how a brokerage
                # concludes nothing is for sale in their market.
                return {
                    "listings": [], "total": 0, "total_is_exact": True, "page": page, "page_size": PAGE_SIZE,
                    "has_more": False, "degraded": True,
                    "source": "combined authorized listing cache", "sources": [],
                    "notice": coverage_note(feeds)["message"]
                              or "No MLS feed is connected for this brokerage.",
                    "coverage": coverage_note(feeds),
                    "feeds": feeds,
                    "freshness": _freshness_summary([]),
                }
            count_args = filter_args + [allowed]
            where_all = " AND ".join(
                conditions + [f"mls_id = ANY(${len(count_args)}::text[])"])
            # Capped: an exact COUNT(*) over a whole state's feed was a
            # parallel seq scan on EVERY search — with the page query, what
            # pushed page-1 p95 to 2.2 s at 25 agents (Mission 8). Past the
            # cap the answer is "10,000+", which is what a person can use.
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM oracle_mls_listings WHERE {where_all} "
                f"LIMIT {_SEARCH_COUNT_CAP + 1}) capped",
                *count_args,
            )
            total = int(count_row["n"]) if count_row else 0
            total_is_exact = total <= _SEARCH_COUNT_CAP
            total = min(total, _SEARCH_COUNT_CAP)

            data_args = filter_args + [allowed, PAGE_SIZE, offset]
            n = len(filter_args)
            where_data = " AND ".join(
                conditions + [f"mls_id = ANY(${n + 1}::text[])"])
            rows = [dict(r) for r in await conn.fetch(
                f"SELECT * FROM oracle_mls_listings WHERE {where_data} "
                # Matches idx_oml_search_order (0116). No NULLS LAST: every
                # column is NOT NULL, and the clause stopped the index from
                # supplying the order, forcing a sort of the whole feed.
                f"ORDER BY last_updated DESC, list_price DESC, "
                f"mls_id ASC, mls_number ASC "
                f"LIMIT ${n + 2} OFFSET ${n + 3}",
                *data_args,
            )]
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("MLS portal search failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    listings = [_listing_json(r) for r in rows]
    sources = sorted({item["source"] for item in listings if item.get("source")})
    note = coverage_note(feeds)
    return {
        "listings": listings,
        "total": total,
        "total_is_exact": total_is_exact,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_more": offset + len(listings) < total,
        # Was hardcoded False, so the response claimed health even while every
        # feed behind it was failing.
        "degraded": note["state"] in ("stale", "partially_stale", "backfilling", "developer_data_only"),
        "source": "combined authorized listing cache",
        "sources": sources,
        "notice": note["message"] or None,
        "coverage": note,
        "feeds": feeds,
        "freshness": _freshness_summary(listings),
    }


@router.get("/listings/{listing_id}", summary="Single MLS listing detail", dependencies=[Depends(require_mls_enabled)])
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
            # Narrowed to the caller's entitled feeds, exactly as search is.
            # Without this an unentitled tenant could fetch a licensed listing
            # by guessing or replaying its UUID — and get its buyer matches
            # with it, which is the CRM half of the leak.
            from mls_health import visible_feeds
            allowed, _feeds = await visible_feeds(conn, ctx)
            row = await conn.fetchrow(
                """
                SELECT * FROM oracle_mls_listings
                 WHERE id = $1::uuid AND mls_id <> ALL($2::text[])
                   AND mls_id = ANY($3::text[])
                """,
                listing_id,
                list(THIRD_PARTY_LISTING_SOURCE_IDS),
                allowed,
            )
            listing = _listing_json(dict(row)) if row else None
            matches = await _buyer_matches(conn, listing) if listing else []
            # Investor buy boxes and the brokerage's own buyer contacts are
            # different populations and are reported separately. Folding them
            # into one list would make "who should I call about this?"
            # ambiguous about whether the answer is a client or a wholesaler.
            crm_buyers = []
            if listing:
                from buyer_matching import buyers_for_listing
                crm_buyers = [
                    {
                        "client_id": m.client_id,
                        "name": m.name,
                        "verdict": m.verdict,
                        "evidence": m.evidence,
                        "unknowns": m.unknowns,
                        "matched_signals": m.matched_signals,
                    }
                    for m in await buyers_for_listing(conn, dict(row), ctx=ctx)
                ]
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("MLS portal detail failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Listing {listing_id!r} not found.")
    # buyer_matches = investor buy boxes; buyers = this brokerage's own
    # contacts. Separate keys because they are different populations and
    # "who should I call about this?" has a different answer for each.
    return {**listing, "buyer_matches": matches, "buyers": crm_buyers}


@router.get("/health", summary="MLS source health and freshness", dependencies=[Depends(require_mls_enabled)])
async def mls_portal_health(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Report live-feed age without making a provider request."""
    try:
        async with tenant_tx(ctx) as conn:
            # Narrowed to entitled feeds, like search and detail. This endpoint
            # reported every feed's listing counts to every tenant, which leaks
            # both the existence and the size of another brokerage's licensed
            # board.
            from mls_health import compute_health, visible_feeds
            allowed, feeds = await visible_feeds(conn, ctx)
            rows = await conn.fetch(
                """
                SELECT mls_id,COUNT(*) AS listing_count,MAX(last_updated) AS last_updated,
                       COUNT(*) FILTER (WHERE last_updated >= now()-interval '24 hours') AS fresh_count,
                       COUNT(*) FILTER (WHERE last_updated < now()-interval '24 hours'
                                         AND last_updated >= now()-interval '72 hours') AS stale_count,
                       COUNT(*) FILTER (WHERE last_updated < now()-interval '72 hours') AS expired_count
                  FROM oracle_mls_listings
                 WHERE mls_id <> ALL($1::text[]) AND mls_id = ANY($2::text[])
                 GROUP BY mls_id ORDER BY mls_id
                """,
                list(THIRD_PARTY_LISTING_SOURCE_IDS),
                allowed,
            )
            # The operator answer (§36): which feed, licensed or not, how
            # healthy, how old, and what failed — without SSH and SQL.
            feed_status = await conn.fetch(
                "SELECT mls_id, mls_name, provider, dataset, license_classification, "
                "       license_reason, agreement_ref, last_success_at, last_attempt_at, "
                "       last_error, last_error_class, consecutive_failures, "
                "       backfill_complete, backfill_records, listings_synced, "
                "       stale_after_minutes FROM mls_sync_status WHERE mls_id = ANY($1::text[])",
                allowed,
            )
            operator = []
            for raw in feed_status:
                row = dict(raw)
                operator.append({
                    "mls_id": row["mls_id"],
                    "mls_name": row.get("mls_name"),
                    "provider": row.get("provider"),
                    "licensed": row.get("license_classification") == "licensed_property_listing",
                    "license_reason": row.get("license_reason"),
                    "agreement_ref": row.get("agreement_ref"),
                    "dataset": row.get("dataset"),
                    "health": compute_health(row),
                    "last_success_at": row.get("last_success_at"),
                    "last_attempt_at": row.get("last_attempt_at"),
                    "consecutive_failures": row.get("consecutive_failures"),
                    "last_error_class": row.get("last_error_class"),
                    # The message itself can carry provider prose; the class is
                    # what an alert should key on.
                    "last_error": (row.get("last_error") or "")[:200] or None,
                    "backfill_complete": row.get("backfill_complete"),
                    "backfill_records": row.get("backfill_records"),
                    "records": row.get("listings_synced"),
                })
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
        # §36: the operator answer — which feed, licensed or not, how healthy,
        # how old, what failed and whether the initial backfill finished.
        "feeds": operator,
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


async def _lead_ids_for_records(conn, rows: list[dict]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map (parcel_id, state) -> this tenant's lead, for a page of records:
    {lead_id, linked, heads_by_record}.

    The public catalog is shared across tenants; a lead is the tenant's own row
    for the same parcel. Everything downstream of the property card is keyed on
    that lead id — the tour resolver, the media list, the tier badge — so a page
    served without it strands every interior affordance regardless of what has
    actually been captured.

    One query per page, not per row. Called inside an existing tenant_tx, so RLS
    scopes the result to the caller's tenant with no predicate needed here.
    Matches the (parcel_id, state) lookup crm.link_client_house already relies on.

    The same query says whether the lead is linked to a client, and carries the
    newest property-match decision (0168, T11.03) per public record on that
    lead: a correlated subquery, not a second round trip. Both tables are read
    under the caller's RLS, so a house the caller cannot see brings no
    decisions with it. `same_client` is whether the decision was made for the
    client the house is linked to now.
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
        SELECT DISTINCT ON (parcel_id, state) parcel_id, state, id,
               seller_client_id IS NOT NULL AS linked,
               (SELECT jsonb_agg(jsonb_build_object(
                           'id', h.id, 'record_id', h.public_record_id, 'decision', h.decision,
                           'same_client', COALESCE(h.client_id = leads.seller_client_id, false),
                           'decided_at', h.decided_at))
                  FROM property_match_decisions h
                 WHERE h.lead_id = leads.id
                   AND NOT EXISTS (SELECT 1 FROM property_match_decisions n
                                    WHERE n.supersedes_id = h.id)) AS heads
          FROM leads
         WHERE (parcel_id, state) = ANY(
                   SELECT * FROM unnest($1::text[], $2::text[])
               )
         ORDER BY parcel_id, state, updated_at DESC, id ASC
        """,
        parcels,
        states,
    )
    leads: dict[tuple[str, str], dict[str, Any]] = {}
    for r in found:
        heads = r.get("heads")
        if isinstance(heads, str):
            heads = json.loads(heads)
        leads[(str(r["parcel_id"]), str(r["state"]))] = {
            "lead_id": str(r["id"]),
            "linked": r.get("linked") is True,
            "heads_by_record": {
                str(h["record_id"]): h for h in (heads or []) if isinstance(h, dict) and h.get("record_id")
            },
        }
    return leads


# What the search actually matched, as a claim about identity (AC11.04). Only
# an exact key is named as one; every partial or unrecognised match type is a
# possible match. A browse row and a record opened by its id were compared with
# nothing the user typed. `verified` is a separate fact about the house (0168,
# T11.03): True only while a person's newest decision on this lead and THIS
# record is a confirmation for the client the house is linked to now. It never
# changes the status or basis, so a confirmed possible match stays a possible
# match, and a row with no lead is never verified.
_EXACT_IDENTITY = {
    "parcel_exact": "exact_parcel",
    "address_exact": "exact_address",
}
_UNMATCHED_TYPES = {"browse", "record_id"}

# The search's exact branches compare compacted text: every space and
# punctuation mark is removed on both sides. That also removes word
# boundaries, so "15 1/2 MAIN ST" equals "1512 Main St" and parcel "123-45"
# equals "12-345". Such a row stays in the results as a candidate, under its
# own match type, which is a possible match.
_EXACT_MATCH_FIELDS = {
    "parcel_exact": ("parcel_id", "parcel_compact"),
    "address_exact": ("address", "address_compact"),
}


def _record_identity(
    match_type: str,
    lead: Optional[dict] = None,
    *,
    record_id: Any = None,
    can_decide: bool = False,
) -> dict[str, Any]:
    """The search's claim, plus, for a record whose parcel has this tenant's
    lead, the house decision (`confirmation`) and whether the caller may make
    one (`confirmable`: the house is linked to a client and the caller is a
    decider; the route is the gate)."""
    if match_type in _UNMATCHED_TYPES:
        status_name = "not_applicable"
    else:
        status_name = _EXACT_IDENTITY.get(match_type, "possible_match")
    identity: dict[str, Any] = {"status": status_name, "basis": match_type, "verified": False}
    if not lead:
        return identity
    head = (lead.get("heads_by_record") or {}).get(str(record_id))
    confirmation = confirmation_state(head)
    identity["verified"] = confirmation["state"] == "confirmed"
    identity["confirmation"] = confirmation
    identity["confirmable"] = bool(lead.get("linked")) and can_decide
    return identity


def _token_key(value: Any) -> str:
    """Lower-case letter and digit runs in any script, one space apart. Unit
    numbers stay, and "5 大街" keeps its street name (the SQL's compaction
    drops it)."""
    return " ".join(re.findall(r"[^\W_]+", str(value or "").lower()))


def _settle_exact_match(row: dict, query_key: str, *, ambiguous_parcels: frozenset = frozenset()) -> dict:
    """Keep an exact match type only if the words line up as well.

    A blank query key or a blank parcel or address is never an exact match.
    A parcel number written without separators on one side ("19142090130000"
    for "19-14-209-013-0000") carries no boundaries to compare, so its digits
    decide — unless the same digits are split differently on another row of
    the results, which makes it ambiguous ("12345" for "12-345" and "123-45").
    """
    field_and_fallback = _EXACT_MATCH_FIELDS.get(row.get("match_type"))
    if field_and_fallback is None:
        return row
    field, fallback = field_and_fallback
    row_key = _token_key(row.get(field))
    if query_key and row_key == query_key:
        return row
    if (
        field == "parcel_id" and query_key and row_key
        and (" " not in query_key or " " not in row_key)
        and query_key.replace(" ", "") == row_key.replace(" ", "")
        and row_key.replace(" ", "") not in ambiguous_parcels
    ):
        return row
    return {**row, "match_type": fallback}


def _ambiguous_parcels(rows: list[dict]) -> frozenset:
    """Compact parcel numbers that appear split two different ways."""
    forms: dict[str, set] = {}
    for row in rows:
        if row.get("match_type") == "parcel_exact":
            key = _token_key(row.get("parcel_id"))
            forms.setdefault(key.replace(" ", ""), set()).add(key)
    return frozenset(compact for compact, seen in forms.items() if len(seen) > 1)


def _public_record_json(r: dict, lead_ids: Optional[dict] = None, *, can_decide: bool = False) -> dict:
    """Map one allow-listed catalog row to the existing property-card contract.

    `lead_ids` comes from _lead_ids_for_records. Absent (or no match) leaves
    lead_id None, which is correct: the tenant has no lead for this parcel, so
    there is nothing captured against it and the tour stays at exterior tier.
    `can_decide` is whether the caller may record a house decision.
    """
    parcel_key = (str(r.get("parcel_id") or ""), str(r.get("state") or ""))
    lead = (lead_ids or {}).get(parcel_key)
    record = {
        "id": r.get("id"),
        "public_record_id": r.get("public_record_id") or r.get("id"),
        "lead_id": lead["lead_id"] if lead else None,
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
    record["identity"] = _record_identity(
        record["match_type"], lead, record_id=record["public_record_id"], can_decide=can_decide,
    )
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


def _enrichment_failure(source: str, exc: BaseException, timeout_s: int) -> dict[str, Any]:
    """A targeted official-source lookup that did not run, in C6 words.

    The sparse row is still served, so this is not a zero — but its missing
    fields mean "the lookup to fill them failed", not "the source does not
    publish this", and the response says which (audit A02). Only the
    exception type is kept: its text can echo the address or parcel back.
    """
    timed_out = isinstance(exc, (TimeoutError, asyncio.TimeoutError))
    return {
        "source": source,
        "status": "timed_out" if timed_out else "unavailable",
        "reason": (
            f"targeted lookup exceeded {timeout_s} s" if timed_out
            else f"targeted lookup error ({type(exc).__name__})"
        ),
    }


async def _reconcile_sparse_cook_record(
    *,
    query_text: str,
    rows: list[dict[str, Any]],
    ctx: TenantContext,
    failures: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Replace an address-only Chicago violation hit with its assessor PIN.

    A failed lookup returns False, as before, and is appended to `failures`
    when the caller passes one.
    """
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
        if failures is not None:
            failures.append(_enrichment_failure("il_cook_assessor", exc, 30))
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
    failures: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Join an exact sparse catalog row against its official detail sources.

    This remains bounded to one property per request. Batch coverage continues
    through the firehose, while an opened or exact-matched record can be made
    useful immediately without crawling a third-party listing site. A failed
    lookup returns False, as before, and is appended to `failures` when the
    caller passes one.
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
        if failures is not None:
            failures.append(_enrichment_failure("fl_fdor_parcel", exc, 30))
        return False


@records_router.get("", summary="Search the shared source-backed property catalog")
@router.get("/pipeline", include_in_schema=False)
@router.get("/public-records", include_in_schema=False)
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
    words_order_sql = words_text = ""

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
        # LIKE metacharacters in the user's text are literal (review DOS-2).
        escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        contains_arg = _arg(f"%{escaped}%")
        # A query with no letters or digits ("#", "-", "ñ") compacts to "",
        # which equals every blank parcel and address. It gets no exact branch.
        exact_where_sql = exact_rank_sql = exact_type_sql = ""
        if normalized_query:
            exact_arg = _arg(normalized_query)
            parcel_exact_sql = f"regexp_replace(lower(parcel_id), '[^a-z0-9]', '', 'g') = {exact_arg}"
            address_exact_sql = (
                f"regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9]', '', 'g') = {exact_arg}"
            )
            exact_where_sql = f" OR {parcel_exact_sql} OR {address_exact_sql}"
            exact_rank_sql = f"WHEN {parcel_exact_sql} THEN 100 WHEN {address_exact_sql} THEN 98 "
            exact_type_sql = (
                f"WHEN {parcel_exact_sql} THEN 'parcel_exact' "
                f"WHEN {address_exact_sql} THEN 'address_exact' "
            )
            words_text = " ".join(re.findall(r"[a-z0-9]+", query_text.lower()))
        conditions.append(f"(search_document ILIKE {contains_arg}{exact_where_sql})")
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
            f"{exact_rank_sql}"
            f"WHEN address ILIKE {contains_arg} THEN 85 "
            f"WHEN owner_name ILIKE {contains_arg} THEN 70 "
            f"WHEN city ILIKE {contains_arg} THEN 55 "
            f"ELSE 40 END"
        )
        match_type_sql = (
            f"CASE "
            f"{exact_type_sql}"
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
    if words_text:
        # Among equal scores, the row whose words line up with the query comes
        # first, so "1512 MAIN ST" is opened before "15 1/2 MAIN ST" (both
        # compact to the same text; _settle_exact_match labels the second a
        # possible match). Ordering only: not an argument of the count.
        words_arg = _arg(words_text)
        words_order_sql = (
            f"(btrim(regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9]+', ' ', 'g')) = {words_arg} "
            f"OR btrim(regexp_replace(lower(COALESCE(parcel_id, '')), '[^a-z0-9]+', ' ', 'g')) = {words_arg}) DESC, "
        )

    # Fetch one row past the page so has_more never depends on `total` — that
    # keeps pagination correct even when total is an estimate (below).
    data_q = (
        f"SELECT {_PUBLIC_RECORD_SELECT}, {rank_sql} AS match_score, "
        f"{match_type_sql} AS match_type "
        f"FROM public_property_records WHERE {where} "
        f"ORDER BY match_score DESC, {words_order_sql}record_refreshed_at DESC, id ASC "
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
    enrichment_failures: list[dict[str, Any]] = []
    try:
        async with tenant_tx(ctx) as conn:
            total, total_is_estimate = await _count_total(conn)
            rows = [dict(r) for r in await conn.fetch(data_q, *args)]
        if q and q.strip() and await _reconcile_sparse_cook_record(
            query_text=" ".join(q.split()),
            rows=rows,
            ctx=ctx,
            failures=enrichment_failures,
        ):
            async with tenant_tx(ctx) as conn:
                total, total_is_estimate = await _count_total(conn)
                rows = [dict(r) for r in await conn.fetch(data_q, *args)]
        if q and q.strip() and await _reconcile_sparse_public_record(
            rows=rows,
            ctx=ctx,
            exact_match_only=True,
            failures=enrichment_failures,
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

    query_key = _token_key(q) if q and q.strip() else ""
    ambiguous = _ambiguous_parcels(rows)
    can_decide = can_decide_record_match(ctx)
    listings = [
        _public_record_json(
            _settle_exact_match(r, query_key, ambiguous_parcels=ambiguous), lead_ids, can_decide=can_decide,
        )
        for r in rows
    ]
    notice = _public_coverage_json()["notice"]
    if enrichment_failures:
        # The catalog itself was read; an official-source detail lookup for a
        # sparse match was not. Say so instead of letting its blank fields
        # read as "not published" (audit A02).
        notice = (
            "An official-source detail lookup for a matching record did not complete, "
            "so some of its fields are blank because they were not checked. " + notice
        )
    return {
        "listings": listings,
        "total": total,
        "total_is_estimate": total_is_estimate,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_more": has_more,
        "degraded": bool(enrichment_failures),
        "enrichment": enrichment_failures,
        "source": "shared public property catalog",
        "notice": notice,
        "coverage": _public_coverage_json(),
        "accuracy": {
            "ranked_exact_matches": bool(q and q.strip()),
            "deduplicated_by": "source + state + source record id",
            "verification_required": True,
        },
    }


@records_router.get("/{record_id}", summary="Single public property record")
@router.get("/pipeline/{record_id}", include_in_schema=False)
@router.get("/public-records/{record_id}", include_in_schema=False)
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
                f"'record_id' AS match_type "
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
    enrichment_failures: list[dict[str, Any]] = []
    if await _reconcile_sparse_public_record(
        rows=[row_dict],
        ctx=ctx,
        exact_match_only=False,
        failures=enrichment_failures,
    ):
        try:
            async with tenant_tx(ctx) as conn:
                refreshed = await conn.fetchrow(
                    f"SELECT {_PUBLIC_RECORD_SELECT}, 0 AS match_score, "
                    f"'record_id' AS match_type "
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

    detail = _public_record_json(row_dict, lead_ids, can_decide=can_decide_record_match(ctx))
    # Blank fields after a failed official-source lookup are "not checked",
    # not "not published" (audit A02). Empty when nothing failed.
    detail["enrichment"] = enrichment_failures
    return detail
