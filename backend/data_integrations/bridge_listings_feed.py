"""
data_integrations/bridge_listings_feed.py — Bridge Interactive (bridgedataoutput.com)
"new listings" delta feed → oracle_mls_listings.

Bridge Interactive is NOT RESO/OData, despite Bridge's field *names* following
the RESO Data Dictionary (`ListingKey`, `ModificationTimestamp`, `StandardStatus`,
...). Its transport is Bridge's own JSON REST protocol:

    GET https://api.bridgedataoutput.com/api/v2/{dataset}/listings
        ?access_token=...
        &limit=...&offset=...
        &ModificationTimestamp.gte=<ISO8601>
        &sortBy=ModificationTimestamp&order=asc

    -> {"success": true, "status": 200, "total": N, "bundle": [ {...}, ... ]}

`$filter`/`$top`/`$skip`/`@odata.nextLink` (the shape `listings_feed.py`
speaks) are all rejected by this endpoint (confirmed live: `$top` -> 400
"Invalid parameter"). That is the entire reason this is a separate module
instead of another entry in `ORACLE_RESO_FEEDS_JSON` — dropping a Bridge URL
into the RESO feed list would silently 400 on every page.

Configuration is deliberately a distinct ``ORACLE_BRIDGE_*`` namespace, not
``ORACLE_RESO_*`` — reusing the RESO names would imply this speaks the RESO
protocol, and it does not:

    ORACLE_BRIDGE_ENABLED=true
    ORACLE_BRIDGE_BASE_URL=https://api.bridgedataoutput.com/api/v2
    ORACLE_BRIDGE_DATASET=test                 # or test_sd / test_sf — the only
                                                # datasets this application is
                                                # currently authorized for; a
                                                # real board needs its own
                                                # separate Bridge approval even
                                                # with a valid platform token
                                                # (confirmed via live 401s on
                                                # fmls/miamire/har/mlspin/...)
    ORACLE_BRIDGE_ACCESS_TOKEN=<server token>  # backend-only; never ships to a browser
    ORACLE_BRIDGE_MLS_ID=bridge_dev
    ORACLE_BRIDGE_MLS_NAME=Bridge Developer Dataset
    ORACLE_BRIDGE_LOOKBACK_HOURS=1
    ORACLE_BRIDGE_PAGE=500
    ORACLE_BRIDGE_MAX_PAGES=200
    ORACLE_INGEST_TENANT_ID=<uuid>             # shared with the RESO feed

The ``test``/``test_sd``/``test_sf`` datasets are Bridge's own synthetic
developer data — not a real board, not real inventory. Every record this
module writes is tagged
``features.provenance.classification = "developer_listing_dataset"`` so it
can never be mistaken, downstream, for licensed MLS data
(``licensed_property_listing``, what `listings_feed.py` writes). Swapping in a
real, licensed Bridge dataset later means changing only the provenance
classification and the dataset/token config — not this module's shape.

Only documented Bridge endpoints are called. This module never scrapes MLS
member sites, consumer portals, or attempts to bypass provider access
controls (same rule as `listings_feed.py`).
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .base import DataIntegrationError, DataSource, RateLimiter, RetryConfig
from .mls_sink import reject_reason, upsert_mls_records_and_status

logger = logging.getLogger("oracle.di.bridge_listings")

_CURSOR_OVERLAP_MIN = 2


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None else None


def _date(v: Any) -> Optional[Any]:
    """oracle_mls_listings.list_date/close_date are typed `date` columns —
    asyncpg's date codec rejects a bare string, so this must return a real
    date object (same fix as listings_feed.py's identical helper)."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_bridge_dt(value: Any) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class BridgeFeedConfig:
    dataset: str
    access_token: str
    base_url: str = "https://api.bridgedataoutput.com/api/v2"
    mls_id: str = "bridge_dev"
    mls_name: str = "Bridge Developer Dataset"
    page_size: int = 500
    lookback_hours: float = 1.0
    max_pages: int = 200


def _bridge_config_from_env() -> tuple[Optional[BridgeFeedConfig], Optional[str]]:
    if os.getenv("ORACLE_BRIDGE_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None, "ORACLE_BRIDGE_ENABLED not set"
    dataset = os.getenv("ORACLE_BRIDGE_DATASET", "").strip()
    token = os.getenv("ORACLE_BRIDGE_ACCESS_TOKEN", "").strip()
    if not dataset or not token:
        return None, "ORACLE_BRIDGE_DATASET / ORACLE_BRIDGE_ACCESS_TOKEN unset"
    base_url = os.getenv("ORACLE_BRIDGE_BASE_URL", "").strip() or "https://api.bridgedataoutput.com/api/v2"
    return BridgeFeedConfig(
        dataset=dataset,
        access_token=token,
        base_url=base_url.rstrip("/"),
        mls_id=os.getenv("ORACLE_BRIDGE_MLS_ID", "").strip() or "bridge_dev",
        mls_name=os.getenv("ORACLE_BRIDGE_MLS_NAME", "").strip() or "Bridge Developer Dataset",
        # Bridge's own ceiling — confirmed live: limit=500 -> 400 "Maximum value
        # for limit is 200". RESO boards allow far larger pages; this cap is
        # Bridge-specific, hence the separate bound here instead of reusing
        # ORACLE_RESO_PAGE's 1-1000 range.
        page_size=_bounded_int(os.getenv("ORACLE_BRIDGE_PAGE"), 200, 1, 200),
        lookback_hours=_bounded_float(os.getenv("ORACLE_BRIDGE_LOOKBACK_HOURS"), 1.0, 0.05, 168.0),
        max_pages=_bounded_int(os.getenv("ORACLE_BRIDGE_MAX_PAGES"), 200, 1, 2000),
    ), None


class BridgeListingsFeed(DataSource):
    """Bridge Interactive `/api/v2/{dataset}/listings` delta puller → oracle_mls_listings."""

    source_name = "bridge_listings"

    def __init__(self, config: Optional[BridgeFeedConfig] = None, **kw: Any) -> None:
        super().__init__(
            rate_limiter=kw.get("rate_limiter") or RateLimiter(min_interval=0.5, jitter=0.3),
            retry_config=kw.get("retry_config") or RetryConfig(max_attempts=4),
            cache=kw.get("cache"),
        )
        if config is None:
            config, error = _bridge_config_from_env()
            if config is None:
                raise ValueError(error or "Bridge feed is not configured")
        self.config = config
        self.base_url = config.base_url
        self.token = config.access_token
        self.dataset = config.dataset
        self.mls_id = config.mls_id
        self.mls_name = config.mls_name
        self.page = config.page_size
        self.lookback_h = config.lookback_hours
        self.max_pages = config.max_pages

    def _cache_ttl(self) -> int:
        return 5 * 60

    @staticmethod
    def is_configured() -> bool:
        config, _error = _bridge_config_from_env()
        return config is not None

    async def _cached_page(self, *, since: str, offset: int) -> dict:
        if self._cache is None:
            from .cache import get_integration_cache

            self._cache = await get_integration_cache()

        async def fetch_page() -> dict:
            return await self.fetch(since=since, offset=offset) or {"bundle": []}

        return await self._cache.get_or_fetch(
            "mls",
            {"provider": "bridge", "mls_id": self.mls_id, "since": since, "offset": offset},
            fetch_page,
            ttl=self._cache_ttl(),
        )

    # -- Bridge fetch ------------------------------------------------------ #
    async def fetch(self, *, since: str, offset: int = 0) -> Optional[dict]:
        params = {
            "access_token": self.token,
            "limit": str(self.page),
            "offset": str(offset),
            "ModificationTimestamp.gte": since,
            "sortBy": "ModificationTimestamp",
            "order": "asc",
        }
        url = f"{self.base_url}/{self.dataset}/listings?{urllib.parse.urlencode(params)}"
        payload = await self._get_json(url, headers={"Accept": "application/json"}, timeout=30)
        if not isinstance(payload, dict) or not isinstance(payload.get("bundle", []), list):
            raise DataIntegrationError(f"Bridge feed {self.mls_id} returned an invalid page")
        if payload.get("success") is False:
            raise DataIntegrationError(f"Bridge feed {self.mls_id} reported success=false")
        return payload

    def normalize(self, raw: dict) -> dict:
        """Map a Bridge listing record into the exact canonical shape
        RESOListingsFeed.normalize() emits — Bridge's own field names already
        follow the RESO Data Dictionary, so most of this is a direct read."""
        g = raw.get
        listing_key = str(g("ListingKey") or g("ListingId") or "").strip()
        address = str(g("UnparsedAddress") or "").strip()
        state = str(g("StateOrProvince") or "").strip()
        price = _num(g("ListPrice"))
        parcel_number = str(g("ParcelNumber") or "").strip() or None
        modified = g("ModificationTimestamp")
        media = g("Media") or []
        photos = [
            str(m.get("MediaURL")) for m in media
            if isinstance(m, dict) and m.get("MediaURL")
        ][:100]
        return {
            "mls_id": self.mls_id,
            "mls_number": listing_key,
            "address": address,
            "city": str(g("City") or "").strip(),
            "state_code": state[:2].upper(),
            "zip_code": str(g("PostalCode") or "").strip(),
            "county": str(g("CountyOrParish") or "").strip(),
            "latitude": _num(g("Latitude")),
            "longitude": _num(g("Longitude")),
            "list_price": price if price is not None and price >= 0 else 0.0,
            "orig_list_price": _num(g("OriginalListPrice")),
            "status": str(g("StandardStatus") or g("MlsStatus") or "").lower().replace(" ", "_"),
            "property_type": str(g("PropertyType") or "").strip(),
            "beds": _int(g("BedroomsTotal")),
            "baths_full": _int(g("BathroomsFull")),
            "baths_half": _int(g("BathroomsHalf")),
            "sqft": _int(g("LivingArea")),
            "lot_sqft": _int(g("LotSizeSquareFeet")),
            "year_built": _int(g("YearBuilt")),
            "hoa_monthly": _num(g("AssociationFee")),
            "days_on_market": _int(g("DaysOnMarket")),
            "list_date": _date(g("ListingContractDate")),
            "close_date": _date(g("CloseDate")),
            "close_price": _num(g("ClosePrice")),
            "description": str(g("PublicRemarks") or "").strip() or None,
            "photos": photos,
            "features": {
                "source_kind": "developer_listing_dataset",
                "mls_id": self.mls_id,
                "listing_key": listing_key,
                "originating_system_key": str(g("OriginatingSystemKey") or "").strip(),
                "originating_system_name": str(g("OriginatingSystemName") or "").strip(),
                "parcel_number": parcel_number,
                "source_modified_at": str(modified or "").strip() or None,
                "matchable": bool(parcel_number or (address and g("PostalCode"))),
                "provenance": {
                    "classification": "developer_listing_dataset",
                    "provider": "Bridge Interactive",
                    "provider_id": self.mls_id,
                    "standard": "Bridge API v2",
                },
            },
            "_modified": modified,
        }

    @staticmethod
    def _reject_reason(rec: dict[str, Any]) -> Optional[str]:
        return reject_reason(rec)

    async def sync_once(self) -> dict:
        """Pull the delta since the stored cursor, upsert, advance the cursor.

        Mirrors RESOListingsFeed.sync_once()'s shape (same cursor semantics,
        same honest partial/succeeded reporting) but walks Bridge's own
        offset-based pagination instead of RESO's nextLink, and writes through
        the shared sink instead of inlining the upsert."""
        tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
        if not tenant:
            return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}

        from tenancy import TenantContext, Role
        from db.connection import tenant_tx

        ctx = TenantContext(agent_id="periodic-bridge-listings", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
        fallback = datetime.now(timezone.utc) - timedelta(hours=self.lookback_h)

        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                "SELECT last_sync_at FROM mls_sync_status WHERE mls_id = $1", self.mls_id
            )
        since_dt = (row and row["last_sync_at"]) or fallback
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        since = since_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        records: list[dict[str, Any]] = []
        rejected: dict[str, int] = {}
        max_modified: Optional[datetime] = None

        # Provider I/O intentionally happens outside a PostgreSQL transaction —
        # a slow provider must not hold an app connection or row locks.
        offset = 0
        exhausted = False
        pages = 0
        while pages < self.max_pages:
            payload = await self._cached_page(since=since, offset=offset)
            pages += 1
            batch = payload.get("bundle") or []
            for raw in batch:
                if not isinstance(raw, dict):
                    rejected["invalid_record"] = rejected.get("invalid_record", 0) + 1
                    continue
                rec = self.normalize(raw)
                reason = self._reject_reason(rec)
                if reason:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue
                records.append(rec)
                # Advance only from a valid source timestamp. A malformed value
                # can never move the durable checkpoint past unseen records.
                modified = _parse_bridge_dt(rec.get("_modified"))
                if modified is not None and (max_modified is None or modified > max_modified):
                    max_modified = modified

            if len(batch) < self.page:
                exhausted = True
                break
            offset += self.page

        cursor = since_dt
        if exhausted and max_modified is not None:
            candidate = max_modified - timedelta(minutes=_CURSOR_OVERLAP_MIN)
            cursor = candidate if candidate > since_dt else since_dt

        async with tenant_tx(ctx) as conn:
            upserted = await upsert_mls_records_and_status(
                conn,
                records=records,
                mls_id=self.mls_id,
                mls_name=self.mls_name,
                feed_type="Bridge_API_v2",
                last_sync_at=cursor,
                sync_lag_minutes=0,
                notes={
                    "state": "succeeded" if exhausted else "partial",
                    "pages": pages,
                    "rejected": rejected,
                    "dataset": self.dataset,
                },
            )

        self._metrics["normalized"] += upserted
        return {
            "mls_id": self.mls_id,
            "mls_name": self.mls_name,
            "state": "succeeded" if exhausted else "partial",
            "since": since,
            "pages": pages,
            "received": len(records) + sum(rejected.values()),
            "upserted": upserted,
            "rejected": rejected,
            "cursor_advanced": cursor > since_dt,
        }
