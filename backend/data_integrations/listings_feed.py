"""
data_integrations/listings_feed.py — fast-moving MLS "new listings" delta feed.

This is the hourly counterpart to the slow parcel harvest. Listings change by the
minute, so this pulls only the DELTA — records whose ModificationTimestamp is
newer than the last successful sync — via the RESO Web API (the MLS industry
standard OData feed). Results upsert into ``oracle_mls_listings`` and the cursor
lives in ``mls_sync_status.last_sync_at`` (DB-persisted, so it survives restarts
and never re-pulls the whole feed).

No feed credentials are committed anywhere. The task is configuration-gated and
skips cleanly when unset, exactly like the parcel harvest skips without a tenant.
One legacy feed can still be configured with the original variables:

    ORACLE_RESO_URL=https://api.<mls>.org/RESO/OData/Property   # the Property resource
    ORACLE_RESO_TOKEN=<bearer token>
    ORACLE_RESO_MLS_ID=<board id, e.g. "crmls">
    ORACLE_RESO_MLS_NAME="California Regional MLS"   # optional label
    ORACLE_RESO_LOOKBACK_HOURS=1                     # first-run / null-cursor window
    ORACLE_RESO_PAGE=500                             # $top per page
    ORACLE_INGEST_TENANT_ID=<uuid>                  # platform-admin ctx for the write

Multiple authorized feeds can be combined with ``ORACLE_RESO_FEEDS_JSON``. The
JSON value itself belongs in Key Vault, not source control:

    [
      {"id":"bright","name":"Bright MLS","url":"https://.../Property",
       "token_env":"ORACLE_RESO_TOKEN_BRIGHT"},
      {"id":"crmls","name":"CRMLS","url":"https://.../Property",
       "token_env":"ORACLE_RESO_TOKEN_CRMLS"}
    ]

Only licensed RESO endpoints are accepted. This module never scrapes MLS member
sites, consumer portals, or attempts to bypass provider access controls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .base import DataIntegrationError, DataSource, RateLimiter, RetryConfig

_RESO_URL = "ORACLE_RESO_URL"
_RESO_TOKEN = "ORACLE_RESO_TOKEN"
_RESO_MLS_ID = "ORACLE_RESO_MLS_ID"
_RESO_FEEDS_JSON = "ORACLE_RESO_FEEDS_JSON"
_RESO_ALLOWED_HOSTS = "ORACLE_RESO_ALLOWED_HOSTS"
_FEED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_MAX_CONFIGURED_FEEDS = 50
_FORBIDDEN_AGGREGATOR_IDS = frozenset({"rentcast", "zillow", "realtor", "redfin"})
logger = logging.getLogger("oracle.di.reso_listings")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


@dataclass(frozen=True)
class RESOFeedConfig:
    """Credential-bearing runtime configuration for one licensed MLS board."""

    mls_id: str
    mls_name: str
    url: str
    token: str
    page_size: int = 500
    lookback_hours: float = 1.0
    max_pages: int = 200


def _feed_config(raw: dict[str, Any], *, position: int) -> tuple[Optional[RESOFeedConfig], Optional[str]]:
    mls_id = str(raw.get("id") or raw.get("mls_id") or "").strip().lower()
    url = str(raw.get("url") or "").strip()
    token_env = str(raw.get("token_env") or "").strip()
    token = str(raw.get("token") or (os.getenv(token_env, "") if token_env else "")).strip()
    if raw.get("enabled") is False:
        return None, None
    if not _FEED_ID_RE.fullmatch(mls_id):
        return None, f"feed {position}: id must match {_FEED_ID_RE.pattern}"
    if mls_id in _FORBIDDEN_AGGREGATOR_IDS:
        return None, f"feed {mls_id}: third-party listing aggregators are not allowed"
    parsed = urllib.parse.urlsplit(url)
    allow_http = os.getenv("ORACLE_ENV", "dev").strip().lower() in {
        "dev", "development", "local", "test",
    }
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}) or not parsed.hostname:
        return None, f"feed {mls_id}: URL must use {'HTTP(S)' if allow_http else 'HTTPS'}"
    if not allow_http:
        allowed_hosts = {
            host.strip().lower()
            for host in os.getenv(_RESO_ALLOWED_HOSTS, "").split(",")
            if host.strip()
        }
        if not allowed_hosts:
            return None, (
                f"feed {mls_id}: {_RESO_ALLOWED_HOSTS} must explicitly authorize "
                "the direct MLS endpoint host"
            )
        if parsed.hostname.lower() not in allowed_hosts:
            return None, f"feed {mls_id}: endpoint host is not in {_RESO_ALLOWED_HOSTS}"
    if not token:
        return None, f"feed {mls_id}: bearer token is not configured"
    return RESOFeedConfig(
        mls_id=mls_id,
        mls_name=str(raw.get("name") or raw.get("mls_name") or mls_id).strip()[:160] or mls_id,
        url=url,
        token=token,
        page_size=_bounded_int(raw.get("page_size"), 500, 1, 5000),
        lookback_hours=_bounded_float(raw.get("lookback_hours"), 1.0, 0.25, 720.0),
        max_pages=_bounded_int(raw.get("max_pages"), 200, 1, 10_000),
    ), None


def load_reso_feed_configs() -> tuple[list[RESOFeedConfig], list[str]]:
    """Load unique feeds without exposing tokens in errors, logs, or metrics."""
    raw_feeds: list[dict[str, Any]] = []
    errors: list[str] = []
    configured = os.getenv(_RESO_FEEDS_JSON, "").strip()
    if configured:
        try:
            decoded = json.loads(configured)
            if not isinstance(decoded, list):
                raise ValueError("must be a JSON array")
            raw_feeds.extend(item for item in decoded[:_MAX_CONFIGURED_FEEDS] if isinstance(item, dict))
            if len(decoded) > _MAX_CONFIGURED_FEEDS:
                errors.append(f"only the first {_MAX_CONFIGURED_FEEDS} feeds are allowed")
            if len(raw_feeds) != min(len(decoded), _MAX_CONFIGURED_FEEDS):
                errors.append("every feed entry must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{_RESO_FEEDS_JSON}: {exc}")

    # Preserve the original single-feed configuration and allow it to coexist
    # with JSON feeds when its board id is unique.
    if any(os.getenv(key, "").strip() for key in (_RESO_URL, _RESO_TOKEN, _RESO_MLS_ID)):
        raw_feeds.append({
            "id": os.getenv(_RESO_MLS_ID, ""),
            "name": os.getenv("ORACLE_RESO_MLS_NAME", ""),
            "url": os.getenv(_RESO_URL, ""),
            "token": os.getenv(_RESO_TOKEN, ""),
            "page_size": os.getenv("ORACLE_RESO_PAGE", "500"),
            "lookback_hours": os.getenv("ORACLE_RESO_LOOKBACK_HOURS", "1"),
            "max_pages": os.getenv("ORACLE_RESO_MAX_PAGES", "200"),
        })

    feeds: list[RESOFeedConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_feeds, start=1):
        feed, error = _feed_config(raw, position=index)
        if error:
            errors.append(error)
            continue
        if feed is None:
            continue
        if feed.mls_id in seen:
            errors.append(f"feed {feed.mls_id}: duplicate id ignored")
            continue
        seen.add(feed.mls_id)
        feeds.append(feed)
    return feeds, errors


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

    def __init__(self, config: Optional[RESOFeedConfig] = None, **kw: Any) -> None:
        # Listings move fast — a tighter limiter than the slow integrations.
        super().__init__(
            rate_limiter=kw.get("rate_limiter") or RateLimiter(min_interval=0.5, jitter=0.3),
            retry_config=kw.get("retry_config") or RetryConfig(max_attempts=4),
            cache=kw.get("cache"),
        )
        if config is None:
            legacy, errors = _feed_config({
                "id": os.getenv(_RESO_MLS_ID, ""),
                "name": os.getenv("ORACLE_RESO_MLS_NAME", ""),
                "url": os.getenv(_RESO_URL, ""),
                "token": os.getenv(_RESO_TOKEN, ""),
                "page_size": os.getenv("ORACLE_RESO_PAGE", "500"),
                "lookback_hours": os.getenv("ORACLE_RESO_LOOKBACK_HOURS", "1"),
                "max_pages": os.getenv("ORACLE_RESO_MAX_PAGES", "200"),
            }, position=1)
            if legacy is None:
                raise ValueError(errors or "RESO feed is not configured")
            config = legacy
        self.config = config
        self.base_url = config.url
        self.token = config.token
        self.mls_id = config.mls_id
        self.mls_name = config.mls_name
        self.page = config.page_size
        self.lookback_h = config.lookback_hours
        self.max_pages = config.max_pages
        base = urllib.parse.urlsplit(self.base_url)
        self._origin = (base.scheme.lower(), (base.hostname or "").lower(), base.port)

    def _cache_ttl(self) -> int:
        return 5 * 60

    async def _cached_page(
        self, *, since: str, skip: int, next_url: Optional[str] = None
    ) -> dict:
        if self._cache is None:
            from .cache import get_integration_cache

            self._cache = await get_integration_cache()

        async def fetch_page() -> dict:
            return await self.fetch(since=since, skip=skip, next_url=next_url) or {"value": []}

        page_ref = hashlib.sha256((next_url or f"skip:{skip}").encode("utf-8")).hexdigest()
        return await self._cache.get_or_fetch(
            "mls",
            {"provider": "reso", "mls_id": self.mls_id, "since": since, "page": page_ref},
            fetch_page,
            ttl=self._cache_ttl(),
        )

    @staticmethod
    def is_configured() -> bool:
        feeds, _errors = load_reso_feed_configs()
        return bool(feeds)

    def _safe_next_url(self, value: Any) -> Optional[str]:
        if not value:
            return None
        candidate = urllib.parse.urljoin(self.base_url, str(value))
        parsed = urllib.parse.urlsplit(candidate)
        origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)
        if origin != self._origin:
            raise DataIntegrationError(
                f"RESO feed {self.mls_id} returned a cross-origin nextLink"
            )
        return candidate

    # -- RESO fetch ------------------------------------------------------- #
    async def fetch(
        self, *, since: str, skip: int = 0, next_url: Optional[str] = None
    ) -> Optional[dict]:
        if next_url:
            url = self._safe_next_url(next_url)
        else:
            params = {
                "$filter": f"ModificationTimestamp gt {since}",
                "$orderby": "ModificationTimestamp asc",
                "$top": str(self.page),
                "$skip": str(skip),
            }
            url = f"{self.base_url}?{urllib.parse.urlencode(params, safe=' :')}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        payload = await self._get_json(url, headers=headers, timeout=30)
        if not isinstance(payload, dict) or not isinstance(payload.get("value", []), list):
            raise DataIntegrationError(f"RESO feed {self.mls_id} returned an invalid OData page")
        return payload

    def normalize(self, raw: dict) -> dict:
        """Map a RESO Data-Dictionary Property record to oracle_mls_listings."""
        g = raw.get
        listing_key = str(g("ListingKey") or g("ListingId") or "").strip()
        address = str(g("UnparsedAddress") or "").strip()
        if not address:
            address = " ".join(
                str(value).strip()
                for value in (
                    g("StreetNumber"), g("StreetDirPrefix"), g("StreetName"),
                    g("StreetSuffix"), g("UnitNumber"),
                )
                if value is not None and str(value).strip()
            )
        modified = g("ModificationTimestamp")
        parcel_number = str(
            g("ParcelNumber") or g("TaxParcelIdentificationNumber") or ""
        ).strip()
        media = g("Media") if isinstance(g("Media"), list) else []
        photos = [
            str(item.get("MediaURL")).strip()
            for item in media
            if isinstance(item, dict)
            and str(item.get("MediaURL") or "").strip().startswith("https://")
        ][:100]
        price = _num(g("ListPrice"))
        return {
            "mls_id": self.mls_id,
            "mls_number": str(g("ListingId") or listing_key).strip(),
            "address": address,
            "city": str(g("City") or "").strip(),
            "state_code": str(g("StateOrProvince") or "").strip()[:2].upper(),
            "zip_code": str(g("PostalCode") or "").strip()[:10],
            "county": str(g("CountyOrParish") or "").strip(),
            "latitude": _num(g("Latitude")),
            "longitude": _num(g("Longitude")),
            "list_price": price if price is not None and price >= 0 else 0.0,
            "orig_list_price": _num(g("OriginalListPrice")),
            "status": str(g("StandardStatus") or "active").strip().lower().replace(" ", "_"),
            "property_type": str(
                g("PropertySubType") or g("PropertyType") or "residential_1_4"
            ).strip(),
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
            "photos": photos,
            "features": {
                "source_kind": "licensed_mls",
                "mls_id": self.mls_id,
                "listing_key": listing_key,
                "originating_system_key": str(g("OriginatingSystemKey") or "").strip(),
                "originating_system_name": str(g("OriginatingSystemName") or "").strip(),
                "parcel_number": parcel_number,
                "source_modified_at": str(modified or "").strip() or None,
                "matchable": bool(parcel_number or (address and g("PostalCode"))),
                "provenance": {
                    "classification": "licensed_property_listing",
                    "provider": self.mls_name,
                    "provider_id": self.mls_id,
                    "standard": "RESO Web API",
                },
            },
            "_modified": modified,
        }

    @staticmethod
    def _reject_reason(rec: dict[str, Any]) -> Optional[str]:
        if not rec.get("mls_number"):
            return "missing_listing_key"
        state = str(rec.get("state_code") or "")
        if len(state) != 2 or not state.isalpha():
            return "invalid_state"
        latitude, longitude = rec.get("latitude"), rec.get("longitude")
        if latitude is not None and not -90 <= latitude <= 90:
            return "invalid_latitude"
        if longitude is not None and not -180 <= longitude <= 180:
            return "invalid_longitude"
        return None

    _UPSERT = """
        INSERT INTO oracle_mls_listings (
            mls_id, mls_number, address, city, state_code, zip_code, county,
            latitude, longitude, list_price, orig_list_price, status, property_type,
            beds, baths_full, baths_half, sqft, lot_sqft, year_built, hoa_monthly,
            days_on_market, list_date, close_date, close_price, description,
            photos, features, last_updated
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
            $21,$22,$23,$24,$25,$26,$27::jsonb,now()
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
            photos=EXCLUDED.photos, features=EXCLUDED.features,
            last_updated=now()
    """

    async def sync_once(self) -> dict:
        """Pull the delta since the stored cursor, upsert, advance the cursor."""
        tenant = os.getenv("ORACLE_INGEST_TENANT_ID", "")
        if not tenant:
            return {"skipped": "ORACLE_INGEST_TENANT_ID unset"}

        from tenancy import TenantContext, Role
        from db.connection import tenant_tx

        ctx = TenantContext(agent_id="periodic-listings", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
        fallback = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_h))

        records: list[dict[str, Any]] = []
        rejected: dict[str, int] = {}
        max_modified: Optional[datetime] = None
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                "SELECT last_sync_at FROM mls_sync_status WHERE mls_id = $1", self.mls_id
            )
        since_dt = (row and row["last_sync_at"]) or fallback
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        since = since_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Provider I/O intentionally happens outside a PostgreSQL transaction.
        # A slow board must not hold an app connection or row locks for minutes.
        skip = 0
        next_url: Optional[str] = None
        seen_pages: set[str] = set()
        exhausted = False
        pages = 0
        while pages < self.max_pages:
            payload = await self._cached_page(since=since, skip=skip, next_url=next_url)
            pages += 1
            batch = payload.get("value") or []
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
                modified = _parse_reso_dt(rec.get("_modified"))
                if modified is not None and (max_modified is None or modified > max_modified):
                    max_modified = modified

            raw_next = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
            if raw_next:
                safe_next = self._safe_next_url(raw_next)
                fingerprint = hashlib.sha256(str(safe_next).encode("utf-8")).hexdigest()
                if fingerprint in seen_pages:
                    raise DataIntegrationError(f"RESO feed {self.mls_id} repeated nextLink")
                seen_pages.add(fingerprint)
                next_url = safe_next
                continue
            if len(batch) < self.page:
                exhausted = True
                break
            skip += self.page
            next_url = None

        upserted = 0
        async with tenant_tx(ctx) as conn:
            for rec in records:
                await conn.execute(
                    self._UPSERT,
                    rec["mls_id"], rec["mls_number"], rec["address"], rec["city"],
                    rec["state_code"], rec["zip_code"], rec["county"], rec["latitude"],
                    rec["longitude"], rec["list_price"], rec["orig_list_price"], rec["status"],
                    rec["property_type"], rec["beds"], rec["baths_full"], rec["baths_half"],
                    rec["sqft"], rec["lot_sqft"], rec["year_built"], rec["hoa_monthly"],
                    rec["days_on_market"], rec["list_date"], rec["close_date"],
                    rec["close_price"], rec["description"], rec["photos"],
                    json.dumps(rec["features"], separators=(",", ":")),
                )
                upserted += 1

            # Cursor = newest record seen minus a small overlap; clamp so it never
            # moves backwards. A capped/incomplete traversal does not advance the
            # cursor; the next run safely retries the same idempotent window.
            cursor = since_dt
            if exhausted and max_modified is not None:
                candidate = max_modified - timedelta(minutes=_CURSOR_OVERLAP_MIN)
                cursor = candidate if candidate > since_dt else since_dt

            await conn.execute(
                """
                INSERT INTO mls_sync_status
                    (mls_id, mls_name, feed_type, last_sync_at, listings_synced,
                     sync_lag_minutes, notes, updated_at)
                VALUES ($1, $2, 'RESO_Web_API', $4, $3, 0, $5, now())
                ON CONFLICT (mls_id) DO UPDATE SET
                    mls_name = EXCLUDED.mls_name,
                    last_sync_at = EXCLUDED.last_sync_at,
                    listings_synced = mls_sync_status.listings_synced + EXCLUDED.listings_synced,
                    sync_lag_minutes = EXCLUDED.sync_lag_minutes,
                    notes = EXCLUDED.notes,
                    updated_at = now()
                """,
                self.mls_id, self.mls_name, upserted, cursor,
                json.dumps({
                    "state": "succeeded" if exhausted else "partial",
                    "pages": pages,
                    "rejected": rejected,
                }, separators=(",", ":")),
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


class RESOListingsAggregator:
    """Run every authorized board independently and return an honest roll-up."""

    @staticmethod
    def is_configured() -> bool:
        return RESOListingsFeed.is_configured()

    async def sync_once(self) -> dict[str, Any]:
        feeds, configuration_errors = load_reso_feed_configs()
        if not feeds:
            return {
                "skipped": "no valid RESO feed configured",
                "configuration_errors": configuration_errors,
            }
        concurrency = _bounded_int(
            os.getenv("ORACLE_RESO_CONCURRENCY", "3"), 3, 1, 10
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def run(feed: RESOFeedConfig) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await RESOListingsFeed(feed).sync_once()
                except Exception as exc:  # noqa: BLE001 - one board must not sink the others
                    logger.error("RESO board %s sync failed: %s", feed.mls_id, exc)
                    return {
                        "mls_id": feed.mls_id,
                        "mls_name": feed.mls_name,
                        "state": "failed",
                        "error": str(exc)[:240],
                    }

        results = await asyncio.gather(*(run(feed) for feed in feeds))
        failures = [row["mls_id"] for row in results if row.get("state") == "failed"]
        partial = [row["mls_id"] for row in results if row.get("state") == "partial"]
        state = (
            "failed" if len(failures) == len(results)
            else "partial" if failures or partial or configuration_errors
            else "succeeded"
        )
        return {
            "state": state,
            "feeds": results,
            "feed_count": len(results),
            "upserted": sum(int(row.get("upserted") or 0) for row in results),
            "failed_feeds": failures,
            "partial_feeds": partial,
            "configuration_errors": configuration_errors,
        }
