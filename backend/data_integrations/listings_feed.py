"""
data_integrations/listings_feed.py — fast-moving MLS "new listings" delta feed.

This is the hourly counterpart to the slow parcel harvest. Listings change by the
minute, so this pulls only the DELTA — records whose ModificationTimestamp is
newer than the last successful sync — via the RESO Web API (the MLS industry
standard OData feed). Results upsert into ``oracle_mls_listings`` and the cursor
lives in ``mls_sync_status.last_sync_at`` (DB-persisted, so it survives restarts
and never re-pulls the whole feed).

No feed credentials are committed anywhere. The task is configuration-gated and
skips cleanly when unset, exactly like the parcel harvest skips without a tenant:

    ORACLE_RESO_URL=https://api.<mls>.org/RESO/OData/Property   # the Property resource
    ORACLE_RESO_TOKEN=<bearer token>
    ORACLE_RESO_MLS_ID=<board id, e.g. "crmls">
    ORACLE_RESO_MLS_NAME="California Regional MLS"   # optional label
    ORACLE_RESO_LOOKBACK_HOURS=1                     # first-run / null-cursor window
    ORACLE_RESO_PAGE=500                             # $top per page
    ORACLE_INGEST_TENANT_ID=<uuid>                  # platform-admin ctx for the write
"""

from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .base import DataSource, RateLimiter, RetryConfig

_RESO_URL = "ORACLE_RESO_URL"
_RESO_TOKEN = "ORACLE_RESO_TOKEN"
_RESO_MLS_ID = "ORACLE_RESO_MLS_ID"


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _date(v: Any) -> Optional[str]:
    """RESO dates are ISO; keep just the date part for a SQL date column."""
    if not v:
        return None
    return str(v)[:10]


# Pull the delta cursor back this many minutes from the max ModificationTimestamp
# actually processed, so a record sharing the boundary second with a record we
# already pulled isn't skipped by the strict `gt` filter on the next run. The
# upsert is idempotent, so the small re-pull overlap is harmless.
_CURSOR_OVERLAP_MIN = 2


def _parse_reso_dt(value: Any) -> Optional[datetime]:
    """Parse a RESO ModificationTimestamp (ISO-8601) into a tz-aware UTC datetime.
    Returns None when absent/unparseable so a bad value can never corrupt (advance
    past, or rewind) the persisted delta cursor."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RESOListingsFeed(DataSource):
    """RESO Web API Property-resource delta puller → oracle_mls_listings."""

    source_name = "reso_listings"

    def __init__(self, **kw: Any) -> None:
        # Listings move fast — a tighter limiter than the slow integrations.
        super().__init__(
            rate_limiter=kw.get("rate_limiter") or RateLimiter(min_interval=0.5, jitter=0.3),
            retry_config=kw.get("retry_config") or RetryConfig(max_attempts=4),
        )
        self.base_url = os.getenv(_RESO_URL, "")
        self.token = os.getenv(_RESO_TOKEN, "")
        self.mls_id = os.getenv(_RESO_MLS_ID, "")
        self.mls_name = os.getenv("ORACLE_RESO_MLS_NAME", self.mls_id)
        self.page = int(os.getenv("ORACLE_RESO_PAGE", "500"))
        self.lookback_h = float(os.getenv("ORACLE_RESO_LOOKBACK_HOURS", "1"))

    @staticmethod
    def is_configured() -> bool:
        return all(os.getenv(k) for k in (_RESO_URL, _RESO_TOKEN, _RESO_MLS_ID))

    # -- RESO fetch ------------------------------------------------------- #
    async def fetch(self, *, since: str, skip: int = 0) -> Optional[dict]:
        params = {
            "$filter": f"ModificationTimestamp gt {since}",
            "$orderby": "ModificationTimestamp asc",
            "$top": str(self.page),
            "$skip": str(skip),
        }
        url = f"{self.base_url}?{urllib.parse.urlencode(params, safe=' :')}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        return await self._get_json(url, headers=headers, timeout=30)

    def normalize(self, raw: dict) -> dict:
        """Map a RESO Data-Dictionary Property record to oracle_mls_listings."""
        g = raw.get
        return {
            "mls_id": self.mls_id,
            "mls_number": str(g("ListingId") or g("ListingKey") or "").strip(),
            "address": (g("UnparsedAddress") or "").strip(),
            "city": (g("City") or "").strip(),
            "state_code": (g("StateOrProvince") or "")[:2].upper(),
            "zip_code": (g("PostalCode") or "").strip(),
            "county": (g("CountyOrParish") or "").strip(),
            "latitude": _num(g("Latitude")),
            "longitude": _num(g("Longitude")),
            "list_price": _num(g("ListPrice")) or 0.0,
            "orig_list_price": _num(g("OriginalListPrice")),
            "status": (g("StandardStatus") or "active").strip().lower().replace(" ", "_"),
            "property_type": (g("PropertySubType") or g("PropertyType") or "residential_1_4").strip(),
            "beds": _int(g("BedroomsTotal")),
            "baths_full": _int(g("BathroomsFull")),
            "baths_half": _int(g("BathroomsHalf")),
            "sqft": _int(g("LivingArea")),
            "lot_sqft": _int(g("LotSizeSquareFeet")),
            "year_built": _int(g("YearBuilt")),
            "hoa_monthly": _num(g("AssociationFee")),
            "days_on_market": _int(g("DaysOnMarket")),
            "list_date": _date(g("ListingContractDate") or g("OnMarketDate")),
            "close_date": _date(g("CloseDate")),
            "close_price": _num(g("ClosePrice")),
            "description": g("PublicRemarks"),
            "_modified": g("ModificationTimestamp"),
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

    async def sync_once(self) -> dict:
        """Pull the delta since the stored cursor, upsert, advance the cursor."""
        if not self.is_configured():
            return {"skipped": "RESO feed not configured"}
        tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
        if not tenant:
            return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}

        from tenancy import TenantContext, Role
        from db.connection import tenant_tx

        ctx = TenantContext(agent_id="periodic-listings", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
        fallback = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_h))

        upserted = 0
        max_modified: Optional[datetime] = None
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                "SELECT last_sync_at FROM mls_sync_status WHERE mls_id = $1", self.mls_id
            )
            since_dt = (row and row["last_sync_at"]) or fallback
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            since = since_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            skip = 0
            while True:
                payload = await self.fetch(since=since, skip=skip)
                batch = (payload or {}).get("value") or []
                if not batch:
                    break
                for raw in batch:
                    rec = self.normalize(raw)
                    if not rec["mls_number"]:
                        continue
                    await conn.execute(
                        self._UPSERT,
                        rec["mls_id"], rec["mls_number"], rec["address"], rec["city"],
                        rec["state_code"], rec["zip_code"], rec["county"], rec["latitude"],
                        rec["longitude"], rec["list_price"], rec["orig_list_price"], rec["status"],
                        rec["property_type"], rec["beds"], rec["baths_full"], rec["baths_half"],
                        rec["sqft"], rec["lot_sqft"], rec["year_built"], rec["hoa_monthly"],
                        rec["days_on_market"], rec["list_date"], rec["close_date"],
                        rec["close_price"], rec["description"],
                    )
                    upserted += 1
                    # Advance the delta cursor by the MAX ModificationTimestamp
                    # actually processed — NEVER wall-clock now(): a record modified
                    # between this fetch and the status write would be permanently
                    # skipped by the next run's `gt now()` filter (silent data loss).
                    m = _parse_reso_dt(rec.get("_modified"))
                    if m is not None and (max_modified is None or m > max_modified):
                        max_modified = m
                if len(batch) < self.page:
                    break
                skip += self.page

            # Cursor = newest record seen minus a small overlap; clamp so it never
            # moves backwards and never advances when nothing new arrived.
            cursor = since_dt
            if max_modified is not None:
                candidate = max_modified - timedelta(minutes=_CURSOR_OVERLAP_MIN)
                cursor = candidate if candidate > since_dt else since_dt

            await conn.execute(
                """
                INSERT INTO mls_sync_status
                    (mls_id, mls_name, feed_type, last_sync_at, listings_synced, sync_lag_minutes, updated_at)
                VALUES ($1, $2, 'RESO_Web_API', $4, $3, 0, now())
                ON CONFLICT (mls_id) DO UPDATE SET
                    mls_name = EXCLUDED.mls_name,
                    last_sync_at = EXCLUDED.last_sync_at,
                    listings_synced = mls_sync_status.listings_synced + EXCLUDED.listings_synced,
                    updated_at = now()
                """,
                self.mls_id, self.mls_name, upserted, cursor,
            )

        self._metrics["normalized"] += upserted
        return {"mls_id": self.mls_id, "since": since, "upserted": upserted}
