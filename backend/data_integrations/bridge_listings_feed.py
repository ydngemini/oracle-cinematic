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
    ORACLE_BRIDGE_DATASET=actris_ref           # test / test_sd / test_sf are
                                                # Bridge's synthetic developer
                                                # datasets; any other slug is a
                                                # board this application was
                                                # approved for and is tagged
                                                # licensed_property_listing.
    ORACLE_BRIDGE_ACCESS_TOKEN=<server token>  # backend-only; never ships to a browser
    ORACLE_BRIDGE_MLS_ID=actris
    ORACLE_BRIDGE_MLS_NAME=ACTRIS / Unlock MLS
    ORACLE_BRIDGE_LOOKBACK_HOURS=1
    ORACLE_BRIDGE_PAGE=500
    ORACLE_BRIDGE_MAX_PAGES=200
    ORACLE_BRIDGE_BACKFILL_MAX_PAGES=4000      # first-run full keyset walk ceiling
    ORACLE_INGEST_TENANT_ID=<uuid>             # shared with the RESO feed

The first sync for a feed (no ``mls_sync_status`` row) runs a one-time full
walk of the whole dataset by keyset pagination — Bridge caps ``offset`` at
10000, and a *reference* dataset has every ``ModificationTimestamp`` frozen, so
a plain delta would import nothing. Subsequent runs are normal deltas.

Provenance is decided by `mls_licensing`, which FAILS CLOSED: a dataset is
developer data unless an operator has explicitly declared it licensed and named
the agreement, and a provider *reference* dataset (``*_ref``, ``*_sample``)
can never be licensed however it is declared — it is frozen sample inventory.

This docstring previously said the opposite: that any dataset outside a
three-name developer list "is treated as a real, licensed Bridge feed (e.g.
``actris_ref``)". That was the bug. ``actris_ref`` is a reference dataset whose
every ``ModificationTimestamp`` is frozen in 2020, and 52,622 of its rows were
stamped ``licensed_property_listing`` on the strength of its name.

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
from .mls_sink import (
    record_sync_status,
    reject_reason,
    upsert_mls_records,
    upsert_mls_records_and_status,
)

logger = logging.getLogger("oracle.di.bridge_listings")

_CURSOR_OVERLAP_MIN = 2

# The developer-dataset list used to live here, alongside a rule that anything
# NOT in it was licensed. That comment ("e.g. `actris_ref`") is a record of the
# mistake: a per-board *reference* dataset was assumed to be licensed
# inventory, and 52,622 rows of frozen 2020 sample data were stamped as live.
#
# The list and the decision now live in mls_licensing, which fails closed. It
# is deliberately not re-declared here — two copies is how this drifted.


def _provenance_for_dataset(dataset: str) -> tuple[str, str]:
    """(source_kind, classification) for a Bridge dataset.

    Delegates to mls_licensing, which fails closed. This used to be a denylist
    of three developer names that defaulted to "licensed", so any other slug —
    a typo, a new sample set, a per-board *reference* dataset — became real
    licensed inventory with nobody deciding that. It is kept as a thin wrapper
    because the provenance block it stamps into features is read by exports and
    by prose downstream; the decision itself now lives in one place.
    """
    from mls_licensing import classify_from_env
    licence = classify_from_env(dataset)
    return licence.source_kind, licence.classification


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


_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


def _int(v: Any) -> Optional[int]:
    """Parse to int, but drop values outside PostgreSQL int4 range — the target
    columns (beds, sqft, lot_sqft, ...) are int4. A lot size of 2.37e9 sq ft
    (~54k acres) is an acreage-field-bleed artifact, not real inventory; NULL is
    more honest than letting one bad row abort a 50k-record backfill."""
    n = _num(v)
    if n is None:
        return None
    i = int(n)
    return i if _INT32_MIN <= i <= _INT32_MAX else None


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
    # First-run full backfill walks the whole dataset by keyset pagination;
    # 52k rows at 200/page is ~264 pages, so this ceiling is much higher than
    # the per-delta one.
    backfill_max_pages: int = 4000


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
        backfill_max_pages=_bounded_int(
            os.getenv("ORACLE_BRIDGE_BACKFILL_MAX_PAGES"), 4000, 1, 100_000
        ),
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
        # NOT cached: recomputed on read so revoking a declaration downgrades
        # the very next sync instead of after a restart.
        self.mls_id = config.mls_id
        self.mls_name = config.mls_name
        self.page = config.page_size
        self.lookback_h = config.lookback_hours
        self.max_pages = config.max_pages
        self.backfill_max_pages = config.backfill_max_pages

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
    async def fetch(
        self,
        *,
        since: Optional[str] = None,
        offset: int = 0,
        after_key: Optional[str] = None,
    ) -> Optional[dict]:
        """One Bridge `/listings` page.

        Delta mode (the default): ``ModificationTimestamp.gte=since`` sorted by
        ModificationTimestamp, walked with ``offset``.

        Keyset mode (``after_key`` not None): sorted by ``ListingKey`` and
        walked with ``ListingKey.gt`` instead of ``offset``. Bridge caps
        ``offset`` at 10000 (offset+limit must stay under its result window),
        so a full backfill of a >10k dataset can only be done by keyset. Pass
        ``after_key=""`` for the first keyset page. ``since`` is omitted here
        unless given, so a backfill sees every record regardless of its
        (possibly frozen) ModificationTimestamp.
        """
        params = {
            "access_token": self.token,
            "limit": str(self.page),
            "order": "asc",
        }
        if after_key is not None:
            params["sortBy"] = "ListingKey"
            if after_key:
                params["ListingKey.gt"] = after_key
        else:
            params["sortBy"] = "ModificationTimestamp"
            params["offset"] = str(offset)
        if since is not None:
            params["ModificationTimestamp.gte"] = since
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
                "source_kind": _provenance_for_dataset(self.dataset)[0],
                "mls_id": self.mls_id,
                "listing_key": listing_key,
                "originating_system_key": str(g("OriginatingSystemKey") or "").strip(),
                "originating_system_name": str(g("OriginatingSystemName") or "").strip(),
                "parcel_number": parcel_number,
                "source_modified_at": str(modified or "").strip() or None,
                "matchable": bool(parcel_number or (address and g("PostalCode"))),
                "provenance": {
                    "classification": self._license_classification(),
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

    def _license_classification(self) -> str:
        """The licence these rows are written under — fail-closed, recomputed
        rather than cached, so revoking a declaration downgrades the next run."""
        from mls_licensing import classify_from_env
        return classify_from_env(self.dataset).classification

    async def _backfill_once(self, ctx: Any, tenant_tx: Any) -> dict:
        """One-time full walk of the whole dataset by keyset pagination.

        Runs on the first sync for this feed (no `mls_sync_status` row). Bridge
        caps `offset` at 10000, so a dataset larger than that can only be walked
        by sorting on `ListingKey` and advancing `ListingKey.gt` page to page.
        No `ModificationTimestamp` filter is applied, so records with a frozen
        or absent modification timestamp (as in a RESO *reference* dataset) are
        still ingested. Records are flushed to the DB every few pages so a large
        dataset never has to sit fully in memory, and the durable cursor is only
        advanced to "now" (handing subsequent runs back to the delta path) once
        the walk actually reaches the end.
        """
        _FLUSH_EVERY_PAGES = 10

        # Resume, rather than restart.
        #
        # The walk position used to live only in this function's locals, so a
        # worker restart, a deploy, or simply hitting backfill_max_pages threw
        # away every page already fetched. The advice logged on that path was
        # "delete the mls_sync_status row to resume" — which would also discard
        # the feed's licence, health and error history, and (until this was
        # fixed) hid the feed's listings from search entirely.
        #
        # 0110 added backfill_cursor_key and backfill_records for exactly this
        # and nothing wrote them. A real board is hundreds of thousands of rows;
        # a backfill that cannot survive a deploy is a backfill that may never
        # finish.
        after_key = ""  # "" → first keyset page
        resumed_from = ""
        already_written = 0
        async with tenant_tx(ctx) as conn:
            prior = await conn.fetchrow(
                "SELECT backfill_cursor_key, backfill_records, backfill_complete "
                "  FROM mls_sync_status WHERE mls_id = $1",
                self.mls_id,
            )
        prior_row = dict(prior) if prior else {}
        if not prior_row.get("backfill_complete") and prior_row.get("backfill_cursor_key"):
            after_key = resumed_from = str(prior_row["backfill_cursor_key"])
            already_written = int(prior_row.get("backfill_records") or 0)
            logger.info(
                "Bridge backfill %s resuming after ListingKey %s (%d rows already written)",
                self.mls_id, after_key, already_written,
            )

        pages = 0
        total_upserted = 0
        received = 0
        rejected: dict[str, int] = {}
        buffer: list[dict[str, Any]] = []
        exhausted = False

        async def flush(cursor_key: str = "") -> None:
            """Write the batch AND the position it reached, in one transaction.

            Both in the same transaction on purpose: a cursor saved without its
            rows would skip records permanently on resume, and rows saved
            without their cursor merely re-fetch a page. If only one can be
            true, it must be the one that loses time rather than data.
            """
            nonlocal buffer, total_upserted
            if not buffer and not cursor_key:
                return
            async with tenant_tx(ctx) as conn:
                if buffer:
                    total_upserted += await upsert_mls_records(
                        conn, buffer, license_classification=self._license_classification())
                if cursor_key:
                    # UPSERT, not UPDATE.
                    #
                    # On the very first sync there IS no status row — that is
                    # precisely why sync_once chose the backfill branch. An
                    # UPDATE therefore matched zero rows, so run 1 of a 264-page
                    # walk killed at page 30 saved nothing and run 2 started
                    # over. Resume only began working from run 3, which on a
                    # large board with a daily deploy means it may never finish.
                    await conn.execute(
                        "INSERT INTO mls_sync_status "
                        "  (mls_id, mls_name, feed_type, provider, dataset, "
                        "   backfill_cursor_key, backfill_records, backfill_complete) "
                        "VALUES ($1, $4, 'Bridge_API_v2', 'bridge', $5, $2, $3, false) "
                        "ON CONFLICT (mls_id) DO UPDATE SET "
                        "  backfill_cursor_key = EXCLUDED.backfill_cursor_key, "
                        "  backfill_records = EXCLUDED.backfill_records, "
                        "  updated_at = now()",
                        self.mls_id, cursor_key, already_written + total_upserted,
                        self.mls_name, self.dataset,
                    )
            buffer = []

        while pages < self.backfill_max_pages:
            payload = await self.fetch(after_key=after_key)
            pages += 1
            batch = (payload or {}).get("bundle") or []
            if not batch:
                exhausted = True
                break

            last_key: Optional[str] = None
            for raw in batch:
                received += 1
                if not isinstance(raw, dict):
                    rejected["invalid_record"] = rejected.get("invalid_record", 0) + 1
                    continue
                key = str(raw.get("ListingKey") or "").strip()
                if key:
                    last_key = key
                rec = self.normalize(raw)
                reason = self._reject_reason(rec)
                if reason:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue
                buffer.append(rec)

            if pages % _FLUSH_EVERY_PAGES == 0:
                await flush(cursor_key=last_key or after_key)

            if len(batch) < self.page:
                exhausted = True
                break
            if last_key is None or last_key == after_key:
                # No key to advance past on a full page — stop rather than
                # refetch the same page forever.
                logger.warning(
                    "Bridge backfill %s: page %d has no advanceable ListingKey; "
                    "stopping walk early", self.mls_id, pages
                )
                break
            after_key = last_key

        await flush(cursor_key=after_key)

        if not exhausted:
            logger.warning(
                "Bridge backfill %s paused after %d pages (max %d) — "
                "%d rows this run, %d total; the next run resumes from "
                "ListingKey %s automatically",
                self.mls_id, pages, self.backfill_max_pages, total_upserted,
                already_written + total_upserted, after_key or "(start)",
            )

        state = "succeeded" if exhausted else "partial"
        # §37: one structured line per run. Feed, mode, outcome, counts, cursor
        # movement — enough to answer "what did this sync do?" without reading
        # the database. Deliberately NOT logged: the access token, the
        # authorization header, whole provider payloads, or listing remarks.
        logger.info(
            "mls_sync feed=%s provider=bridge mode=backfill state=%s pages=%d "
            "received=%d accepted=%d rejected=%d upserted=%d rows_total=%d "
            "resumed=%s cursor_advanced=%s licence=%s",
            self.mls_id, state, pages, received,
            received - sum(rejected.values()), sum(rejected.values()),
            total_upserted, already_written + total_upserted,
            bool(resumed_from), bool(after_key), self._license_classification(),
        )
        # Advance to "now" only on a completed walk; otherwise keep the cursor
        # in the past so the next run's delta re-checks recent records (a
        # partial walk still leaves a status row, so it will not re-backfill).
        cursor = (
            datetime.now(timezone.utc)
            if exhausted
            else datetime.now(timezone.utc) - timedelta(hours=self.lookback_h)
        )
        async with tenant_tx(ctx) as conn:
            await record_sync_status(
                conn,
                mls_id=self.mls_id,
                mls_name=self.mls_name,
                feed_type="Bridge_API_v2",
                last_sync_at=cursor,
                listings_synced=total_upserted,
                sync_lag_minutes=0,
                notes={
                    "state": state,
                    "mode": "backfill",
                    "pages": pages,
                    "rejected": rejected,
                    "dataset": self.dataset,
                    "resumed_from": resumed_from or None,
                    "rows_total": already_written + total_upserted,
                },
                provider="bridge",
                dataset=self.dataset,
                succeeded=exhausted,
                # Only a walk that ran out of pages is a completed backfill.
                # A partial walk must keep the feed BACKFILLING, because half
                # a board looks exactly like a whole board to a searcher.
                backfill_complete=True if exhausted else None,
            )

        self._metrics["normalized"] += total_upserted
        return {
            "mls_id": self.mls_id,
            "mls_name": self.mls_name,
            "state": state,
            "mode": "backfill",
            "pages": pages,
            "received": received,
            "upserted": total_upserted,
            "rejected": rejected,
            "cursor_advanced": True,
        }

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
                "SELECT last_sync_at, backfill_complete "
                "  FROM mls_sync_status WHERE mls_id = $1",
                self.mls_id,
            )

        # Backfill when nothing has ever been pulled — and ALSO when a previous
        # backfill did not finish.
        #
        # This used to key on `row is None` alone, which meant a partial walk
        # was never resumed: the partial run wrote a status row, and every
        # later run therefore took the delta path. A delta only sees records
        # modified within `lookback_hours`, which for a reference dataset with
        # frozen timestamps is none of them — so a backfill interrupted at page
        # 30 of 264 silently stayed at page 30 forever while reporting success.
        #
        # A plain delta would also miss the remainder on a live board: those
        # records were modified before the lookback window and will never
        # reappear in it.
        # dict() first: asyncpg Records raise KeyError on a missing column, and
        # a caller supplying a narrower row should get the safe answer (run the
        # backfill) rather than an exception.
        if row is None or not dict(row).get("backfill_complete"):
            return await self._backfill_once(ctx, tenant_tx)

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
                provider="bridge",
                dataset=self.dataset,
                succeeded=exhausted,
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
