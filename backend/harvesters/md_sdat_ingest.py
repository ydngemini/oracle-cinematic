"""
Maryland SDAT Harvester — async Playwright ingest for the 4-State data expansion.

Navigates the Maryland State Department of Assessments and Taxation (SDAT) Open
Data portal, downloads the raw residential property export (CSV), maps each row
to the unified `PropertyRecord` schema, and batch-inserts the result into the
RLS-scoped `leads` table via the hardened asyncpg pool (decision 007).

Design notes
------------
* Conforms to the existing `PropertyHarvester` ABC (`property_adapter.py`) so the
  multi-state Scout pipeline can treat it like any other source.
* Maryland's Open Data lives on a Socrata instance, which exposes every dataset
  as a stable CSV endpoint: `/api/views/{id}/rows.csv?accessType=DOWNLOAD`. We
  let Playwright open the portal page and resolve that export href from the DOM
  (so we follow the *current* published dataset), then pull the bytes through the
  browser's APIRequestContext — which gives us real HTTP status codes for
  rate-limit handling. Both the portal URL and a direct export URL are
  env-overridable; the exact dataset slug / column headers below are the pieces
  most likely to need tuning against the live portal.
* Rate limiting + retry are first-class: a min-interval throttle with jitter
  between every request, plus exponential backoff that treats HTTP 429/503 and
  navigation timeouts as retryable. The goal is to never trip the state's WAF.
* `tenancy` and `db.connection` transitively import FastAPI, so they are imported
  lazily inside `_persist()` — this module stays importable in a bare scraper
  environment that only has Playwright + asyncpg.

Run:  ORACLE_DB_PASSWORD=... ORACLE_INGEST_TENANT_ID=<uuid> \\
      python -m backend.harvesters.md_sdat_ingest 21201 21043
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import os
import random
import time
from typing import Optional

from .base import persist_leads
from .property_adapter import PropertyHarvester, PropertyRecord

logger = logging.getLogger("oracle.harvester.md_sdat")

STATE = "MD"

# --- Portal / dataset config (env-overridable; tune against the live portal) ---
SDAT_PORTAL_URL = os.getenv(
    "MD_SDAT_PORTAL_URL",
    "https://opendata.maryland.gov/Business-and-Economy/Maryland-Real-Property-Assessments/",
)
# Fast path: skip DOM resolution and hit the Socrata CSV export directly.
# Direct Socrata SODA CSV endpoint for the statewide "Maryland Real Property
# Assessments" dataset (ed4q-f8tm). Using the stable resource API (bounded by
# $limit, ordered by :id) is far more robust than scraping the portal page's
# rendered "Export" link, which broke when the page DOM changed (2026-06). The
# DOM-scrape `_resolve_export_url` path remains as a fallback if this is blanked.
SDAT_EXPORT_URL = os.getenv(
    "MD_SDAT_EXPORT_URL",
    "https://opendata.maryland.gov/resource/ed4q-f8tm.csv?$order=:id&$limit=10000",
)
# CSS selector for the portal's "Export → CSV" anchor, used when no direct URL.
SDAT_EXPORT_SELECTOR = os.getenv(
    "MD_SDAT_EXPORT_SELECTOR", "a[href*='rows.csv'], a[href*='accessType=DOWNLOAD']"
)

# --- Rate limiting / retry / batching ---
MIN_REQUEST_INTERVAL = float(os.getenv("MD_SDAT_MIN_INTERVAL", "2.5"))  # seconds
REQUEST_JITTER = float(os.getenv("MD_SDAT_JITTER", "1.0"))              # +/- seconds
MAX_RETRIES = int(os.getenv("MD_SDAT_MAX_RETRIES", "5"))
BASE_BACKOFF = float(os.getenv("MD_SDAT_BASE_BACKOFF", "3.0"))
MAX_BACKOFF = float(os.getenv("MD_SDAT_MAX_BACKOFF", "120.0"))
NAV_TIMEOUT_MS = int(os.getenv("MD_SDAT_NAV_TIMEOUT_MS", "60000"))
BATCH_SIZE = int(os.getenv("MD_SDAT_BATCH_SIZE", "500"))

# Header synonyms — SDAT/Socrata column names drift, so we map fuzzily rather
# than hard-coding a single layout. First matching header (case-insensitive,
# punctuation-stripped) wins.
_COLUMN_SYNONYMS = {
    "parcel_id": ("acctid", "account number", "account id", "parcel id", "parcel", "real property account"),
    "address": ("street address", "premise address", "property address", "site address", "address"),
    "city": ("premise city", "property city", "city", "town"),
    "zip_code": ("premise zip", "property zip", "zip code", "zip", "postal code"),
    "owner_name": ("owner name", "owner", "ownername", "owner 1"),
    "mail_address": ("mailing address", "owner address", "mail address"),
    "estimated_value": ("total assessment", "total assessed value", "current assessment", "assessed value", "total value"),
    "last_sale_date": ("transfer date", "deed date", "sale date", "last sale date"),
    "last_sale_price": ("consideration", "sale price", "transfer price"),
    "county": ("county", "county name", "jurisdiction"),
    "bedrooms": ("bedrooms", "bedroom count", "number of bedrooms"),
    "bathrooms": ("bathrooms", "bathroom count", "number of bathrooms"),
    "rooms": ("rooms", "total rooms", "room count"),
    "year_built": ("year built", "construction year"),
    "property_class": ("property class", "assessment class", "use class"),
    "building_area_sqft": (
        "building area", "building square feet", "living area", "gross living area",
    ),
    "lot_area_sqft": ("lot area", "lot square feet", "land square feet"),
}

_CORP_TOKENS = ("llc", "l.l.c", "inc", "incorporated", "corp", "co.", "company", "lp", "l.p", "partners", "holdings", "properties", "group", "ventures")
_TRUST_TOKENS = ("trust", "trustee", "estate of", "living trust", "family trust")


# --------------------------------------------------------------------------- #
# Rate limiter — min interval between requests with symmetric jitter so we look
# like a human and never hammer the state servers in a tight loop.
# --------------------------------------------------------------------------- #
class _RateLimiter:
    def __init__(self, min_interval: float, jitter: float):
        self._min_interval = min_interval
        self._jitter = jitter
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = (self._last + self._min_interval) - now
            wait += random.uniform(0, self._jitter)  # always add a little noise
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class MdSdatHarvester(PropertyHarvester):
    """Async SDAT residential ingest. Concrete `PropertyHarvester` so it slots
    into the Scout pipeline; the real work is the async `harvest()` coroutine."""

    def __init__(
        self,
        tenant_id: str,
        agent_id: str = "md-sdat-harvester",
        headless: bool = True,
        cache=None,
    ):
        if not tenant_id:
            raise ValueError(
                "tenant_id is required — leads.tenant_id is NOT NULL and RLS-checked. "
                "Set ORACLE_INGEST_TENANT_ID to the target tenant UUID."
            )
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.headless = headless
        self._cache = cache
        self._limiter = _RateLimiter(MIN_REQUEST_INTERVAL, REQUEST_JITTER)
        self.metrics = {
            "state": STATE,  # required by firehose — results keyed on m["state"]
            "source": "Maryland SDAT real property assessments",
            "source_key": "md_sdat",
            "requests": 0,
            "retries": 0,
            "fetched": 0,    # CSV rows seen
            "parsed": 0,     # rows mapped to a PropertyRecord
            "skipped": 0,    # rows dropped (missing parcel/address)
            "inserted": 0,   # rows written to leads
        }

    # ------------------------------------------------------------------ #
    # Async core
    # ------------------------------------------------------------------ #
    async def harvest(
        self,
        *,
        max_records: Optional[int] = None,
        checkpoint: int = 0,
        persist: bool = True,
    ) -> dict:
        """Full pipeline: launch browser → resolve export → download CSV →
        parse → persist. Returns the metrics dict; also logs a summary."""
        try:
            from playwright.async_api import async_playwright  # lazy: heavy dep
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "Playwright not installed. `pip install playwright && playwright install chromium`"
            ) from e

        t0 = time.monotonic()
        logger.info("MD SDAT harvest starting (tenant=%s, headless=%s)", self.tenant_id, self.headless)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    accept_downloads=True,
                )
                page = await context.new_page()

                export_url = SDAT_EXPORT_URL or await self._resolve_export_url(page)
                logger.info("Resolved residential export URL: %s", export_url)

                raw = await self._fetch_csv(context, export_url)
                self.metrics["fetched_bytes"] = len(raw)

                records = self._parse_csv(
                    raw,
                    max_records=max_records,
                    checkpoint=max(0, int(checkpoint or 0)),
                )
                logger.info(
                    "Parsed %d/%d rows into PropertyRecords (%d skipped)",
                    self.metrics["parsed"], self.metrics["fetched"], self.metrics["skipped"],
                )

                self._records = records
                if persist:
                    await self._persist(records)
            finally:
                await browser.close()

        elapsed = time.monotonic() - t0
        rate = self.metrics["inserted"] / elapsed if elapsed else 0.0
        logger.info(
            "MD SDAT harvest complete in %.1fs — fetched=%d parsed=%d skipped=%d "
            "inserted=%d requests=%d retries=%d (%.1f rows/s)",
            elapsed, self.metrics["fetched"], self.metrics["parsed"], self.metrics["skipped"],
            self.metrics["inserted"], self.metrics["requests"], self.metrics["retries"], rate,
        )
        self.metrics["elapsed_s"] = round(elapsed, 2)
        self.metrics["rows_per_s"] = round(rate, 2)
        return self.metrics

    async def _resolve_export_url(self, page) -> str:
        """Open the portal page and pull the CSV export href from the DOM, so we
        always follow the currently-published dataset version."""
        async def _nav():
            self.metrics["requests"] += 1
            await page.goto(SDAT_PORTAL_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            el = await page.query_selector(SDAT_EXPORT_SELECTOR)
            if el is None:
                raise RuntimeError(f"No export link matched {SDAT_EXPORT_SELECTOR!r} on {SDAT_PORTAL_URL}")
            href = await el.get_attribute("href")
            if not href:
                raise RuntimeError("Export element found but href was empty")
            if href.startswith("/"):
                origin = SDAT_PORTAL_URL.split("/", 3)
                href = f"{origin[0]}//{origin[2]}{href}"
            return href

        if self._cache is None:
            from data_integrations.cache import get_integration_cache

            self._cache = await get_integration_cache()

        async def fetch_portal() -> dict:
            return {"export_url": await self._with_retries(_nav, what="resolve export URL")}

        cached = await self._cache.get_or_fetch(
            "state_gis",
            {"provider": "maryland_sdat", "operation": "resolve_export", "portal": SDAT_PORTAL_URL},
            fetch_portal,
            ttl=7 * 86_400,
        )
        return str(cached["export_url"])

    async def _fetch_csv(self, context, url: str) -> bytes:
        """Download the CSV through the browser's request context so we get HTTP
        status codes (429/503 → retry with backoff)."""
        async def _get():
            await self._limiter.acquire()
            self.metrics["requests"] += 1
            resp = await context.request.get(url, timeout=NAV_TIMEOUT_MS)
            status = resp.status
            if status in (429, 503):
                # Honor Retry-After if present; raise so _with_retries backs off.
                retry_after = resp.headers.get("retry-after")
                raise _Retryable(f"HTTP {status} from SDAT", retry_after=_parse_retry_after(retry_after))
            if status >= 400:
                raise RuntimeError(f"SDAT export returned HTTP {status}")
            return await resp.body()

        if self._cache is None:
            from data_integrations.cache import get_integration_cache

            self._cache = await get_integration_cache()

        async def fetch_export() -> dict:
            raw = await self._with_retries(_get, what="download residential CSV")
            return {"csv_base64": base64.b64encode(raw).decode("ascii")}

        cached = await self._cache.get_or_fetch(
            "state_gis",
            {"provider": "maryland_sdat", "operation": "parcel_export", "url": url},
            fetch_export,
            ttl=7 * 86_400,
        )
        return base64.b64decode(cached["csv_base64"], validate=True)

    async def _with_retries(self, coro_factory, *, what: str):
        """Run `coro_factory()` with exponential backoff + jitter. Retries on
        `_Retryable`, timeouts, and transient connection errors; re-raises the
        last error once MAX_RETRIES is exhausted."""
        attempt = 0
        while True:
            try:
                return await coro_factory()
            except Exception as e:  # noqa: BLE001 — classified below
                attempt += 1
                retryable = isinstance(e, _Retryable) or _is_transient(e)
                if not retryable or attempt > MAX_RETRIES:
                    logger.error("%s failed permanently (attempt %d): %s", what, attempt, e)
                    raise
                self.metrics["retries"] += 1
                hinted = getattr(e, "retry_after", None)
                backoff = hinted if hinted else min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
                backoff += random.uniform(0, REQUEST_JITTER)
                logger.warning(
                    "%s failed (attempt %d/%d): %s — backing off %.1fs",
                    what, attempt, MAX_RETRIES, e, backoff,
                )
                await asyncio.sleep(backoff)

    # ------------------------------------------------------------------ #
    # Parsing / mapping
    # ------------------------------------------------------------------ #
    def _parse_csv(
        self,
        raw: bytes,
        *,
        max_records: Optional[int] = None,
        checkpoint: int = 0,
    ) -> list[PropertyRecord]:
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            logger.warning("CSV had no header row — nothing to ingest")
            return []
        colmap = self._build_colmap(reader.fieldnames)
        missing = [k for k in ("parcel_id", "address") if k not in colmap]
        if missing:
            logger.warning(
                "CSV missing required column(s) %s; available headers: %s — aborting parse",
                missing, reader.fieldnames,
            )
            return []

        records: list[PropertyRecord] = []
        self.metrics["checkpoint_start"] = checkpoint
        next_checkpoint = checkpoint
        complete = True
        for source_index, row in enumerate(reader):
            if source_index < checkpoint:
                continue
            self.metrics["fetched"] += 1
            next_checkpoint = source_index + 1
            rec = self._map_row(row, colmap)
            if rec is None:
                self.metrics["skipped"] += 1
                continue
            records.append(rec)
            self.metrics["parsed"] += 1
            if max_records and len(records) >= max_records:
                complete = False
                break
        self.metrics["checkpoint"] = None if complete else next_checkpoint
        self.metrics["checkpoint_complete"] = complete
        return records

    def _build_colmap(self, headers: list[str]) -> dict[str, str]:
        """Map our field names → the actual CSV header for this export."""
        # Exact normalized match wins; else the first header whose normalized form
        # CONTAINS the synonym. SDAT/Socrata headers carry long mdp/sdat suffixes
        # (e.g. 'account_id_mdp_field_acctid', 'mdp_street_address_mdp_field_address'),
        # so substring matching is required — exact-equality silently mapped nothing.
        norm_pairs = [(self._norm(h), h) for h in headers]
        exact = {n: h for n, h in norm_pairs}
        colmap: dict[str, str] = {}
        for field, synonyms in _COLUMN_SYNONYMS.items():
            for syn in synonyms:
                ns = self._norm(syn)
                actual = exact.get(ns) or next((h for n, h in norm_pairs if ns in n), None)
                if actual:
                    colmap[field] = actual
                    break
        return colmap

    @staticmethod
    def _norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    def _map_row(self, row: dict, colmap: dict[str, str]) -> Optional[PropertyRecord]:
        def g(field: str, default: str = "") -> str:
            col = colmap.get(field)
            return (row.get(col, default) or default).strip() if col else default

        parcel_id = g("parcel_id")
        address = g("address")
        if not parcel_id or not address:
            return None  # unusable — caller counts as skipped

        owner_name = g("owner_name")
        owner_type = self._classify_owner(owner_name)
        est_value = _to_float(g("estimated_value"))
        mail_address = g("mail_address")
        # Absentee heuristic: owner's mailing address differs from the premise.
        is_absentee = bool(mail_address) and self._norm(mail_address) != self._norm(address)

        distress: list[str] = []
        # Long-tenure absentee owners are the classic motivated-seller signal SDAT
        # can surface without lien data; richer flags are layered in by Scout.
        if is_absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=g("city"),
            state=STATE,
            zip_code=g("zip_code")[:10],
            owner_name=owner_name,
            owner_type=owner_type,
            estimated_value=est_value,
            equity_percent=0.0,  # not derivable from assessment alone; Scout enriches
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=g("last_sale_date") or None,
            county=g("county") or None,
            bedrooms=_to_float(g("bedrooms")) or None,
            bathrooms=_to_float(g("bathrooms")) or None,
            rooms=_to_float(g("rooms")) or None,
            year_built=int(value) if (value := _to_float(g("year_built"))) >= 1600 else None,
            property_class=g("property_class") or None,
            last_sale_price=_to_float(g("last_sale_price")) or None,
            building_area_sqft=_to_float(g("building_area_sqft")) or None,
            lot_area_sqft=_to_float(g("lot_area_sqft")) or None,
        )

    @staticmethod
    def _classify_owner(name: str) -> str:
        low = name.lower()
        if any(tok in low for tok in _TRUST_TOKENS):
            return "trust"
        if any(tok in low for tok in _CORP_TOKENS):
            return "corporate"
        return "individual"

    @staticmethod
    def _motivation_score(rec: PropertyRecord) -> int:
        """Cheap 0–100 prior from the raw assessment signal. Scout's underwriter
        replaces this once ARV/rehab/MAO are computed."""
        score = min(len(rec.distress_flags) * 25, 50)
        if rec.is_absentee_owner:
            score += 20
        score += int(max(0.0, min(rec.equity_percent, 100.0)) * 0.3)  # up to 30
        return min(score, 100)

    # ------------------------------------------------------------------ #
    # Persistence — batch INSERT into the RLS-scoped leads table
    # ------------------------------------------------------------------ #
    async def _persist(self, records: list[PropertyRecord]) -> int:
        if not records:
            logger.info("No records to persist.")
            return 0
        # Use the same strict payload contract as every other state.  This
        # replaces the historical MD-only persistence path, which could drift
        # from source provenance and cleanup rules used by the national run.
        return await persist_leads(
            self.tenant_id,
            self.agent_id,
            records,
            metrics=self.metrics,
        )

    # ------------------------------------------------------------------ #
    # PropertyHarvester ABC compliance
    # ------------------------------------------------------------------ #
    def fetch_by_zip(self, zip_code: str) -> list[PropertyRecord]:
        """Sync convenience wrapper required by the ABC. Runs the async pipeline
        and returns the parsed records (does NOT persist). Prefer `harvest()`."""
        async def _run():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                try:
                    context = await browser.new_context(accept_downloads=True)
                    page = await context.new_page()
                    url = SDAT_EXPORT_URL or await self._resolve_export_url(page)
                    raw = await self._fetch_csv(context, url)
                    return [r for r in self._parse_csv(raw) if r.zip_code.startswith(zip_code)]
                finally:
                    await browser.close()

        return asyncio.run(_run())

    def fetch_cash_buyers(self, state: str, min_purchases: int = 3) -> list[dict]:
        """SDAT's residential assessment export exposes no buyer-activity data."""
        return []


# --------------------------------------------------------------------------- #
# Retry classification helpers
# --------------------------------------------------------------------------- #
class _Retryable(Exception):
    """Raised for explicitly-retryable conditions (e.g. HTTP 429/503)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("timeout", "timed out", "econnreset", "reset by peer", "temporarily"))


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)  # delta-seconds form
    except ValueError:
        return None  # HTTP-date form — fall back to exponential backoff


def _to_float(value: str) -> float:
    if not value:
        return 0.0
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# CLI:  python -m backend.harvesters.md_sdat_ingest [zip ...]
# --------------------------------------------------------------------------- #
async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tenant_id = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant_id:
        raise SystemExit("Set ORACLE_INGEST_TENANT_ID to the target tenant UUID before ingesting.")

    from db.connection import init_pool, close_pool

    max_records = int(os.getenv("MD_SDAT_MAX_RECORDS", "0")) or None
    headless = os.getenv("MD_SDAT_HEADLESS", "1") != "0"

    await init_pool()
    try:
        harvester = MdSdatHarvester(tenant_id=tenant_id, headless=headless)
        metrics = await harvester.harvest(max_records=max_records)
        logger.info("Final metrics: %s", json.dumps(metrics, indent=2))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
