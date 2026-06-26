"""
mls_portal.py — Neoh retail MLS browse portal (router prefix /api/mls).

A quota-safe public browse surface over ``oracle_mls_listings``. RentCast's free
tier is **50 calls/MONTH total**, so this NEVER hits RentCast on every browse.
Instead:

  1. A browse request resolves the searched AREA (zip, or city+state, or state).
  2. If that area has NO fresh fetch marker (di_cache, 24h TTL) we make ONE
     RentCast ``GET /v1/listings/sale`` call for the whole area, UPSERT the
     results into ``oracle_mls_listings``, and stamp the area marker — even when
     the area returns zero listings (a negative-cache, so an empty area is not
     re-fetched 50 times and burns the monthly quota).
  3. EVERY browse then serves straight from ``oracle_mls_listings`` with the
     caller's filters applied in SQL. Price/beds/property-type filters never
     trigger a new fetch — the area is fetched once, then filtered locally.

Quota guards (defense in depth):
  * Per-request: there is exactly one RentCast call site in the fetch path.
  * Per-area: the di_cache marker caps fetches to ~1 per area per 24h.
  * Per-process: a per-area asyncio.Lock collapses concurrent same-area misses
    onto a single provider hit (no double-spend under load).
  * Hard stop: no ``RENTCAST_API_KEY`` ⇒ never fetch, serve cache + degraded.
  * On 429 (quota exhausted) we back off for the full 24h; on a transient error
    we back off 1h. Either way the request still serves whatever is cached.

This is the same table the RESO feed (data_integrations/listings_feed.py) writes,
so when a real RESO feed is configured its rows appear here automatically and the
RentCast bridge simply stops being the only source. Cached MLS rows are written
under the platform/ingest tenant, mirroring listings_feed.py.

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
import logging
import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query, status

from db.connection import tenant_tx, get_pool
from tenancy import TenantContext, Role, require_context

logger = logging.getLogger("oracle.mls_portal")

router = APIRouter(prefix="/api/mls", tags=["MLS Portal"])

# ── Provider / cache constants ────────────────────────────────────────────────
RENTCAST_BASE = "https://api.rentcast.io/v1"
REQUEST_TIMEOUT = 20

# Source discriminator for rows this portal caches. The (mls_id, mls_number)
# UNIQUE key makes the upsert idempotent; "rentcast" namespaces our rows away
# from any real RESO board id, so they never collide with a future live feed.
MLS_SOURCE_ID = "rentcast"

PAGE_SIZE = 24                     # listing cards per page
RENTCAST_FETCH_LIMIT = 500        # RentCast max per call — one precious call, grab the most
AREA_TTL = 24 * 3600              # fresh-area window: don't re-fetch within 24h
QUOTA_TTL = 24 * 3600            # 429 backoff: quota exhausted, hard back-off for the day
ERROR_TTL = 3600                 # transient-error backoff: retry the area in 1h

PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000000"

_STATE_RE = re.compile(r"^[A-Za-z]{2}$")
_ZIP_RE = re.compile(r"^\d{5}$")

# Per-area locks: collapse concurrent same-area cache-misses onto one fetch so two
# simultaneous browsers of the same city never spend two RentCast calls.
_AREA_LOCKS: dict[str, asyncio.Lock] = {}


class _RentCastError(Exception):
    """Raised on a non-recoverable RentCast response; carries the HTTP status."""

    def __init__(self, http_status: int, message: str = "") -> None:
        super().__init__(message or f"RentCast HTTP {http_status}")
        self.http_status = http_status


# ── small coercers (mirror data_integrations/listings_feed.py) ────────────────

def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime,)):
        return v.isoformat()
    return str(v)


def _date(v: Any) -> Optional[str]:
    """ISO timestamps → date part for a SQL date column."""
    if not v:
        return None
    return str(v)[:10]


def _lock_for(key: str) -> asyncio.Lock:
    lock = _AREA_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _AREA_LOCKS[key] = lock
    return lock


def _ingest_ctx() -> TenantContext:
    """Platform-admin context for writing cached MLS rows — same idiom as
    listings_feed.py. The table has no tenant column/RLS, but we still write
    under the platform/ingest tenant so attribution is consistent."""
    tenant = os.getenv("ORACLE_INGEST_TENANT_ID") or PLATFORM_TENANT_ID
    return TenantContext(agent_id="mls-portal", tenant_id=tenant, role=Role.PLATFORM_ADMIN)


# ── area resolution ───────────────────────────────────────────────────────────

def _resolve_area(city: Optional[str], state: Optional[str], zip_code: Optional[str]) -> dict:
    """Normalize the search location into a fetch plan.

    Returns {"key", "rentcast_params", "fetchable"}. ``fetchable`` is False when
    we lack enough to safely query RentCast (RentCast needs a zip, or a state —
    a bare city would otherwise pull the wrong city nationwide / waste a call).
    """
    city_n = (city or "").strip()
    state_n = (state or "").strip().upper()
    zip_n = (zip_code or "").strip()

    if zip_n and _ZIP_RE.match(zip_n):
        return {
            "key": f"zip:{zip_n}",
            "rentcast_params": {"zipCode": zip_n},
            "fetchable": True,
        }
    if state_n and _STATE_RE.match(state_n) and city_n:
        return {
            "key": f"cs:{state_n}:{city_n.lower()}",
            "rentcast_params": {"city": city_n, "state": state_n},
            "fetchable": True,
        }
    if state_n and _STATE_RE.match(state_n):
        return {
            "key": f"st:{state_n}",
            "rentcast_params": {"state": state_n},
            "fetchable": True,
        }
    return {"key": "", "rentcast_params": {}, "fetchable": False}


# ── RentCast fetch + normalize ────────────────────────────────────────────────

async def _fetch_rentcast_listings(rentcast_params: dict) -> list[dict]:
    """ONE RentCast GET /v1/listings/sale call. Returns the raw listing array.

    Raises _RentCastError on 429 (quota) or any non-200, so the caller can choose
    its back-off TTL. Never returns partial garbage."""
    key = os.environ.get("RENTCAST_API_KEY", "")
    if not key:
        raise _RentCastError(0, "RENTCAST_API_KEY not configured")

    params = {**rentcast_params, "status": "Active", "limit": str(RENTCAST_FETCH_LIMIT)}
    headers = {"X-Api-Key": key, "accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{RENTCAST_BASE}/listings/sale", params=params, headers=headers) as resp:
            if resp.status == 429:
                logger.warning("RentCast listings/sale: 429 quota/rate limit for %s", params)
                raise _RentCastError(429, "RentCast rate/quota limit")
            if resp.status == 401:
                logger.warning("RentCast listings/sale: 401 — RENTCAST_API_KEY rejected.")
                raise _RentCastError(401, "RentCast API key rejected")
            if resp.status != 200:
                logger.warning("RentCast listings/sale: HTTP %s for %s", resp.status, params)
                raise _RentCastError(resp.status, f"RentCast HTTP {resp.status}")
            data = await resp.json()
    # RentCast returns a JSON array for this endpoint.
    if isinstance(data, list):
        return data
    if isinstance(data, dict):  # be tolerant if they ever wrap it
        return data.get("listings") or data.get("data") or []
    return []


def _normalize_rentcast(item: dict) -> Optional[dict]:
    """Map a RentCast sale-listing to the oracle_mls_listings column shape.

    Keyed by RentCast's stable ``id`` slug as mls_number so re-fetches upsert in
    place. Returns None for records we can't key (no id/mlsNumber)."""
    g = item.get
    mls_number = str(g("id") or g("mlsNumber") or "").strip()
    if not mls_number:
        return None

    # RentCast exposes a single float `bathrooms` (e.g. 2.5). Split into the
    # full/half columns the table carries.
    baths = _num(g("bathrooms"))
    baths_full = int(math.floor(baths)) if baths is not None else None
    baths_half = (1 if (baths - math.floor(baths)) >= 0.5 else 0) if baths is not None else None

    hoa = g("hoa") or {}
    hoa_fee = _num(hoa.get("fee")) if isinstance(hoa, dict) else None

    return {
        "mls_id": MLS_SOURCE_ID,
        "mls_number": mls_number,
        "address": (g("formattedAddress") or g("addressLine1") or "").strip(),
        "city": (g("city") or "").strip(),
        "state_code": (g("state") or "")[:2].upper(),
        "zip_code": (g("zipCode") or "").strip(),
        "county": (g("county") or "").strip(),
        "latitude": _num(g("latitude")),
        "longitude": _num(g("longitude")),
        "list_price": _num(g("price")) or 0.0,
        "orig_list_price": None,
        "status": (g("status") or "active").strip().lower().replace(" ", "_"),
        "property_type": (g("propertyType") or "residential_1_4").strip(),
        "beds": _int(g("bedrooms")),
        "baths_full": baths_full,
        "baths_half": baths_half,
        "sqft": _int(g("squareFootage")),
        "lot_sqft": _int(g("lotSize")),
        "year_built": _int(g("yearBuilt")),
        "hoa_monthly": hoa_fee,
        "days_on_market": _int(g("daysOnMarket")),
        "list_date": _date(g("listedDate")),
        "close_date": None,
        "close_price": None,
        "description": g("description"),
    }


_UPSERT = """
    INSERT INTO oracle_mls_listings (
        mls_id, mls_number, address, city, state_code, zip_code, county,
        latitude, longitude, list_price, orig_list_price, status, property_type,
        beds, baths_full, baths_half, sqft, lot_sqft, year_built, hoa_monthly,
        days_on_market, list_date, close_date, close_price, description, last_updated
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
        $21,$22,$23,$24,$25, now()
    )
    ON CONFLICT (mls_id, mls_number) DO UPDATE SET
        address=EXCLUDED.address, city=EXCLUDED.city, state_code=EXCLUDED.state_code,
        zip_code=EXCLUDED.zip_code, county=EXCLUDED.county,
        latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
        list_price=EXCLUDED.list_price, orig_list_price=EXCLUDED.orig_list_price,
        status=EXCLUDED.status, property_type=EXCLUDED.property_type,
        beds=EXCLUDED.beds, baths_full=EXCLUDED.baths_full, baths_half=EXCLUDED.baths_half,
        sqft=EXCLUDED.sqft, lot_sqft=EXCLUDED.lot_sqft, year_built=EXCLUDED.year_built,
        hoa_monthly=EXCLUDED.hoa_monthly, days_on_market=EXCLUDED.days_on_market,
        list_date=EXCLUDED.list_date, close_date=EXCLUDED.close_date,
        close_price=EXCLUDED.close_price, description=EXCLUDED.description,
        last_updated=now()
"""


async def _upsert_listings(records: list[dict]) -> int:
    """Idempotent bulk upsert of normalized rows under the ingest tenant."""
    if not records:
        return 0
    written = 0
    async with tenant_tx(_ingest_ctx()) as conn:
        for rec in records:
            if not rec or not rec.get("mls_number"):
                continue
            await conn.execute(
                _UPSERT,
                rec["mls_id"], rec["mls_number"], rec["address"], rec["city"],
                rec["state_code"], rec["zip_code"], rec["county"], rec["latitude"],
                rec["longitude"], rec["list_price"], rec["orig_list_price"], rec["status"],
                rec["property_type"], rec["beds"], rec["baths_full"], rec["baths_half"],
                rec["sqft"], rec["lot_sqft"], rec["year_built"], rec["hoa_monthly"],
                rec["days_on_market"], rec["list_date"], rec["close_date"],
                rec["close_price"], rec["description"],
            )
            written += 1
    return written


# ── di_cache area marker (durable negative cache + quota guard) ────────────────

def _cache():
    """PG-backed IntegrationCache, or None outside app context. Best-effort:
    a cache failure must never break a browse (it just means we may re-fetch)."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        from data_integrations.cache import IntegrationCache
        return IntegrationCache(pool)
    except Exception as e:  # noqa: BLE001
        logger.debug("MLS portal cache unavailable: %s", e)
        return None


async def _area_marker_get(key: str) -> Optional[dict]:
    cache = _cache()
    if cache is None or not key:
        return None
    try:
        return await cache.get(f"mlsportal:area:{key}")
    except Exception as e:  # noqa: BLE001
        logger.debug("MLS portal marker GET skipped for %s: %s", key, e)
        return None


async def _area_marker_set(key: str, payload: dict, ttl: int) -> None:
    cache = _cache()
    if cache is None or not key:
        return
    try:
        await cache.set(f"mlsportal:area:{key}", payload, ttl)
    except Exception as e:  # noqa: BLE001
        logger.debug("MLS portal marker SET skipped for %s: %s", key, e)


async def _ensure_area_cached(area: dict) -> tuple[bool, Optional[str]]:
    """Ensure the area has been fetched from RentCast within the TTL.

    Returns (degraded, notice). Makes AT MOST ONE RentCast call. Never raises —
    any provider failure degrades to serving whatever is already cached."""
    key = area["key"]
    if not area["fetchable"]:
        # Not enough location to fetch safely; serve cache only (not degraded —
        # the empty/area-needed state is a normal UX, not a provider failure).
        return (False, None)

    if not os.environ.get("RENTCAST_API_KEY"):
        return (True, "RentCast not configured — showing cached listings only.")

    # Fast path: a fresh marker means we already fetched this area recently.
    if await _area_marker_get(key) is not None:
        return (False, None)

    # Serialize same-area work so concurrent misses share one provider call.
    async with _lock_for(key):
        if await _area_marker_get(key) is not None:
            return (False, None)

        try:
            raw = await _fetch_rentcast_listings(area["rentcast_params"])
        except _RentCastError as e:
            if e.http_status == 429:
                # Quota/rate exhausted — back off hard for the day.
                await _area_marker_set(key, {"status": "quota", "at": _iso(datetime.now(timezone.utc))}, QUOTA_TTL)
                return (True, "RentCast quota reached — showing cached listings.")
            if e.http_status in (401, 403):
                # Persistent auth/plan rejection — NOT transient. Back off the full
                # day so we don't pointlessly re-hit the provider every hour.
                await _area_marker_set(key, {"status": "unauthorized", "code": e.http_status, "at": _iso(datetime.now(timezone.utc))}, QUOTA_TTL)
                return (True, "RentCast listings not authorized for this key — showing cached listings.")
            # Transient/other error — short back-off so we recover within the hour.
            await _area_marker_set(key, {"status": "error", "code": e.http_status, "at": _iso(datetime.now(timezone.utc))}, ERROR_TTL)
            return (True, "Live listing service unavailable — showing cached listings.")
        except Exception as e:  # noqa: BLE001 — provider is best-effort
            logger.warning("RentCast listings fetch failed for %s: %s", key, e)
            await _area_marker_set(key, {"status": "error", "at": _iso(datetime.now(timezone.utc))}, ERROR_TTL)
            return (True, "Live listing service unavailable — showing cached listings.")

        records = [r for r in (_normalize_rentcast(it) for it in raw) if r]
        try:
            written = await _upsert_listings(records)
        except Exception as e:  # noqa: BLE001 — never let a write error break the browse
            logger.warning("MLS portal upsert failed for %s: %s", key, e)
            written = 0

        # Stamp the marker even on a zero-result area (negative cache) so we don't
        # re-spend the monthly quota re-querying an empty market.
        await _area_marker_set(
            key,
            {"status": "ok", "fetched": len(raw), "written": written, "at": _iso(datetime.now(timezone.utc))},
            AREA_TTL,
        )
        return (False, None)


# ── serializer ────────────────────────────────────────────────────────────────

def _listing_json(r: dict) -> dict:
    bf = r.get("baths_full")
    bh = r.get("baths_half")
    baths = None
    if bf is not None or bh is not None:
        baths = (bf or 0) + 0.5 * (bh or 0)
    photos = r.get("photos") or []
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
        "source": "rentcast" if r.get("mls_id") == MLS_SOURCE_ID else (r.get("mls_id") or "mls"),
        "last_updated": _iso(r.get("last_updated")),
    }


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
    """Paged, filtered browse served from ``oracle_mls_listings``.

    The searched area is fetched from RentCast at most once per 24h (then cached);
    all filtering/pagination runs locally against the table — browsing is free."""
    area = _resolve_area(city, state, zip)

    # Bridge step: make sure the area is populated (≤1 RentCast call), then serve.
    degraded, notice = await _ensure_area_cached(area)

    conditions: list[str] = ["1 = 1"]
    args: list[Any] = []

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
        f"ORDER BY last_updated DESC NULLS LAST, list_price DESC NULLS LAST "
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
    return {
        "listings": listings,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_more": offset + len(listings) < total,
        "degraded": degraded,
        "source": "RentCast (cached)",
        "notice": notice,
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
                "SELECT * FROM oracle_mls_listings WHERE id = $1::uuid", listing_id
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    except Exception as exc:  # noqa: BLE001
        logger.error("MLS portal detail failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Listing {listing_id!r} not found.")
    return _listing_json(dict(row))
