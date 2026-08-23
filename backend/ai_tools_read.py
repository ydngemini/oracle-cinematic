"""Read-only agent tools that answer from a named source, or refuse.

Every tool in :data:`TOOLS_HANDLED` is classified ``READ_ONLY`` in
``ai_tool_policy.TOOL_RISK`` and none of them writes a row. What they mostly do
is decide whether the data on this deployment can support the sentence the tool
name promises, and say so when it cannot.

Three refusal shapes recur, and they are deliberately distinct:

``ok: false`` with ``code: "DATASET_NOT_LOADED"``
    The backing table is empty on this deployment. Reusing the vocabulary
    ``state_compliance._common`` already established, because "we never loaded
    the data" and "there is no data about your subject" are different answers
    and only one of them is about the property.

``ok: true`` with an empty result and a stated scope
    The query ran and the subject genuinely has nothing. The scope that was
    searched comes back with it, so "no comps" is readable as "none within
    0.5 miles sold in the last 12 months" rather than as a verdict.

``ok: false`` with a reason
    An input could not be resolved — an address that matches no public record,
    a zip that appears nowhere in the dataset. Guessing a state from a zip
    prefix or widening a radius silently would both turn a failed lookup into
    a confident wrong answer.

Every payload that carries a date carries its ``as_of``, and anything older
than :data:`_MARKET_STALE_DAYS` is flagged. ``state_market_stats`` served
migration 0025's 2024-10-01 seed until ``state_market_projection`` began
refreshing it from the scheduled Redfin sync; even now the publisher lag means
a fresh row describes a period ~80 days back, so the date travels with every
figure rather than being assumed.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from record_json import clean as _clean, json_value as _json
from intelligence_engine import (
    IntelligenceInputError,
    calculate_mao as engine_calculate_mao,
    calculate_underwriting,
)
from state_compliance._common import DATASET_NOT_LOADED, dataset_load_hint
from tenancy import TenantContext


TOOLS_HANDLED = frozenset({
    "get_property_tour",
    "search_listings",
    "get_listing_detail",
    "list_comparable_sales",
    "estimate_arv",
    "estimate_rehab",
    "calculate_mao",
    "get_market_trends",
    "get_days_on_market",
    "list_closing_checklist",
    "get_transaction_workflow",
    "get_deal_financial_summary",
    "list_required_disclosures",
    "check_contract_deadlines",
    "list_property_photos",
    "get_agent_performance",
    "list_contract_templates",
    # Ops reads. Allowlisted for every agent, but each checks the caller's role
    # inside its handler — see the section comment there.
    "get_tenant_health",
    "get_job_queue",
    "get_audit_trail",
    "list_integration_status",
    "get_feature_flags",
    "get_billing_status",
    "list_billing_invoices",
    "list_recent_errors",
    "get_database_stats",
    "run_health_check",
})

_EARTH_RADIUS_MILES = 3958.7613
_DEFAULT_RADIUS_MILES = 0.5
_MAX_RADIUS_MILES = 5.0
_COMP_LOOKBACK_MONTHS = 12
_COMP_FALLBACK_MONTHS = 24
_MIN_COMPS_FOR_ARV = 3

# A market median older than this is reported as stale. Thirteen months, so a
# figure that has survived a full seasonal cycle without a refresh is called
# out rather than presented as the current market.
_MARKET_STALE_DAYS = 400

# Share of a ZIP's records that must agree on a state before the lookup is
# trusted. A ZIP has exactly one state, so anything below this is the
# dataset contradicting itself; 80% leaves room for a few stray rows
# without letting a 58% plurality pass as a resolution.
_ZIP_STATE_AGREEMENT = 0.8

# National rules of thumb, and labelled as such everywhere they are returned.
# These are the same bands the chat system prompt already states; there is no
# local labour-rate source in this codebase, so a per-market number would be
# invented. `high=None` on "gut" is not an omission — the documented band is
# "$50-100+/sf", and the "+" has no upper bound to report.
_REHAB_BANDS: dict[str, tuple[int, Optional[int]]] = {
    "light": (15, 25),
    "moderate": (25, 50),
    "major": (50, 100),
    "gut": (100, None),
}
_REHAB_CONTINGENCY_PCT = 15


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

def _ok(tool_name: str, **payload: Any) -> dict:
    return {"ok": True, "action_type": tool_name, **payload}


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _dataset_not_loaded(tool_name: str, table: str) -> dict:
    """The tool-surface twin of ``state_compliance._common.dataset_not_loaded``.

    Same code and the same operator hint, shaped as a tool result instead of an
    HTTPException so the model reads a fact about the deployment rather than a
    fact about the property.
    """
    return {
        "ok": False,
        "action_type": tool_name,
        "code": DATASET_NOT_LOADED,
        "dataset": table,
        "error": (
            f"The {table!r} table is empty on this deployment, so this tool "
            f"cannot answer. This is not a finding that no data exists."
        ),
        "how_to_populate": dataset_load_hint(table),
    }


async def _table_is_empty(conn, table: str) -> bool:
    # Fixed identifiers only, never user input; called only after a query
    # already came back empty, so the populated path never pays for it.
    return not await conn.fetchval(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608


def _staleness(as_of: Any) -> dict:
    """``as_of`` plus how old it is, so a caller cannot miss the age."""
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    if not isinstance(as_of, date):
        return {"as_of": None, "age_days": None, "stale": None,
                "staleness_note": "This row carries no as-of date, so its age is unknown."}
    age = (datetime.now(timezone.utc).date() - as_of).days
    stale = age > _MARKET_STALE_DAYS
    return {
        "as_of": as_of.isoformat(),
        "age_days": age,
        "stale": stale,
        "staleness_note": (
            f"This figure is {age} days old and has not been refreshed; treat it "
            f"as historical, not as the current market."
            if stale else f"This figure is {age} days old."
        ),
    }


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def _number(value: Any, field: str, *, minimum: float, maximum: float,
            default: Optional[float] = None) -> tuple[Optional[float], Optional[dict]]:
    if value in (None, ""):
        return default, None
    try:
        parsed = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None, _err(f"{field} must be a number.")
    if not minimum <= parsed <= maximum:
        return None, _err(f"{field} must be between {minimum} and {maximum}.")
    return parsed, None


def _state_code(value: Any) -> tuple[Optional[str], Optional[dict]]:
    code = str(value or "").strip().upper()
    if not code:
        return None, None
    if len(code) != 2 or not code.isalpha():
        return None, _err("state must be a two-letter code.")
    return code, None


_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_TRAILING_STATE_RE = re.compile(r"\b([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?\s*$")


def _zip_code(value: Any) -> tuple[Optional[str], Optional[dict]]:
    match = _ZIP_RE.search(str(value or "").strip())
    if not match:
        return None, _err("zip_code must be a five-digit US ZIP code.")
    return match.group(1), None


def _state_in_address(address: str) -> Optional[str]:
    """The state code from an address string, or None.

    Only used to pick the *indexed* lookup path — an address without one still
    resolves, through the trigram index, so this is an optimisation and never a
    correctness gate.
    """
    match = _TRAILING_STATE_RE.search(address.strip().upper())
    return match.group(1) if match else None


def _search_term(value: Any, field: str) -> tuple[Optional[str], Optional[dict]]:
    term = str(value or "").strip()
    if not 3 <= len(term) <= 200:
        return None, _err(f"{field} must be 3-200 characters.")
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%", None


async def _resolve_deal(conn, ctx: TenantContext, tool_input: dict, field: str = "deal_id"):
    import uuid as _uuid
    try:
        deal_id = str(_uuid.UUID(str(tool_input.get(field) or "").strip()))
    except (ValueError, AttributeError):
        return None, _err(f"{field} must be a UUID.")
    row = await conn.fetchrow(
        "SELECT id,address,state,sqft,asking_price,underwriting,dossier_status "
        "FROM leads WHERE id=$1::uuid AND tenant_id=$2::uuid",
        deal_id, ctx.tenant_id,
    )
    if not row:
        return None, _err("That deal is not in this workspace.")
    return row, None


# ---------------------------------------------------------------------------
# Public-record resolution
# ---------------------------------------------------------------------------

_SUBJECT_COLUMNS = """
    id,address,city,county,state,zip_code,latitude,longitude,
    building_area_sqft,lot_area_sqft,bedrooms,bathrooms,year_built,
    zoning_district,land_use,last_sale_price,reported_record_date,
    source_name,source_key,record_refreshed_at
"""


async def _resolve_subject(conn, address: str) -> Optional[dict]:
    """Best public record for a free-text address, using an index either way.

    Two paths, both indexed: an exact normalised-address match when the string
    carries a state (``idx_public_property_address`` is keyed on state first,
    so without one it is unusable), and a trigram containment match otherwise.
    An unindexed match here would seq-scan ~7M rows on every call.
    """
    cleaned = address.strip()
    state = _state_in_address(cleaned)
    if state:
        row = await conn.fetchrow(
            f"""SELECT {_SUBJECT_COLUMNS} FROM public_property_records
                 WHERE state=$1
                   AND regexp_replace(lower(coalesce(address,'')),'[^a-z0-9]','','g')
                     = regexp_replace(lower($2),'[^a-z0-9]','','g')
                 ORDER BY record_refreshed_at DESC NULLS LAST
                 LIMIT 1""",
            state, re.sub(r",?\s*[A-Z]{2}\b.*$", "", cleaned, flags=re.IGNORECASE) or cleaned,
        )
        if row:
            return dict(row)
    row = await conn.fetchrow(
        f"""SELECT {_SUBJECT_COLUMNS} FROM public_property_records
             WHERE search_document ILIKE $1
             ORDER BY record_refreshed_at DESC NULLS LAST
             LIMIT 1""",
        f"%{cleaned}%",
    )
    return dict(row) if row else None


async def _comps_near(conn, subject: dict, *, radius_miles: float, limit: int,
                      months: int) -> list[dict]:
    """Sold public records inside a real radius of the subject.

    The bounding box is what makes ``idx_public_property_comps`` usable; the
    haversine then trims the box corners to a true circle. Its partial
    predicates are repeated verbatim in the WHERE clause so the planner matches
    the index.
    """
    lat, lon = float(subject["latitude"]), float(subject["longitude"])
    lat_span = radius_miles / 69.0
    # Longitude degrees shrink with latitude; the floor keeps the box finite
    # near the poles rather than dividing by ~0.
    lon_span = radius_miles / max(69.0 * abs(_cos_deg(lat)), 1e-6)
    since = date.today() - timedelta(days=int(months * 30.44))
    rows = await conn.fetch(
        f"""
        SELECT * FROM (
            SELECT {_SUBJECT_COLUMNS},
                   {_EARTH_RADIUS_MILES} * 2 * asin(sqrt(
                       power(sin(radians(latitude - $1) / 2), 2)
                     + cos(radians($1)) * cos(radians(latitude))
                       * power(sin(radians(longitude - $2) / 2), 2)
                   )) AS distance_miles
              FROM public_property_records
             WHERE state = $3
               AND latitude IS NOT NULL
               AND longitude IS NOT NULL
               AND last_sale_price IS NOT NULL
               AND latitude BETWEEN $1 - $4 AND $1 + $4
               AND longitude BETWEEN $2 - $5 AND $2 + $5
               AND (reported_record_date IS NULL OR reported_record_date >= $6)
               AND id <> $7
        ) candidates
         WHERE distance_miles <= $8
         ORDER BY distance_miles ASC
         LIMIT $9
        """,
        lat, lon, subject["state"], lat_span, lon_span, since,
        subject["id"], radius_miles, limit,
    )
    return [dict(row) for row in rows]


async def _comps_in_zip(conn, subject: dict, *, limit: int, months: int) -> list[dict]:
    """Sold records in the same ZIP, for the 15x of the dataset with no coordinate.

    Measured: 797,235 records carry a sale price, but only 41,292 also carry a
    latitude — so a radius search can see 5% of the comparable evidence that
    exists. Another 610,655 have a sale price and a ZIP and nothing else.

    A ZIP is a coarser neighbourhood than half a mile and this never pretends
    otherwise: `distance_miles` comes back null and the basis says `same_zip`,
    so a caller can tell a measured distance from a shared postal code.
    """
    since = date.today() - timedelta(days=int(months * 30.44))
    rows = await conn.fetch(
        f"""SELECT {_SUBJECT_COLUMNS}, NULL::float8 AS distance_miles
              FROM public_property_records
             WHERE zip_code = $1
               AND state = $2
               AND last_sale_price IS NOT NULL
               AND (reported_record_date IS NULL OR reported_record_date >= $3)
               AND id <> $4
             ORDER BY reported_record_date DESC NULLS LAST
             LIMIT $5""",
        subject["zip_code"], subject["state"], since, subject["id"], limit,
    )
    return [dict(row) for row in rows]


def _cos_deg(degrees: float) -> float:
    import math
    return math.cos(math.radians(degrees))


def _comp_payload(row: dict) -> dict:
    sqft = row.get("building_area_sqft")
    price = row.get("last_sale_price")
    ppsf = None
    if sqft and price and float(sqft) > 0:
        ppsf = round(float(price) / float(sqft), 2)
    return _clean({
        "record_id": str(row["id"]),
        "address": row.get("address"),
        "city": row.get("city"),
        "zip_code": row.get("zip_code"),
        # None for a same-ZIP comparable: there is no measured distance, and a
        # zero would read as "next door".
        "distance_miles": (
            round(float(row["distance_miles"]), 3)
            if row.get("distance_miles") is not None else None
        ),
        "sale_price": float(price) if price is not None else None,
        "sale_date": row.get("reported_record_date"),
        "sqft": float(sqft) if sqft else None,
        "price_per_sqft": ppsf,
        "bedrooms": row.get("bedrooms"),
        "bathrooms": row.get("bathrooms"),
        "year_built": row.get("year_built"),
        "source": row.get("source_name") or row.get("source_key"),
    })


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

async def _search_listings(conn, ctx: TenantContext, tool_input: dict) -> dict:
    pattern, error = _search_term(tool_input.get("query"), "query")
    if error:
        return error
    state, error = _state_code(tool_input.get("state"))
    if error:
        return error
    min_price, error = _number(tool_input.get("min_price"), "min_price",
                               minimum=0, maximum=1e10)
    if error:
        return error
    max_price, error = _number(tool_input.get("max_price"), "max_price",
                               minimum=0, maximum=1e10)
    if error:
        return error
    if min_price is not None and max_price is not None and min_price > max_price:
        return _err("min_price cannot exceed max_price.")

    owned = await conn.fetch(
        """SELECT l.id,l.address,l.price,l.status,l.is_shared_mls,l.updated_at,
                  ld.state,ld.beds,ld.baths,ld.sqft,ld.id AS lead_id
             FROM listings l
             LEFT JOIN leads ld ON ld.id=l.lead_id AND ld.tenant_id=l.tenant_id
            WHERE l.tenant_id=$1::uuid
              AND l.address ILIKE $2
              AND ($3::text IS NULL OR ld.state=$3)
              AND ($4::numeric IS NULL OR l.price >= $4)
              AND ($5::numeric IS NULL OR l.price <= $5)
            ORDER BY l.updated_at DESC
            LIMIT 25""",
        ctx.tenant_id, pattern, state, min_price, max_price,
    )
    mls = await conn.fetch(
        """SELECT mls_number,address,city,state_code,zip_code,list_price,status,
                  beds,baths_full,baths_half,sqft,year_built,days_on_market,list_date
             FROM oracle_mls_listings
            WHERE (address ILIKE $1 OR city ILIKE $1 OR zip_code ILIKE $1)
              AND ($2::text IS NULL OR state_code=$2)
              AND ($3::numeric IS NULL OR list_price >= $3)
              AND ($4::numeric IS NULL OR list_price <= $4)
            ORDER BY last_updated DESC NULLS LAST
            LIMIT 25""",
        pattern, state, min_price, max_price,
    )

    # An empty MLS half is reported, never hidden. Returning only the owned
    # listings without saying the other source held nothing would read as
    # "these are all the listings that match".
    mls_note = None
    if not mls and await _table_is_empty(conn, "oracle_mls_listings"):
        mls_note = (
            "The MLS listing cache is empty on this deployment — no rows at all, "
            "not a miss on this query. Live listing access goes through the "
            "ORACLE_RESO_* feeds; results below are owned listings only."
        )

    return _ok(
        "search_listings",
        owned_listings=[_clean(dict(row)) for row in owned],
        mls_listings=[_clean(dict(row)) for row in mls],
        sources_searched=[
            {"source": "listings", "scope": "this workspace", "matched": len(owned)},
            {"source": "oracle_mls_listings", "scope": "shared MLS cache",
             "matched": len(mls), "note": mls_note},
        ],
        filters={"query": str(tool_input.get("query")).strip(), "state": state,
                 "min_price": min_price, "max_price": max_price},
    )


async def _get_listing_detail(conn, ctx: TenantContext, tool_input: dict) -> dict:
    import uuid as _uuid
    try:
        listing_id = str(_uuid.UUID(str(tool_input.get("listing_id") or "").strip()))
    except (ValueError, AttributeError):
        return _err("listing_id must be a UUID.")
    listing = await conn.fetchrow(
        """SELECT l.id,l.address,l.price,l.status,l.is_shared_mls,l.created_at,
                  l.updated_at,l.lead_id,
                  ld.parcel_id,ld.state,ld.beds,ld.baths,ld.sqft,ld.asking_price,
                  ld.motivation_score,ld.underwriting,ld.dossier_status,
                  c.id AS seller_client_id,c.full_name AS seller_name
             FROM listings l
             LEFT JOIN leads ld ON ld.id=l.lead_id AND ld.tenant_id=l.tenant_id
             LEFT JOIN clients c ON c.id=ld.seller_client_id AND c.tenant_id=l.tenant_id
            WHERE l.id=$1::uuid AND l.tenant_id=$2::uuid""",
        listing_id, ctx.tenant_id,
    )
    if not listing:
        return _err("That listing is not in this workspace.")

    record = dict(listing)
    counts = await conn.fetchrow(
        """SELECT (SELECT count(*) FROM property_media m
                    WHERE m.tenant_id=$1::uuid AND m.listing_id=$2::uuid)::int AS media_count,
                  (SELECT count(*) FROM showings s
                    WHERE s.tenant_id=$1::uuid AND s.listing_id=$2::uuid)::int AS showing_count""",
        ctx.tenant_id, listing_id,
    )

    # Beds/baths/sqft/zoning live on the public record, not on the listing row.
    # Absent a match, they come back null with the reason attached rather than
    # silently missing from the payload.
    public_record = None
    if record.get("address"):
        public_record = await _resolve_subject(conn, str(record["address"]))

    unavailable: list[str] = []
    for field in ("beds", "baths", "sqft"):
        if record.get(field) in (None, 0):
            unavailable.append(field)
    if not public_record:
        unavailable.extend(["zoning_district", "land_use", "lot_area_sqft", "year_built"])

    return _ok(
        "get_listing_detail",
        listing=_clean(record),
        media_count=counts["media_count"],
        showing_count=counts["showing_count"],
        public_record=_clean(public_record) if public_record else None,
        fields_unavailable=sorted(set(unavailable)),
        fields_unavailable_reason=(
            "The listing row stores address, price and status only. Physical "
            "characteristics come from the linked lead or a matched public "
            "record; the fields listed above have neither."
            if unavailable else None
        ),
    )


# ---------------------------------------------------------------------------
# Comparables and valuation
# ---------------------------------------------------------------------------

async def _comp_coverage(conn, state: str) -> dict:
    """How many records in this state could ever be a comp.

    "No comps within half a mile" and "this state has 312 geocoded sales in the
    dataset" are different answers, and only the first is about the market.
    """
    total = await conn.fetchval(
        """SELECT count(*)::int FROM public_property_records
            WHERE state=$1 AND latitude IS NOT NULL AND longitude IS NOT NULL
              AND last_sale_price IS NOT NULL""",
        state,
    )
    return {
        "state": state,
        "records_with_coordinates_and_sale_price": int(total or 0),
        "note": (
            "A comparable needs both a coordinate and a recorded sale price. "
            "Most rows in this dataset carry neither, so an empty result may "
            "reflect dataset coverage rather than an absence of sales."
        ),
    }


async def _gather_comps(conn, tool_input: dict, tool_name: str):
    """Shared by list_comparable_sales and estimate_arv."""
    address = str(tool_input.get("address") or "").strip()
    if len(address) < 5:
        return None, _err("address must be at least 5 characters.")
    radius, error = _number(tool_input.get("radius_miles"), "radius_miles",
                            minimum=0.05, maximum=_MAX_RADIUS_MILES,
                            default=_DEFAULT_RADIUS_MILES)
    if error:
        return None, error
    limit, error = _number(tool_input.get("limit"), "limit", minimum=1, maximum=50,
                           default=10)
    if error:
        return None, error

    subject = await _resolve_subject(conn, address)
    if not subject:
        if await _table_is_empty(conn, "public_property_records"):
            return None, _dataset_not_loaded(tool_name, "public_property_records")
        return None, _err(
            f"No public record matches {address!r}. A radius search needs a "
            f"resolved coordinate; including the city and two-letter state "
            f"improves the match."
        )
    if (subject.get("latitude") is None or subject.get("longitude") is None) \
            and not subject.get("zip_code"):
        return None, _err(
            f"The public record for {address!r} carries neither a coordinate nor "
            f"a ZIP code, so there is no neighbourhood to compare within."
        )

    months = _COMP_LOOKBACK_MONTHS
    basis = "radius"
    comps: list[dict] = []
    has_coordinate = subject.get("latitude") is not None and subject.get("longitude") is not None
    if has_coordinate:
        comps = await _comps_near(conn, subject, radius_miles=float(radius),
                                  limit=int(limit), months=months)
        if not comps:
            months = _COMP_FALLBACK_MONTHS
            comps = await _comps_near(conn, subject, radius_miles=float(radius),
                                      limit=int(limit), months=months)

    # Widening from a radius to a ZIP is a real loss of precision, so it happens
    # only after the precise search has genuinely found nothing, and the result
    # says which one answered.
    if not comps and subject.get("zip_code"):
        basis = "same_zip"
        months = _COMP_LOOKBACK_MONTHS
        comps = await _comps_in_zip(conn, subject, limit=int(limit), months=months)
        if not comps:
            months = _COMP_FALLBACK_MONTHS
            comps = await _comps_in_zip(conn, subject, limit=int(limit), months=months)
    attempted = (["radius"] if has_coordinate else []) + (
        ["same_zip"] if subject.get("zip_code") else [])
    if not comps:
        # Nothing answered, so no tier gets the credit. Reporting the widest
        # one attempted as the "basis" would imply it produced the empty set on
        # its own terms.
        basis = None
        months = _COMP_LOOKBACK_MONTHS
    return {"subject": subject, "comps": comps, "radius_miles": float(radius),
            "months": months, "limit": int(limit), "basis": basis,
            "tiers_attempted": attempted,
            "subject_has_coordinate": has_coordinate}, None


async def _list_comparable_sales(conn, ctx: TenantContext, tool_input: dict) -> dict:
    gathered, error = await _gather_comps(conn, tool_input, "list_comparable_sales")
    if error:
        return error
    subject, comps = gathered["subject"], gathered["comps"]
    payload = [_comp_payload(row) for row in comps]
    result = _ok(
        "list_comparable_sales",
        subject=_clean({k: subject[k] for k in (
            "address", "city", "county", "state", "zip_code",
            "building_area_sqft", "bedrooms", "bathrooms", "year_built")}),
        comparables=payload,
        count=len(payload),
        scope={
            "basis": gathered["basis"],
            "tiers_attempted": gathered["tiers_attempted"],
            "basis_note": (
                f"Within {gathered['radius_miles']} miles of the subject."
                if gathered["basis"] == "radius" else
                "Same ZIP code, not a measured distance: no coordinate was "
                "available for a radius search. A ZIP is a coarser "
                "neighbourhood than half a mile."
                if gathered["basis"] == "same_zip" else
                "Nothing was found by any search this tool can run."
            ),
            "radius_miles": (
                gathered["radius_miles"] if gathered["basis"] == "radius" else None
            ),
            "sold_within_months": gathered["months"],
            "widened_from_months": (
                _COMP_LOOKBACK_MONTHS
                if gathered["months"] == _COMP_FALLBACK_MONTHS else None
            ),
            "subject_has_coordinate": gathered["subject_has_coordinate"],
            "source": "public_property_records",
        },
    )
    if not payload:
        result["coverage"] = await _comp_coverage(conn, subject["state"])
    return result


async def _estimate_arv(conn, ctx: TenantContext, tool_input: dict) -> dict:
    gathered, error = await _gather_comps(conn, tool_input, "estimate_arv")
    if error:
        return error
    subject, comps = gathered["subject"], gathered["comps"]

    sqft, error = _number(tool_input.get("sqft"), "sqft", minimum=1, maximum=1_000_000)
    if error:
        return error
    if sqft is None:
        sqft = subject.get("building_area_sqft")
    if not sqft:
        return _err(
            "ARV is derived from price per square foot, and neither the request "
            "nor the public record supplies a square footage for this property. "
            "Pass sqft to proceed."
        )

    # The engine refuses a comparable without a positive sqft rather than
    # guessing one, so they are dropped here and counted — a median taken over
    # a silently smaller set is the error this reports instead of making.
    usable = [c for c in comps if c.get("building_area_sqft")
              and float(c["building_area_sqft"]) > 0
              and c.get("last_sale_price")]
    dropped = len(comps) - len(usable)
    if not usable:
        result = _ok(
            "estimate_arv", arv=None,
            error_detail="No comparable in range carries both a sale price and a "
                         "square footage, so a price-per-square-foot median cannot "
                         "be taken. No ARV is returned.",
            comparables_found=len(comps), comparables_usable=0,
            scope={"basis": gathered["basis"],
                   "radius_miles": (gathered["radius_miles"]
                                    if gathered["basis"] == "radius" else None),
                   "sold_within_months": gathered["months"]},
        )
        result["coverage"] = await _comp_coverage(conn, subject["state"])
        return result

    try:
        underwriting = calculate_underwriting(
            subject_sqft=sqft,
            comparables=[{
                "record_id": str(c["id"]), "address": c.get("address"),
                "sale_price": c["last_sale_price"], "sqft": c["building_area_sqft"],
                "sale_date": c.get("reported_record_date"),
                "source": c.get("source_name") or c.get("source_key"),
            } for c in usable],
            rehab_items=[],
        )
    except IntelligenceInputError as exc:
        return _err(f"ARV could not be calculated: {exc}")

    ppsf = sorted(
        float(c["last_sale_price"]) / float(c["building_area_sqft"]) for c in usable
    )
    low, high = ppsf[0] * float(sqft), ppsf[-1] * float(sqft)
    spread_pct = round(((ppsf[-1] - ppsf[0]) / ppsf[0]) * 100, 1) if ppsf[0] else None

    return _ok(
        "estimate_arv",
        arv=underwriting["arv"],
        arv_range={"low": round(low, 2), "high": round(high, 2),
                   "basis": "lowest and highest comparable price per square foot "
                            "applied to the subject square footage"},
        model_version=underwriting["model_version"],
        subject={"address": subject.get("address"), "sqft": float(sqft),
                 "sqft_source": "request" if tool_input.get("sqft") else
                                "public_property_records.building_area_sqft"},
        comparables=[_comp_payload(c) for c in usable],
        comparables_found=len(comps),
        comparables_usable=len(usable),
        comparables_dropped_for_missing_sqft=dropped,
        # Deliberately not a confidence score: nothing here has been calibrated
        # against realised sale prices, so a number would imply a validation
        # that never ran. The inputs a reader would need are returned instead.
        confidence=None,
        confidence_basis={
            "scored": False,
            "reason": "No calibration set exists, so a confidence percentage "
                      "would be invented. Judge the estimate from the spread "
                      "and the comparable count.",
            "comparable_count": len(usable),
            "price_per_sqft_spread_pct": spread_pct,
            "below_minimum_comparables": len(usable) < _MIN_COMPS_FOR_ARV,
        },
        trace=underwriting["trace"],
        scope={"basis": gathered["basis"],
               "radius_miles": (gathered["radius_miles"]
                                if gathered["basis"] == "radius" else None),
               "sold_within_months": gathered["months"],
               "source": "public_property_records"},
    )


async def _estimate_rehab(conn, ctx: TenantContext, tool_input: dict) -> dict:
    condition = str(tool_input.get("condition") or "").strip().lower()
    if condition not in _REHAB_BANDS:
        return _err(
            "condition must be one of: " + ", ".join(sorted(_REHAB_BANDS)) + "."
        )
    address = str(tool_input.get("address") or "").strip()
    if len(address) < 5:
        return _err("address must be at least 5 characters.")

    subject = await _resolve_subject(conn, address)
    sqft = subject.get("building_area_sqft") if subject else None
    if not sqft:
        return _err(
            f"A rehab estimate is square footage times a cost band, and no "
            f"public record with a building area matches {address!r}. Without "
            f"a square footage there is nothing to multiply."
        )

    year_built, error = _number(tool_input.get("year_built"), "year_built",
                                minimum=1600, maximum=2100)
    if error:
        return error
    if year_built is None:
        year_built = subject.get("year_built")

    low_psf, high_psf = _REHAB_BANDS[condition]
    sqft = float(sqft)
    low = low_psf * sqft
    high = high_psf * sqft if high_psf is not None else None
    contingency = _REHAB_CONTINGENCY_PCT / 100.0

    risk_flags: list[str] = []
    if year_built and float(year_built) < 1978:
        risk_flags.append(
            "Built before 1978: federal lead-based paint disclosure applies and "
            "abatement is not included in these bands."
        )
    if year_built and float(year_built) < 1950:
        risk_flags.append(
            "Built before 1950: knob-and-tube wiring, galvanised supply and "
            "asbestos-containing materials are common and priced separately."
        )

    return _ok(
        "estimate_rehab",
        cost_low=round(low * (1 + contingency), 2),
        cost_high=round(high * (1 + contingency), 2) if high is not None else None,
        cost_high_unbounded=high is None,
        condition=condition,
        band_per_sqft={"low": low_psf, "high": high_psf},
        sqft=sqft,
        year_built=int(year_built) if year_built else None,
        contingency_pct=_REHAB_CONTINGENCY_PCT,
        risk_flags=risk_flags,
        # The tool catalog once advertised "local labor rates". There is no
        # labour-rate source in this codebase, and a per-market multiplier would
        # be an invention dressed as data.
        basis=(
            f"National rule-of-thumb band of ${low_psf}"
            + (f"-${high_psf}" if high_psf is not None else "+")
            + f"/sqft for a {condition} scope, times {sqft:,.0f} sqft, plus a "
              f"{_REHAB_CONTINGENCY_PCT}% contingency. These are national bands: "
              f"no local labour or material rates were used, because this "
              f"deployment has no source for them."
        ),
        method="national_band_times_area",
        subject={"address": subject.get("address"), "city": subject.get("city"),
                 "state": subject.get("state"),
                 "sqft_source": "public_property_records.building_area_sqft"},
    )


async def _calculate_mao(conn, ctx: TenantContext, tool_input: dict) -> dict:
    arv, error = _number(tool_input.get("arv"), "arv", minimum=0, maximum=1e10)
    if error:
        return error
    rehab, error = _number(tool_input.get("rehab"), "rehab", minimum=0, maximum=1e10)
    if error:
        return error
    holding, error = _number(tool_input.get("holding_costs"), "holding_costs",
                             minimum=0, maximum=1e10, default=0.0)
    if error:
        return error
    if arv is None or rehab is None:
        return _err("Both arv and rehab are required.")
    try:
        result = engine_calculate_mao(arv=arv, rehab=rehab, holding_costs=holding)
    except IntelligenceInputError as exc:
        return _err(f"MAO could not be calculated: {exc}")
    return _ok("calculate_mao", **result)


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

async def _resolve_zip(conn, zip_code: str) -> dict:
    """Zip to state/county from the data, never from a prefix table.

    A ZIP belongs to exactly one state, so any disagreement inside the dataset
    is a defect rather than a genuine ambiguity — and there are plenty: 12,322
    of 293,564 ZIPs in this deployment appear under more than one state code,
    one of them under eleven. ZIP 19901 is Dover, Delaware, and the TN harvester
    has written it onto records in Dover, Tennessee; a modal vote returns
    Tennessee with a 58% plurality and no sign that anything went wrong.

    So the modal state has to *dominate*, and the distribution comes back either
    way. Reporting Tennessee market figures for a Delaware ZIP is precisely the
    confident wrong answer the rest of this module exists to avoid.

    Both queries ride ``idx_public_property_zip_state`` (0076). Before that
    index a ZIP with no matching rows scanned all ~7M — 13.5 s measured, and an
    agent can type any five digits.
    """
    rows = await conn.fetch(
        """SELECT state, count(*)::int AS n FROM public_property_records
            WHERE zip_code=$1 AND state IS NOT NULL
            GROUP BY state ORDER BY n DESC LIMIT 6""",
        zip_code,
    )
    if not rows:
        return {"resolved": False, "reason": "absent"}

    distribution = [{"state": row["state"], "record_count": int(row["n"])}
                    for row in rows]
    total = sum(item["record_count"] for item in distribution)
    dominant = distribution[0]
    share = dominant["record_count"] / total if total else 0.0
    if share < _ZIP_STATE_AGREEMENT:
        return {"resolved": False, "reason": "conflicting_states",
                "state_distribution": distribution,
                "dominant_share": round(share, 3)}

    county = await conn.fetchval(
        """SELECT county FROM public_property_records
            WHERE zip_code=$1 AND state=$2 AND county IS NOT NULL
            GROUP BY county ORDER BY count(*) DESC LIMIT 1""",
        zip_code, dominant["state"],
    )
    return {"resolved": True, "zip_code": zip_code, "state": dominant["state"],
            "county": county, "state_agreement": round(share, 3),
            "state_distribution": distribution if len(distribution) > 1 else None}


def _unresolved_zip(tool_name: str, zip_code: str, resolved: dict) -> dict:
    if resolved.get("reason") == "conflicting_states":
        listed = ", ".join(
            f"{item['state']} ({item['record_count']} records)"
            for item in resolved["state_distribution"]
        )
        return {
            "ok": False, "action_type": tool_name,
            "error": (
                f"ZIP {zip_code} appears under several state codes in the public "
                f"property dataset — {listed} — with only "
                f"{resolved['dominant_share']:.0%} agreement. A ZIP belongs to one "
                f"state, so this is a defect in the harvested data, not a real "
                f"ambiguity. No market figures are returned, because the most "
                f"common state here may not be the right one."
            ),
            "state_distribution": resolved["state_distribution"],
        }
    return {
        "ok": False, "action_type": tool_name,
        "error": (
            f"ZIP {zip_code} appears nowhere in the public property dataset, so "
            f"it cannot be resolved to a state or county. No market figures are "
            f"returned rather than guessing the geography from the prefix."
        ),
    }


async def _market_context(conn, resolved: dict) -> dict:
    """County and state aggregates, each labelled with the geography it covers."""
    county_row = None
    if resolved.get("county"):
        county_row = await conn.fetchrow(
            """SELECT county_name,state_code,median_sale_price,median_list_price,
                      median_days_on_market,property_tax_rate_pct,median_annual_tax,
                      as_of_date
                 FROM county_market_stats
                WHERE state_code=$1 AND lower(county_name)=lower($2)
                LIMIT 1""",
            resolved["state"], resolved["county"],
        )
    state_row = await conn.fetchrow(
        """SELECT state_code,state_name,median_sale_price,median_list_price,
                  median_days_on_market,months_of_supply,yoy_price_change_pct,
                  active_listings,closed_sales_last_30d,list_to_sale_ratio,
                  avg_price_per_sqft,as_of_date
             FROM state_market_stats WHERE state_code=$1""",
        resolved["state"],
    )
    county_note = None
    if county_row is None and resolved.get("county"):
        county_note = (
            "No county aggregate is loaded for this county."
            if not await _table_is_empty(conn, "county_market_stats")
            else dataset_load_hint("county_market_stats")
        )
    return {
        "county": ({"geography": f"{resolved['county']} County, {resolved['state']}",
                    **_clean(dict(county_row)), **_staleness(county_row["as_of_date"])}
                   if county_row else None),
        "county_unavailable_reason": county_note,
        "state": ({"geography": resolved["state"], **_clean(dict(state_row)),
                   **_staleness(state_row["as_of_date"])} if state_row else None),
    }


async def _get_market_trends(conn, ctx: TenantContext, tool_input: dict) -> dict:
    zip_code, error = _zip_code(tool_input.get("zip_code"))
    if error:
        return error
    resolved = await _resolve_zip(conn, zip_code)
    if not resolved["resolved"]:
        if resolved.get("reason") == "absent" and await _table_is_empty(
                conn, "public_property_records"):
            return _dataset_not_loaded("get_market_trends", "public_property_records")
        return _unresolved_zip("get_market_trends", zip_code, resolved)
    context = await _market_context(conn, resolved)
    metrics = await conn.fetch(
        """SELECT metric_key,geography_name,geography_type,period_end,value,unit,
                  source_url,dataset_updated_at
             FROM public_market_metrics
            WHERE state_code=$1
            ORDER BY period_end DESC NULLS LAST, metric_key
            LIMIT 25""",
        resolved["state"],
    )
    return _ok(
        "get_market_trends",
        requested_zip=zip_code,
        resolved_geography=resolved,
        # The aggregates below are county- and state-level. Nothing in this
        # deployment holds a zip-level median, and relabelling a state figure
        # as the caller's zip is the exact misstatement this key prevents.
        granularity_note=(
            f"No zip-level aggregate exists on this deployment. ZIP {zip_code} was "
            f"resolved to {resolved.get('county') or 'an unknown county'}, "
            f"{resolved['state']}, and the figures below describe those larger "
            f"geographies — not ZIP {zip_code} specifically."
        ),
        **context,
        published_metrics=[_clean(dict(row)) for row in metrics],
        forecast=None,
        forecast_unavailable_reason=(
            "A forecast needs a time series. public_market_metrics holds "
            "point-in-time observations and no per-zip history, so no trend "
            "line is fitted here."
        ),
    )


async def _get_days_on_market(conn, ctx: TenantContext, tool_input: dict) -> dict:
    zip_code, error = _zip_code(tool_input.get("zip_code"))
    if error:
        return error
    resolved = await _resolve_zip(conn, zip_code)
    if not resolved["resolved"]:
        return _unresolved_zip("get_days_on_market", zip_code, resolved)
    # This is the one genuinely zip-level source: MLS rows carry both a zip and
    # an observed days_on_market.
    observed = await conn.fetchrow(
        """SELECT count(*)::int AS n,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY days_on_market) AS median_dom,
                  avg(days_on_market)::numeric(10,1) AS mean_dom,
                  count(*) FILTER (WHERE status='Active')::int AS active,
                  count(*) FILTER (WHERE close_date IS NOT NULL)::int AS closed
             FROM oracle_mls_listings
            WHERE zip_code=$1 AND days_on_market IS NOT NULL""",
        zip_code,
    )
    zip_level = None
    zip_note = None
    if observed and observed["n"]:
        zip_level = {
            "geography": f"ZIP {zip_code}",
            "listing_count": observed["n"],
            "median_days_on_market": float(observed["median_dom"]) if observed["median_dom"] is not None else None,
            "mean_days_on_market": float(observed["mean_dom"]) if observed["mean_dom"] is not None else None,
            "active_listings": observed["active"],
            "closed_listings": observed["closed"],
            "source": "oracle_mls_listings",
        }
    else:
        zip_note = (
            dataset_load_hint("mls_boards")
            if await _table_is_empty(conn, "oracle_mls_listings")
            else f"The MLS cache holds no listing in ZIP {zip_code} with a "
                 f"recorded days-on-market."
        )
    context = await _market_context(conn, resolved)
    return _ok(
        "get_days_on_market",
        requested_zip=zip_code,
        resolved_geography=resolved,
        zip_level=zip_level,
        zip_level_unavailable_reason=zip_note,
        county=context["county"],
        county_unavailable_reason=context["county_unavailable_reason"],
        state=context["state"],
        by_property_type=None,
        by_property_type_unavailable_reason=(
            "Days on market is not stored broken down by property type on this "
            "deployment; only the aggregate above is available."
        ),
    )


# ---------------------------------------------------------------------------
# Transactions and compliance
# ---------------------------------------------------------------------------

async def _transactions_for_deal(conn, ctx: TenantContext, deal_id: str) -> list[dict]:
    rows = await conn.fetch(
        """SELECT id,status,state_code,property_address,purchase_price,earnest_money,
                  financing_amount,offer_deadline,inspection_deadline,
                  financing_deadline,closing_deadline,accepted_offer_id,closed_at,
                  created_at,updated_at
             FROM transactions
            WHERE lead_id=$1::uuid AND tenant_id=$2::uuid
            ORDER BY updated_at DESC""",
        deal_id, ctx.tenant_id,
    )
    return [dict(row) for row in rows]


def _no_transaction(tool_name: str, deal: dict) -> dict:
    return _ok(
        tool_name,
        deal={"id": str(deal["id"]), "address": deal.get("address"),
              "stage": deal.get("dossier_status")},
        transactions=[],
        note=(
            "This deal has no transaction record, so there is nothing to report. "
            "A transaction is created when an offer is written; the deal is still "
            "at pipeline stage "
            f"{deal.get('dossier_status') or 'unknown'}."
        ),
    )


async def _list_closing_checklist(conn, ctx: TenantContext, tool_input: dict) -> dict:
    deal, error = await _resolve_deal(conn, ctx, tool_input)
    if error:
        return error
    transactions = await _transactions_for_deal(conn, ctx, str(deal["id"]))
    if not transactions:
        return _no_transaction("list_closing_checklist", dict(deal))
    ids = [t["id"] for t in transactions]
    items = await conn.fetch(
        """SELECT transaction_id,form_name,status,due_date,delivered_at,signed_at,
                  signed_by,notes,updated_at
             FROM compliance_checklist_items
            WHERE tenant_id=$1::uuid AND transaction_id = ANY($2::uuid[])
            ORDER BY due_date ASC NULLS LAST, form_name""",
        ctx.tenant_id, ids,
    )
    milestones = await conn.fetch(
        """SELECT transaction_id,milestone_type,title,status,due_at,completed_at,
                  assigned_to,updated_at
             FROM transaction_milestones
            WHERE tenant_id=$1::uuid AND transaction_id = ANY($2::uuid[])
            ORDER BY due_at ASC NULLS LAST, milestone_type""",
        ctx.tenant_id, ids,
    )
    return _ok(
        "list_closing_checklist",
        deal={"id": str(deal["id"]), "address": deal.get("address")},
        transactions=[_clean({"id": str(t["id"]), "status": t["status"],
                              "property_address": t["property_address"]})
                      for t in transactions],
        compliance_items=[_clean(dict(row)) for row in items],
        milestones=[_clean(dict(row)) for row in milestones],
        # A checklist with no rows means none was generated for this
        # transaction, not that the transaction has no requirements.
        note=(
            "No checklist rows exist for this transaction. Items are written by "
            "the compliance engine when a transaction is opened against a state "
            "with loaded requirements; an empty list is not a finding that "
            "nothing is required."
            if not items and not milestones else None
        ),
    )


async def _get_transaction_workflow(conn, ctx: TenantContext, tool_input: dict) -> dict:
    deal, error = await _resolve_deal(conn, ctx, tool_input)
    if error:
        return error
    transactions = await _transactions_for_deal(conn, ctx, str(deal["id"]))
    if not transactions:
        return _no_transaction("get_transaction_workflow", dict(deal))
    ids = [t["id"] for t in transactions]
    milestones = await conn.fetch(
        """SELECT transaction_id,milestone_type,title,status,due_at,completed_at
             FROM transaction_milestones
            WHERE tenant_id=$1::uuid AND transaction_id = ANY($2::uuid[])
            ORDER BY due_at ASC NULLS LAST""",
        ctx.tenant_id, ids,
    )
    offers = await conn.fetch(
        """SELECT transaction_id,id,status,amount,earnest_money,financing_type,
                  proposed_closing_date,expires_at,version,submitted_at,
                  accepted_at,rejected_at,withdrawn_at
             FROM transaction_offers
            WHERE tenant_id=$1::uuid AND transaction_id = ANY($2::uuid[])
            ORDER BY version DESC""",
        ctx.tenant_id, ids,
    )
    # party contact details are encrypted at rest and stay that way: role and
    # display name answer "who is on this deal", and the ciphertext answers
    # nothing a chat model should be handed.
    parties = await conn.fetch(
        """SELECT transaction_id,party_role,display_name,verified_at
             FROM transaction_parties
            WHERE tenant_id=$1::uuid AND transaction_id = ANY($2::uuid[])
            ORDER BY party_role""",
        ctx.tenant_id, ids,
    )
    documents = await conn.fetch(
        """SELECT transaction_id,id,document_type,template_key,template_version,
                  status,attorney_review_required,reviewed_at,approval_id,
                  created_at,updated_at
             FROM contract_documents
            WHERE tenant_id=$1::uuid AND transaction_id = ANY($2::uuid[])
            ORDER BY created_at DESC""",
        ctx.tenant_id, ids,
    )
    return _ok(
        "get_transaction_workflow",
        deal={"id": str(deal["id"]), "address": deal.get("address")},
        transactions=[_clean(t) for t in transactions],
        milestones=[_clean(dict(row)) for row in milestones],
        offers=[_clean(dict(row)) for row in offers],
        parties=[_clean(dict(row)) for row in parties],
        documents=[_clean(dict(row)) for row in documents],
        excluded_fields={
            "transaction_parties.contact_ciphertext": "encrypted contact details",
            "contract_documents.content_ciphertext": "encrypted document body",
        },
    )


async def _get_deal_financial_summary(conn, ctx: TenantContext, tool_input: dict) -> dict:
    deal, error = await _resolve_deal(conn, ctx, tool_input)
    if error:
        return error
    transactions = await _transactions_for_deal(conn, ctx, str(deal["id"]))
    underwriting = _json(deal["underwriting"], {}) or {}

    accepted = None
    if transactions:
        accepted = await conn.fetchrow(
            """SELECT o.transaction_id,o.amount,o.earnest_money,o.financing_type,
                      o.proposed_closing_date,o.accepted_at
                 FROM transaction_offers o
                WHERE o.tenant_id=$1::uuid
                  AND o.transaction_id = ANY($2::uuid[])
                  AND o.status='accepted'
                ORDER BY o.accepted_at DESC NULLS LAST
                LIMIT 1""",
            ctx.tenant_id, [t["id"] for t in transactions],
        )

    recorded = {
        "asking_price": _clean(deal.get("asking_price")),
        "purchase_price": _clean(transactions[0]["purchase_price"]) if transactions else None,
        "earnest_money": _clean(transactions[0]["earnest_money"]) if transactions else None,
        "financing_amount": _clean(transactions[0]["financing_amount"]) if transactions else None,
        "accepted_offer_amount": _clean(accepted["amount"]) if accepted else None,
        "arv": underwriting.get("arv"),
        "rehab": underwriting.get("rehab") or underwriting.get("rehab_estimate"),
        "mao": underwriting.get("mao"),
    }
    # Named explicitly rather than omitted: a summary that silently drops
    # holding costs reads as a deal with no holding costs.
    not_recorded = {
        "holding_costs": "No holding-cost field exists on the deal or transaction.",
        "closing_costs": "Not stored; estimate_closing_costs is a separate tool.",
        "assignment_fee": "Not stored on the transaction; it lives in the "
                          "assignment contract, which is encrypted.",
        "net_profit": "Cannot be computed without holding, closing and "
                      "assignment figures — the inputs above are incomplete.",
    }
    return _ok(
        "get_deal_financial_summary",
        deal={"id": str(deal["id"]), "address": deal.get("address"),
              "stage": deal.get("dossier_status")},
        recorded=recorded,
        not_recorded=not_recorded,
        transaction_count=len(transactions),
        underwriting_source=("leads.underwriting" if underwriting else None),
    )


async def _list_required_disclosures(conn, ctx: TenantContext, tool_input: dict) -> dict:
    state, error = _state_code(tool_input.get("state"))
    if error:
        return error
    if not state:
        return _err("state is required and must be a two-letter code.")
    rows = await conn.fetch(
        """SELECT form_name,form_type,required_when,effective_date,download_url,notes,
                  updated_at
             FROM state_disclosure_forms
            WHERE state_code=$1
            ORDER BY form_type,form_name""",
        state,
    )
    if not rows and await _table_is_empty(conn, "state_disclosure_forms"):
        return _dataset_not_loaded("list_required_disclosures", "state_disclosure_forms")
    return _ok(
        "list_required_disclosures",
        state=state,
        disclosures=[_clean(dict(row)) for row in rows],
        count=len(rows),
        note=(
            f"No disclosure forms are loaded for {state}. Other states have rows, "
            f"so this is a gap in the reference data for {state} — not a finding "
            f"that {state} requires no disclosures."
            if not rows else
            "These are the reference rows loaded on this deployment. They are a "
            "starting point for a transaction, not legal advice, and a form "
            "absent here may still be required."
        ),
    )


async def _check_contract_deadlines(conn, ctx: TenantContext, tool_input: dict) -> dict:
    import uuid as _uuid
    try:
        contract_id = str(_uuid.UUID(str(tool_input.get("contract_id") or "").strip()))
    except (ValueError, AttributeError):
        return _err("contract_id must be a UUID.")
    document = await conn.fetchrow(
        """SELECT id,transaction_id,lead_id,document_type,template_key,status,
                  attorney_review_required,reviewed_at,approval_id,created_at,updated_at
             FROM contract_documents
            WHERE id=$1::uuid AND tenant_id=$2::uuid""",
        contract_id, ctx.tenant_id,
    )
    if not document:
        return _err("That contract is not in this workspace.")
    transaction = None
    milestones: list = []
    if document["transaction_id"]:
        transaction = await conn.fetchrow(
            """SELECT id,status,property_address,offer_deadline,inspection_deadline,
                      financing_deadline,closing_deadline,closed_at
                 FROM transactions
                WHERE id=$1::uuid AND tenant_id=$2::uuid""",
            document["transaction_id"], ctx.tenant_id,
        )
        milestones = await conn.fetch(
            """SELECT milestone_type,title,status,due_at,completed_at
                 FROM transaction_milestones
                WHERE tenant_id=$1::uuid AND transaction_id=$2::uuid
                ORDER BY due_at ASC NULLS LAST""",
            ctx.tenant_id, document["transaction_id"],
        )

    deadlines = []
    now = datetime.now(timezone.utc)
    for label in ("offer_deadline", "inspection_deadline", "financing_deadline",
                  "closing_deadline"):
        value = transaction[label] if transaction else None
        if value is None:
            continue
        due = value if isinstance(value, datetime) else datetime.combine(
            value, datetime.min.time(), tzinfo=timezone.utc)
        deadlines.append({"deadline": label, "due_at": _clean(value),
                          "overdue": due < now,
                          "days_remaining": (due.date() - now.date()).days})

    return _ok(
        "check_contract_deadlines",
        contract=_clean(dict(document)),
        transaction=_clean(dict(transaction)) if transaction else None,
        deadlines=sorted(deadlines, key=lambda item: str(item["due_at"])),
        milestones=[_clean(dict(row)) for row in milestones],
        note=(
            "This contract is not linked to a transaction, so it carries no "
            "dates. Deadlines live on the transaction, not on the document."
            if not transaction else None
        ),
        excluded_fields={"contract_documents.content_ciphertext":
                         "encrypted document body"},
    )


# ---------------------------------------------------------------------------
# Media and performance
# ---------------------------------------------------------------------------

async def _list_property_photos(conn, ctx: TenantContext, tool_input: dict) -> dict:
    import uuid as _uuid
    try:
        listing_id = str(_uuid.UUID(str(tool_input.get("listing_id") or "").strip()))
    except (ValueError, AttributeError):
        return _err("listing_id must be a UUID.")
    rows = await conn.fetch(
        """SELECT id,kind,caption,sort_order,surface,uploaded_via,review_status,
                  provenance,generator,content_type,created_at
             FROM property_media
            WHERE tenant_id=$1::uuid AND listing_id=$2::uuid AND kind='photo'
            ORDER BY sort_order NULLS LAST, created_at""",
        ctx.tenant_id, listing_id,
    )
    photos = [_clean(dict(row)) for row in rows]
    synthetic = [p for p in photos if p.get("provenance") in ("ai_generated", "synthetic")]
    return _ok(
        "list_property_photos",
        listing_id=listing_id,
        photos=photos,
        count=len(photos),
        # An AI-generated image described as a photograph of the property is a
        # misrepresentation in an advertising context, so provenance travels
        # with every row and the count is stated separately.
        ai_generated_count=len(synthetic),
        provenance_note=(
            f"{len(synthetic)} of {len(photos)} images are AI-generated or "
            f"synthetic and must be labelled as such wherever they are shown."
            if synthetic else None
        ),
        url_note=(
            "Storage keys and URLs are omitted; use get_document_download_url "
            "or the media API to obtain a time-limited link."
        ),
    )


async def _get_agent_performance(conn, ctx: TenantContext, tool_input: dict) -> dict:
    agent_id = str(tool_input.get("agent_id") or "").strip() or None
    clients = await conn.fetchrow(
        """SELECT count(*)::int AS total,
                  count(*) FILTER (WHERE stage='closed')::int AS closed,
                  count(*) FILTER (WHERE stage='lost')::int AS lost,
                  count(*) FILTER (WHERE stage NOT IN ('closed','lost'))::int AS open
             FROM clients
            WHERE tenant_id=$1::uuid AND archived_at IS NULL
              AND ($2::text IS NULL OR assignee_id=$2)""",
        ctx.tenant_id, agent_id,
    )
    deals = await conn.fetchrow(
        """SELECT count(*)::int AS total,
                  count(*) FILTER (WHERE closed_at IS NOT NULL)::int AS closed,
                  coalesce(sum(purchase_price) FILTER (WHERE closed_at IS NOT NULL), 0) AS closed_volume,
                  coalesce(sum(purchase_price) FILTER (WHERE closed_at IS NULL), 0) AS open_volume
             FROM transactions
            WHERE tenant_id=$1::uuid AND ($2::text IS NULL OR created_by=$2)""",
        ctx.tenant_id, agent_id,
    )
    showings = await conn.fetchval(
        """SELECT count(*)::int FROM showings s
            WHERE s.tenant_id=$1::uuid
              AND ($2::text IS NULL OR EXISTS (
                    SELECT 1 FROM clients c
                     WHERE c.id=s.client_id AND c.tenant_id=s.tenant_id
                       AND c.assignee_id=$2))""",
        ctx.tenant_id, agent_id,
    )
    conversion = None
    if clients["total"]:
        conversion = round(clients["closed"] / clients["total"] * 100, 1)
    return _ok(
        "get_agent_performance",
        scope=("agent" if agent_id else "workspace"),
        agent_id=agent_id,
        clients=_clean(dict(clients)),
        transactions=_clean(dict(deals)),
        showings=showings,
        conversion_rate_pct=conversion,
        conversion_rate_formula=(
            "clients with stage 'closed' ÷ all non-archived assigned clients × 100"
        ),
        average_margin=None,
        not_recorded={
            "average_margin": "No per-deal margin is stored; purchase price is "
                              "recorded but neither cost basis nor sale proceeds are.",
            "pipeline_attribution": (
                "Pipeline leads carry no assigned-agent column, so lead counts "
                "cannot be attributed to an individual and are omitted rather "
                "than reported as workspace figures under an agent's name."
            ),
        },
        attribution_basis={
            "clients": "clients.assignee_id",
            "transactions": "transactions.created_by",
            "showings": "the assignee of the showing's client",
        },
    )


# ---------------------------------------------------------------------------
# Contract templates
# ---------------------------------------------------------------------------

async def _list_contract_templates(conn, ctx: TenantContext, tool_input: dict) -> dict:
    """Which templates are approved here — what draft_contract needs to name one.

    A tool that requires a template key nothing can enumerate is unusable, which
    is the same defect the marketplace buyer-profile routes had.
    """
    rows = await conn.fetch(
        """SELECT template_key,version,document_type,jurisdiction,status,
                  attorney_reviewed_by,attorney_reviewed_at,required_fields,
                  updated_at
             FROM contract_templates
            WHERE tenant_id=$1::uuid
            ORDER BY status,document_type,template_key""",
        ctx.tenant_id,
    )
    templates = [_clean(dict(row)) for row in rows]
    approved = [t for t in templates if t["status"] == "approved"]
    return _ok(
        "list_contract_templates",
        templates=templates,
        approved_count=len(approved),
        note=(
            "No template in this workspace is approved, so no contract can be "
            "drafted. Templates are bootstrapped and reviewed in Contract Vault; "
            "an unapproved template is not one an attorney has vetted for this "
            "brokerage."
            if not approved else None
        ),
    )


# ---------------------------------------------------------------------------
# Ops reads
# ---------------------------------------------------------------------------
#
# The allowlist decides what the model is *offered*; require_role decides what
# this tenant may *do*. They are different questions, so the role check lives
# here in the handler rather than in the allowlist: an agent asking about the
# job queue should be told it is not their information, not silently handed a
# model that pretends the tool does not exist.

def _require_ops_role(ctx: TenantContext, tool_name: str) -> Optional[dict]:
    from tenancy import Role

    if ctx.is_platform_admin or ctx.role is Role.BROKER_OWNER:
        return None
    return {
        "ok": False,
        "action_type": tool_name,
        "error": (
            f"{tool_name} reports on the whole workspace, so it is limited to "
            f"the broker-owner. Your role is {ctx.role.value}."
        ),
        "required_role": Role.BROKER_OWNER.value,
    }


async def _get_tenant_health(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "get_tenant_health")
    if denied:
        return denied
    # The lead count comes from lead_pipeline_counts, the rollup migration 0038
    # maintains by trigger on insert, delete and tenant/state updates. Counting
    # the table directly took 10,185 ms here — an index-only scan over 6.9M
    # entries, holding a pool connection, on a tool an agent can call at will.
    # The rollup answers the same number in 0.28 ms.
    counts = await conn.fetchrow(
        """SELECT (SELECT count(*) FROM clients WHERE tenant_id=$1::uuid AND archived_at IS NULL)::int AS clients,
                  (SELECT coalesce(sum(row_count),0) FROM lead_pipeline_counts WHERE tenant_id=$1::uuid)::bigint AS leads,
                  (SELECT count(*) FROM transactions WHERE tenant_id=$1::uuid AND closed_at IS NULL)::int AS open_transactions,
                  (SELECT count(*) FROM automation_jobs WHERE tenant_id=$1::uuid AND state='failed')::int AS failed_jobs,
                  (SELECT count(*) FROM action_approvals WHERE tenant_id=$1::uuid AND status='pending')::int AS pending_approvals""",
        ctx.tenant_id,
    )
    return _ok("get_tenant_health", counts=_clean(dict(counts)),
               scope="this workspace only",
               counts_source={"leads": "lead_pipeline_counts (trigger-maintained rollup)"})


async def _get_job_queue(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "get_job_queue")
    if denied:
        return denied
    rows = await conn.fetch(
        """SELECT job_type,state,risk_class,attempt_count,max_attempts,
                  last_error_code,scheduled_at,started_at,completed_at,created_at
             FROM automation_jobs
            WHERE tenant_id=$1::uuid
            ORDER BY created_at DESC LIMIT 50""",
        ctx.tenant_id,
    )
    # `payload` and `result` are excluded: a job payload carries the contents of
    # whatever it was going to do, which is not what "how is the queue doing"
    # asks for.
    return _ok("get_job_queue", jobs=[_clean(dict(row)) for row in rows],
               count=len(rows),
               excluded_fields={"automation_jobs.payload": "job input",
                                "automation_jobs.result": "job output"})


async def _get_audit_trail(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "get_audit_trail")
    if denied:
        return denied
    rows = await conn.fetch(
        """SELECT seq,category,action,user_id,target_id,created_at
             FROM audit_ledger
            WHERE tenant_id=$1::uuid
            ORDER BY seq DESC LIMIT 50""",
        ctx.tenant_id,
    )
    # `metadata` is withheld: entries carry approval reasons and record
    # contents, and the hash chain is the integrity claim, not the payload.
    return _ok("get_audit_trail", entries=[_clean(dict(row)) for row in rows],
               count=len(rows),
               excluded_fields={"audit_ledger.metadata": "entry detail",
                                "audit_ledger.entry_hash": "chain hash"})


async def _list_integration_status(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "list_integration_status")
    if denied:
        return denied
    rows = await conn.fetch(
        """SELECT provider,account_label,validation_status,validation_error,
                  validated_capabilities,expires_at,last_validated_at,disabled_at
             FROM provider_credentials
            WHERE tenant_id=$1::uuid
            ORDER BY provider""",
        ctx.tenant_id,
    )
    # Ciphertext and refresh tokens never leave the database through a tool.
    return _ok("list_integration_status",
               providers=[_clean(dict(row)) for row in rows], count=len(rows))


# The one flag whose call site disagrees with the generic default. Reporting
# feature_enabled(SPEED_TO_LEAD) alone would say "on" for an unset env var while
# speed_to_lead._enabled() reads default=False and keeps it off — and this is
# the single feature that contacts a consumer without an agent asking it to, so
# it is the worst possible flag to be wrong about.
_FEATURE_CALLSITE_DEFAULT: dict[str, bool] = {"speed_to_lead": False}


async def _get_feature_flags(conn, ctx: TenantContext, tool_input: dict) -> dict:
    """Deliberately not role-gated.

    The other ops tools report on the workspace's operations, which is the
    broker-owner's business. This one reports what the product can do — and an
    assistant that cannot see its own deployment's capabilities will describe
    them wrongly to whoever is asking.
    """
    from platform_policy import _FEATURE_ENV, Feature, feature_enabled

    flags = []
    for feature in Feature:
        generic = feature_enabled(feature)
        override = _FEATURE_CALLSITE_DEFAULT.get(feature.value)
        env_name = _FEATURE_ENV[feature]
        env_set = os.getenv(env_name) is not None
        effective = generic if (env_set or override is None) else override
        flags.append({
            "feature": feature.value,
            "env_var": env_name,
            "env_set": env_set,
            "enabled": effective,
            "differs_from_generic_default": effective != generic,
            "note": (
                f"Unset, and {feature.value} defaults OFF at its call site "
                f"rather than ON like the others."
                if effective != generic else None
            ),
        })
    return _ok(
        "get_feature_flags",
        flags=flags,
        enabled_count=sum(1 for flag in flags if flag["enabled"]),
        scope="this deployment, not this workspace — flags are process-wide",
    )


async def _get_billing_status(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "get_billing_status")
    if denied:
        return denied
    from billing_usage import metering_configured

    subscription = await conn.fetchrow(
        """SELECT status,plan,current_period_end,
                  stripe_customer_id IS NOT NULL AS has_customer,
                  stripe_subscription_id IS NOT NULL AS has_subscription,updated_at
             FROM subscriptions WHERE tenant_id=$1::uuid""",
        ctx.tenant_id,
    )
    pending = await conn.fetchrow(
        """SELECT count(*)::int AS n,
                  count(*) FILTER (WHERE report_error IS NOT NULL)::int AS failed
             FROM billing_usage_events
            WHERE tenant_id=$1::uuid AND reported_at IS NULL""",
        ctx.tenant_id,
    )
    return _ok(
        "get_billing_status",
        subscription=_clean(dict(subscription)) if subscription else None,
        subscription_note=(
            None if subscription else
            "No subscription row exists for this workspace. That is not the "
            "same as an unpaid one — it means billing was never set up here."
        ),
        usage_metering_configured=metering_configured(),
        unreported_usage_events=pending["n"] if pending else 0,
        usage_events_with_report_errors=pending["failed"] if pending else 0,
        # Processor ids are reported as present/absent rather than by value, and
        # the card itself never touches this database.
        not_available={
            "payment_method": (
                "Card and bank details are held by the payment processor and "
                "are not stored on this platform, so no tool can report them."
            ),
        },
    )


async def _list_billing_invoices(conn, ctx: TenantContext, tool_input: dict) -> dict:
    """There is no local invoice mirror, and inventing one would be worse.

    Issued invoices live with the payment processor. What this platform does
    hold is metered usage that has not been reported yet — which answers the
    question behind most invoice lookups ("what is the next bill for?") without
    pretending to be an invoice.
    """
    denied = _require_ops_role(ctx, "list_billing_invoices")
    if denied:
        return denied
    rows = await conn.fetch(
        """SELECT metric,
                  sum(quantity)::numeric AS quantity,
                  count(*)::int AS event_count,
                  min(occurred_at) AS first_occurred_at,
                  max(occurred_at) AS last_occurred_at
             FROM billing_usage_events
            WHERE tenant_id=$1::uuid AND reported_at IS NULL
            GROUP BY metric ORDER BY metric""",
        ctx.tenant_id,
    )
    return _ok(
        "list_billing_invoices",
        invoices=[],
        invoices_unavailable_reason=(
            "This platform does not mirror issued invoices. They are held by "
            "the payment processor and have to be read there — an invoice "
            "reconstructed from local usage would not be the document the "
            "brokerage was actually billed."
        ),
        unreported_usage_by_metric=[_clean(dict(row)) for row in rows],
        unreported_usage_note=(
            "Usage recorded here but not yet reported to the processor. It is "
            "what the next invoice will draw on, not an invoice."
        ),
    )


async def _list_recent_errors(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "list_recent_errors")
    if denied:
        return denied
    jobs = await conn.fetch(
        """SELECT job_type,state,last_error_code,attempt_count,max_attempts,
                  created_at,completed_at
             FROM automation_jobs
            WHERE tenant_id=$1::uuid AND last_error_code IS NOT NULL
            ORDER BY created_at DESC LIMIT 25""",
        ctx.tenant_id,
    )
    anomalies = await conn.fetch(
        """SELECT anomaly_type,severity,route,created_at
             FROM audit_anomaly_alerts
            WHERE tenant_id=$1::uuid
            ORDER BY created_at DESC LIMIT 25""",
        ctx.tenant_id,
    )
    return _ok(
        "list_recent_errors",
        failed_jobs=[_clean(dict(row)) for row in jobs],
        anomalies=[_clean(dict(row)) for row in anomalies],
        count=len(jobs) + len(anomalies),
        # The catalog once said "from the application log". There is no
        # queryable application log on this deployment; these two tables are
        # what durably records a failure, and an error that only reached stdout
        # is not visible here at all.
        sources=["automation_jobs.last_error_code", "audit_anomaly_alerts"],
        not_covered=(
            "Errors that were only logged to stdout are not recorded in the "
            "database and cannot be listed here."
        ),
        excluded_fields={
            "automation_jobs.last_error": "free-text provider response",
            "audit_anomaly_alerts.evidence": "captured request detail",
            "audit_anomaly_alerts.source_ip": "caller address",
        },
    )


async def _get_database_stats(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "get_database_stats")
    if denied:
        return denied
    from db.connection import pool_stats

    ledger = await conn.fetchval("SELECT to_regclass('public.schema_migrations')")
    applied = None
    if ledger:
        applied = await conn.fetchval("SELECT count(*)::int FROM schema_migrations")
    workspace = await conn.fetchrow(
        """SELECT (SELECT coalesce(sum(row_count),0) FROM lead_pipeline_counts WHERE tenant_id=$1::uuid)::bigint AS leads,
                  (SELECT count(*) FROM clients WHERE tenant_id=$1::uuid)::int AS clients,
                  (SELECT count(*) FROM transactions WHERE tenant_id=$1::uuid)::int AS transactions,
                  (SELECT count(*) FROM property_media WHERE tenant_id=$1::uuid)::int AS media""",
        ctx.tenant_id,
    )
    stats = pool_stats()
    result = _ok(
        "get_database_stats",
        pool=stats or None,
        pool_unavailable_reason=(
            None if stats else
            "The connection pool has not been initialised in this process."
        ),
        workspace_rows=_clean(dict(workspace)),
        migration_ledger_present=bool(ledger),
        migrations_applied=applied,
        migration_note=(
            None if ledger else
            "No schema_migrations table exists on this deployment, so the "
            "applied migration version is unknown. Files on disk are not "
            "evidence that they ran."
        ),
        # Table sizes and index-usage counters are database-wide. Reporting them
        # to a broker-owner would leak the platform's aggregate scale and every
        # other tenant's data volume through a per-tenant tool.
        scope="this workspace, plus this process's pool",
    )
    if ctx.is_platform_admin:
        tables = await conn.fetch(
            """SELECT relname AS table_name,seq_scan,idx_scan,n_live_tup,
                      pg_size_pretty(pg_total_relation_size(relid)) AS total_size
                 FROM pg_stat_user_tables
                ORDER BY pg_total_relation_size(relid) DESC LIMIT 15"""
        )
        result["platform_tables"] = [_clean(dict(row)) for row in tables]
        result["scope"] = (
            "this workspace, this process's pool, and platform-wide table stats"
        )
    return result


async def _run_health_check(conn, ctx: TenantContext, tool_input: dict) -> dict:
    denied = _require_ops_role(ctx, "run_health_check")
    if denied:
        return denied
    from db.connection import pool_stats

    started = time.monotonic()
    await conn.fetchval("SELECT 1")
    roundtrip_ms = round((time.monotonic() - started) * 1000, 1)

    failed_jobs = await conn.fetchval(
        """SELECT count(*)::int FROM automation_jobs
            WHERE tenant_id=$1::uuid AND state='failed'""",
        ctx.tenant_id,
    )
    unhealthy = await conn.fetch(
        """SELECT provider,validation_status,disabled_at
             FROM provider_credentials
            WHERE tenant_id=$1::uuid
              AND (disabled_at IS NOT NULL OR validation_status IS DISTINCT FROM 'valid')""",
        ctx.tenant_id,
    )
    return _ok(
        "run_health_check",
        database={"reachable": True, "roundtrip_ms": roundtrip_ms},
        pool=pool_stats() or None,
        failed_jobs=int(failed_jobs or 0),
        unhealthy_providers=[_clean(dict(row)) for row in unhealthy],
        # A health check that reports "payment processor: OK" without calling it
        # is worse than one that says it did not look. Reaching a third party is
        # a side effect with a cost and a rate limit, and not something a chat
        # turn should trigger.
        checks_not_performed={
            "payment_processor": "not contacted",
            "mls_feed": "not contacted",
            "inference_provider": "not contacted",
            "mail_transport": "not contacted",
            "cache": "not contacted",
        },
        checks_not_performed_reason=(
            "This check reads local state only. The stored validation status of "
            "each provider is reported above; that records the last time a "
            "credential was checked, not whether the service is up now."
        ),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def _get_property_tour(conn, ctx: TenantContext, tool_input: dict) -> dict:
    """What a property can actually show, and why anything is missing.

    The agent had no way to see this at all — no tour, capture or reconstruction
    tool existed on the surface — so "is there a 3D tour of 12 Oak St?" was
    unanswerable and any answer it gave was invented.

    Two things it must get right. The asset list is the *union* of what exists,
    not a single best one, because a property can hold a capture and 360s and
    photos at once. And each asset carries its own provenance, so the agent can
    say "the 360s are of this house, the 3D model is a demo" instead of
    flattening both into one claim.

    When nothing is available, the reason is the provider's own `available()`
    string, verbatim. That is the difference between the agent saying "RunPod
    has no credits — add some at runpod.io" and "3D tours are unavailable",
    which sends someone looking for an outage that is not there.
    """
    from uuid import UUID as _UUID

    import tour_api

    lead_raw = str(tool_input.get("lead_id") or "").strip()
    listing_raw = str(tool_input.get("listing_id") or "").strip()
    if not lead_raw and not listing_raw:
        return {"error": "Provide lead_id or listing_id."}

    def _uuid_or_none(raw: str, field: str):
        if not raw:
            return None, None
        try:
            return _UUID(raw), None
        except (ValueError, AttributeError):
            return None, {"error": f"{field} must be a UUID."}

    lead_id, err = _uuid_or_none(lead_raw, "lead_id")
    if err:
        return err
    listing_id, err = _uuid_or_none(listing_raw, "listing_id")
    if err:
        return err

    rows, scene_rows, plan_row = await tour_api.fetch_tour_rows(conn, lead_id, listing_id)
    tour = tour_api.build_tour(rows, scene_rows, plan_row,
                               lead_id=lead_id, listing_id=listing_id)

    assets = [
        {
            "kind": asset["kind"],
            "label": asset["label"],
            "shows_this_property": asset["is_this_property"],
            "provenance": asset.get("provenance"),
            "walkable": asset.get("walkable", False),
            "count": asset.get("count"),
        }
        for asset in tour["assets"]
    ]

    # Why an interior capture is missing, in the provider's own words.
    missing_reason = ""
    if not any(a["kind"] == "splat" and a["shows_this_property"] for a in assets):
        try:
            from reconstruction_providers import get_provider

            ready, reason = get_provider().available()
            missing_reason = "" if ready else reason
        except Exception as exc:  # noqa: BLE001 - a config error is an answer, not a crash
            missing_reason = str(exc)

    return {
        "assets": assets,
        "has_walkable_interior": any(a["walkable"] for a in assets),
        "interior_capture_unavailable_because": missing_reason,
        "note": (
            "These are everything this property has; they combine into one tour "
            "rather than replacing each other. `shows_this_property` false means "
            "the asset is a demo or generated space and must never be described "
            "as this home."
        ),
    }


_HANDLERS = {
    "get_property_tour": _get_property_tour,
    "search_listings": _search_listings,
    "get_listing_detail": _get_listing_detail,
    "list_comparable_sales": _list_comparable_sales,
    "estimate_arv": _estimate_arv,
    "estimate_rehab": _estimate_rehab,
    "calculate_mao": _calculate_mao,
    "get_market_trends": _get_market_trends,
    "get_days_on_market": _get_days_on_market,
    "list_closing_checklist": _list_closing_checklist,
    "get_transaction_workflow": _get_transaction_workflow,
    "get_deal_financial_summary": _get_deal_financial_summary,
    "list_required_disclosures": _list_required_disclosures,
    "check_contract_deadlines": _check_contract_deadlines,
    "list_property_photos": _list_property_photos,
    "get_agent_performance": _get_agent_performance,
    "list_contract_templates": _list_contract_templates,
    "get_tenant_health": _get_tenant_health,
    "get_job_queue": _get_job_queue,
    "get_audit_trail": _get_audit_trail,
    "list_integration_status": _list_integration_status,
    "get_feature_flags": _get_feature_flags,
    "get_billing_status": _get_billing_status,
    "list_billing_invoices": _list_billing_invoices,
    "list_recent_errors": _list_recent_errors,
    "get_database_stats": _get_database_stats,
    "run_health_check": _run_health_check,
}

assert set(_HANDLERS) == TOOLS_HANDLED, "handler table and TOOLS_HANDLED disagree"


async def execute(conn, ctx: TenantContext, tool_name: str, tool_input: dict) -> dict:
    """Run one read-only tool. Raises KeyError for a name this module does not own."""
    return await _HANDLERS[tool_name](conn, ctx, tool_input or {})
