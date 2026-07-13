"""
Harvester spine — shared machinery for the 10-state firehose around Delaware.

Every state harvester is a *real* scraper hitting that state's actual open-data
endpoint, but they all share this base: a polite rate limiter, exponential
backoff that respects HTTP 429/503, JSON fetch over stdlib urllib (run in a
thread so it never blocks the loop — no aiohttp/httpx dependency), unified
mapping helpers, and one batched INSERT into the RLS-scoped `leads` table.

Three source archetypes cover every state portal we touch:

  * SocrataHarvester — `{base}.json?$limit&$offset`            (MD-alt, NY, CT)
  * ArcGISHarvester  — `{service}/query?...&resultOffset`      (DE, NJ, VA, WV, MA, NC)
  * CartoHarvester   — `{domain}/api/v2/sql?q=SELECT ... LIMIT` (PA / Philadelphia)

A concrete state subclasses the right archetype, sets its endpoint constant(s),
and implements `map_record(row) -> PropertyRecord | None` against that dataset's
real column names. `tenancy` / `db.connection` (→ FastAPI) are imported lazily so
these modules stay importable in a bare scraper environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from .property_adapter import PropertyRecord

logger = logging.getLogger("oracle.harvester")

# Politeness / resilience defaults (env-overridable, shared across all states).
MIN_REQUEST_INTERVAL = float(os.getenv("FIREHOSE_MIN_INTERVAL", "1.5"))
REQUEST_JITTER = float(os.getenv("FIREHOSE_JITTER", "0.8"))
MAX_RETRIES = int(os.getenv("FIREHOSE_MAX_RETRIES", "5"))
BASE_BACKOFF = float(os.getenv("FIREHOSE_BASE_BACKOFF", "3.0"))
MAX_BACKOFF = float(os.getenv("FIREHOSE_MAX_BACKOFF", "120.0"))
HTTP_TIMEOUT = int(os.getenv("FIREHOSE_HTTP_TIMEOUT", "45"))
PAGE_SIZE = int(os.getenv("FIREHOSE_PAGE_SIZE", "1000"))
BATCH_SIZE = int(os.getenv("FIREHOSE_BATCH_SIZE", "500"))
USER_AGENT = os.getenv(
    "FIREHOSE_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) Oracle-Firehose/1.0 (+research)",
)

_CORP_TOKENS = ("llc", "l.l.c", "inc", "incorporated", "corp", "co.", "company",
                "lp", "l.p", "partners", "holdings", "properties", "group", "ventures",
                "associates", "realty", "investments", "capital", "enterprises")
_TRUST_TOKENS = ("trust", "trustee", "estate of", "living trust", "family trust")


class Retryable(Exception):
    """Explicitly-retryable condition (HTTP 429/503). Carries an optional hint."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name or "urlerror" in name:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("timed out", "timeout", "reset", "temporarily", "refused"))


def to_float(value) -> float:
    if value is None:
        return 0.0
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def classify_owner(name: str) -> str:
    low = (name or "").lower()
    if any(tok in low for tok in _TRUST_TOKENS):
        return "trust"
    if any(tok in low for tok in _CORP_TOKENS):
        return "corporate"
    return "individual"


def norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def motivation_score(rec: PropertyRecord) -> int:
    """Cheap 0–100 prior from raw assessment signal. Scout's underwriter replaces
    this once ARV / rehab / MAO are computed downstream."""
    score = min(len(rec.distress_flags) * 22, 55)
    if rec.is_absentee_owner:
        score += 22
    score += int(max(0.0, min(rec.equity_percent, 100.0)) * 0.23)  # up to ~23
    if rec.owner_type in ("corporate", "trust"):
        score += 5
    return max(0, min(score, 100))


class RateLimiter:
    """Min-interval throttle with symmetric jitter, shared by a firehose run so
    10 states don't collectively hammer any one host."""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL, jitter: float = REQUEST_JITTER):
        self._min_interval = min_interval
        self._jitter = jitter
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            wait = (self._last + self._min_interval) - time.monotonic()
            wait += random.uniform(0, self._jitter)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


# --------------------------------------------------------------------------- #
# Leads persistence — one place, every state.
# --------------------------------------------------------------------------- #
async def persist_leads(tenant_id: str, agent_id: str, records: list[PropertyRecord], *, metrics: dict) -> int:
    if not records:
        return 0

    # Lazy: these transitively import FastAPI. tenant_tx applies the RLS GUCs.
    from tenancy import TenantContext, Role
    from db.connection import tenant_tx

    ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    # Idempotent upsert: a recurring harvest re-sees the same parcels every cycle,
    # so refresh the row in place instead of accumulating a duplicate lead per run.
    # Requires UNIQUE (tenant_id, parcel_id) from migration 0018.
    sql = (
        "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score, underwriting, payload) "
        "VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb) "
        "ON CONFLICT (tenant_id, parcel_id) DO UPDATE SET "
        "motivation_score = EXCLUDED.motivation_score, "
        "underwriting = EXCLUDED.underwriting, "
        "payload = EXCLUDED.payload, "
        "updated_at = now()"
    )

    total = 0
    async with tenant_tx(ctx) as conn:
        for start in range(0, len(records), BATCH_SIZE):
            batch = records[start:start + BATCH_SIZE]
            args = [
                (
                    tenant_id,
                    rec.parcel_id,
                    rec.state,
                    motivation_score(rec),
                    json.dumps({
                        "estimated_value": rec.estimated_value,
                        "equity_percent": rec.equity_percent,
                        "source": f"firehose:{rec.state}",
                    }),
                    json.dumps(asdict(rec)),
                )
                for rec in batch
            ]
            await conn.executemany(sql, args)
            total += len(batch)
            metrics["inserted"] = total
    return total


class BaseHarvester(ABC):
    """One state's real ingest. Subclasses set the endpoint and implement
    `fetch_raw()` (archetype mixins do this) + `map_record()`."""

    STATE: str = "??"
    SOURCE_LABEL: str = "unknown"
    SOURCE_KEY: str = ""
    RETAIN_RAW: bool = False
    SOQL_CURSOR_FIELD: str = ""

    def __init__(self, tenant_id: str, agent_id: Optional[str] = None, limiter: Optional[RateLimiter] = None):
        if not tenant_id:
            raise ValueError(
                "tenant_id is required — leads.tenant_id is NOT NULL and RLS-checked. "
                "Set ORACLE_INGEST_TENANT_ID to the target tenant UUID."
            )
        self.tenant_id = tenant_id
        self.agent_id = agent_id or f"firehose-{self.STATE.lower()}"
        self.limiter = limiter or RateLimiter()
        self.metrics = {
            "state": self.STATE, "source": self.SOURCE_LABEL,
            "requests": 0, "retries": 0,
            "fetched": 0, "parsed": 0, "skipped": 0, "aggregated": 0,
            "inserted": 0, "raw_retained": 0,
        }
        self._cursor_start: Optional[str] = None

    # -- HTTP (stdlib urllib in a thread; rate-limited + retried) -- #
    async def _get_json(self, url: str, headers: Optional[dict] = None):
        async def _do():
            await self.limiter.acquire()
            self.metrics["requests"] += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})

            def _blocking():
                try:
                    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503):
                        ra = e.headers.get("Retry-After") if e.headers else None
                        raise Retryable(f"HTTP {e.code} from {self.SOURCE_LABEL}",
                                        retry_after=_parse_retry_after(ra))
                    raise

            body = await asyncio.to_thread(_blocking)
            text = body.decode("utf-8", errors="replace").strip()
            if not text:
                logger.warning("[%s] empty response body from %s", self.STATE, url)
                return []
            return json.loads(text)

        return await self._with_retries(_do, what=f"{self.STATE} fetch")

    async def _with_retries(self, coro_factory, *, what: str):
        attempt = 0
        while True:
            try:
                return await coro_factory()
            except Exception as e:  # noqa: BLE001 — classified below
                attempt += 1
                retryable = isinstance(e, Retryable) or is_transient(e)
                if not retryable or attempt > MAX_RETRIES:
                    logger.error("%s failed permanently (attempt %d): %s", what, attempt, e)
                    raise
                self.metrics["retries"] += 1
                hinted = getattr(e, "retry_after", None)
                backoff = hinted if hinted else min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
                backoff += random.uniform(0, REQUEST_JITTER)
                logger.warning("%s failed (attempt %d/%d): %s — backing off %.1fs",
                               what, attempt, MAX_RETRIES, e, backoff)
                await asyncio.sleep(backoff)

    # -- Template method -- #
    @abstractmethod
    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        """Return raw source rows (already paginated/limited)."""

    @abstractmethod
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        """Map one source row to a PropertyRecord, or None to skip."""

    def aggregate_records(self, records: list[PropertyRecord]) -> list[PropertyRecord]:
        """Hook for row-heavy feeds (violations) to collapse by property."""
        return records

    def raw_property_key(self, row: dict) -> str:
        """Property reconciliation key used by raw-source retention."""
        return ""

    async def _load_cursor(self) -> Optional[str]:
        if not self.RETAIN_RAW or not self.SOURCE_KEY:
            return None
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(ctx) as conn:
            return await conn.fetchval(
                """
                SELECT cursor_value FROM harvest_sources
                WHERE tenant_id=$1::uuid AND source_key=$2
                """,
                self.tenant_id,
                self.SOURCE_KEY,
            )

    @staticmethod
    def _cursor_sort_key(value: Any) -> tuple[int, Any]:
        text = str(value or "").strip()
        try:
            return (0, int(text))
        except ValueError:
            return (1, text)

    def _cursor_end(self, rows: list[dict]) -> Optional[str]:
        if not self.SOQL_CURSOR_FIELD:
            return None
        values = [
            str(row.get(self.SOQL_CURSOR_FIELD) or "").strip()
            for row in rows
            if str(row.get(self.SOQL_CURSOR_FIELD) or "").strip()
        ]
        return max(values, key=self._cursor_sort_key) if values else self._cursor_start

    async def _retain_raw(self, rows: list[dict], cursor_end: Optional[str]) -> None:
        """Persist exact public observations and advance the cursor atomically."""
        if not self.RETAIN_RAW or not self.SOURCE_KEY:
            return
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        retrieved_at = datetime.now(timezone.utc)
        request_material = json.dumps(
            {
                "source": self.SOURCE_KEY,
                "cursor_start": self._cursor_start,
                "cursor_end": cursor_end,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(request_material.encode("utf-8")).hexdigest()
        retention_days = max(
            1,
            min(3650, int(os.getenv("ORACLE_RAW_SOURCE_RETENTION_DAYS", "730"))),
        )
        async with tenant_tx(ctx) as conn:
            license_row = await conn.fetchrow(
                """
                INSERT INTO source_licenses (
                    tenant_id,source_key,source_name,source_url,license_name,
                    property_level_allowed,outreach_use_allowed,retention_days
                ) VALUES ($1::uuid,$2,$3,$4,'municipal-open-data',true,false,$5)
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    source_name=EXCLUDED.source_name,source_url=EXCLUDED.source_url,
                    active=true,updated_at=now()
                RETURNING id,retention_days
                """,
                self.tenant_id,
                self.SOURCE_KEY,
                self.SOURCE_LABEL,
                getattr(self, "RESOURCE_URL", None),
                retention_days,
            )
            for row in rows:
                raw_blob = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                payload_digest = hashlib.sha256(raw_blob.encode("utf-8")).hexdigest()
                record_key = str(
                    row.get(self.SOQL_CURSOR_FIELD)
                    or row.get("id")
                    or row.get("violationid")
                    or payload_digest
                )
                await conn.execute(
                    """
                    INSERT INTO source_records (
                        tenant_id,source_license_id,source_key,record_key,
                        property_key,jurisdiction,observed_at,retrieved_at,
                        request_hash,payload_hash,raw_payload,expires_at
                    ) VALUES (
                        $1::uuid,$2,$3,$4,$5,$6,$7,$7,$8,$9,$10::jsonb,
                        $7 + ($11 || ' days')::interval
                    ) ON CONFLICT (tenant_id,source_key,record_key,observed_at) DO NOTHING
                    """,
                    self.tenant_id,
                    license_row["id"],
                    self.SOURCE_KEY,
                    record_key,
                    self.raw_property_key(row) or None,
                    self.STATE,
                    retrieved_at,
                    request_hash,
                    payload_digest,
                    raw_blob,
                    str(license_row["retention_days"]),
                )
                self.metrics["raw_retained"] += 1
            await conn.execute(
                """
                INSERT INTO harvest_sources (
                    tenant_id,source_key,display_name,jurisdiction,adapter,
                    cursor_value,cursor_observed_at,last_started_at,
                    last_succeeded_at,last_record_observed_at,coverage
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,now(),now(),now(),now(),$7::jsonb)
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    cursor_value=EXCLUDED.cursor_value,
                    cursor_observed_at=EXCLUDED.cursor_observed_at,
                    last_succeeded_at=EXCLUDED.last_succeeded_at,
                    last_record_observed_at=EXCLUDED.last_record_observed_at,
                    coverage=EXCLUDED.coverage,
                    failure_count=0,circuit_state='closed',last_error=NULL,
                    updated_at=now()
                """,
                self.tenant_id,
                self.SOURCE_KEY,
                self.SOURCE_LABEL,
                self.STATE,
                type(self).__name__,
                cursor_end,
                json.dumps({"fetched": len(rows), "raw_retained": self.metrics["raw_retained"]}),
            )

    async def harvest(self, *, max_records: Optional[int] = None, persist: bool = True) -> dict:
        t0 = time.monotonic()
        logger.info("[%s] harvest starting (%s)", self.STATE, self.SOURCE_LABEL)
        if persist and self.RETAIN_RAW:
            self._cursor_start = await self._load_cursor()
        rows = await self.fetch_raw(max_records)

        records: list[PropertyRecord] = []
        for row in rows:
            self.metrics["fetched"] += 1
            rec = self.map_record(row)
            if rec is None:
                self.metrics["skipped"] += 1
                continue
            records.append(rec)
            self.metrics["parsed"] += 1

        records = self.aggregate_records(records)
        self.metrics["aggregated"] = len(records)

        if persist:
            await persist_leads(self.tenant_id, self.agent_id, records, metrics=self.metrics)
            if self.RETAIN_RAW:
                await self._retain_raw(rows, self._cursor_end(rows))

        elapsed = time.monotonic() - t0
        self.metrics["elapsed_s"] = round(elapsed, 2)
        logger.info(
            "[%s] done in %.1fs — fetched=%d parsed=%d skipped=%d inserted=%d "
            "(requests=%d retries=%d)",
            self.STATE, elapsed, self.metrics["fetched"], self.metrics["parsed"],
            self.metrics["skipped"], self.metrics["inserted"],
            self.metrics["requests"], self.metrics["retries"],
        )
        self._records = records  # exposed for fetch_by_zip / tests
        return self.metrics


# --------------------------------------------------------------------------- #
# Source archetypes — paginated fetch_raw implementations.
# --------------------------------------------------------------------------- #
class SocrataHarvester(BaseHarvester):
    """SoQL/Socrata: GET {RESOURCE_URL}?$limit&$offset → list[dict]."""

    RESOURCE_URL: str = ""          # e.g. https://data.ct.gov/resource/5mzw-sjtu.json
    SOQL_WHERE: str = ""            # optional $where clause
    SOQL_ORDER: str = ":id"         # stable pagination order; override if dataset lacks :id

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        url = os.getenv(f"{self.STATE}_SOURCE_URL", self.RESOURCE_URL)
        if not url:
            logger.warning("[%s] no Socrata RESOURCE_URL configured — skipping", self.STATE)
            return []
        out: list[dict] = []
        offset = 0
        while True:
            page = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break
            params: dict = {"$limit": page, "$offset": offset}
            if self.SOQL_ORDER:
                params["$order"] = self.SOQL_ORDER
            if self.SOQL_WHERE:
                params["$where"] = self.SOQL_WHERE
            if self.SOQL_CURSOR_FIELD and self._cursor_start:
                cursor = str(self._cursor_start).replace("'", "''")
                cursor_clause = (
                    f"{self.SOQL_CURSOR_FIELD}>{cursor}"
                    if cursor.isdigit()
                    else f"{self.SOQL_CURSOR_FIELD}>'{cursor}'"
                )
                params["$where"] = (
                    f"({params['$where']}) AND {cursor_clause}"
                    if params.get("$where")
                    else cursor_clause
                )
            rows = await self._get_json(f"{url}?{urllib.parse.urlencode(params)}")
            if not rows:
                break
            out.extend(rows)
            offset += len(rows)
            if len(rows) < page or (max_records and len(out) >= max_records):
                break
        return out


class ArcGISHarvester(BaseHarvester):
    """ArcGIS REST FeatureServer: query with resultOffset → features[].attributes."""

    SERVICE_URL: str = ""           # e.g. https://.../FeatureServer/0/query
    WHERE: str = "1=1"
    OUT_FIELDS: str = "*"

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        url = os.getenv(f"{self.STATE}_SOURCE_URL", self.SERVICE_URL)
        if not url:
            logger.warning("[%s] no ArcGIS SERVICE_URL configured — skipping", self.STATE)
            return []
        out: list[dict] = []
        offset = 0
        while True:
            page = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break
            params = {
                "where": self.WHERE, "outFields": self.OUT_FIELDS,
                "returnGeometry": "false", "f": "json",
                "resultOffset": offset, "resultRecordCount": page,
            }
            data = await self._get_json(f"{url}?{urllib.parse.urlencode(params)}")
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                logger.warning(
                    "[%s] ArcGIS error response (code=%s): %s",
                    self.STATE, err.get("code", "?"), err.get("message", str(err))[:200],
                )
                break
            feats = data.get("features", []) if isinstance(data, dict) else []
            rows = [f.get("attributes", {}) for f in feats if isinstance(f, dict)]
            if not rows:
                break
            out.extend(rows)
            offset += len(rows)
            if not data.get("exceededTransferLimit") and len(rows) < page:
                break
            if max_records and len(out) >= max_records:
                break
        return out


class CartoHarvester(BaseHarvester):
    """CARTO SQL API: GET {DOMAIN}/api/v2/sql?q=SELECT ... LIMIT n OFFSET o."""

    CARTO_DOMAIN: str = ""          # e.g. https://phl.carto.com
    TABLE: str = ""
    SELECT: str = "*"
    WHERE: str = ""

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        domain = os.getenv(f"{self.STATE}_SOURCE_URL", self.CARTO_DOMAIN)
        if not domain or not self.TABLE:
            logger.warning("[%s] no CARTO source configured — skipping", self.STATE)
            return []
        out: list[dict] = []
        offset = 0
        where = f" WHERE {self.WHERE}" if self.WHERE else ""
        while True:
            page = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break
            sql = f"SELECT {self.SELECT} FROM {self.TABLE}{where} LIMIT {page} OFFSET {offset}"
            data = await self._get_json(f"{domain}/api/v2/sql?{urllib.parse.urlencode({'q': sql})}")
            if isinstance(data, dict) and data.get("error"):
                logger.warning(
                    "[%s] CARTO error response: %s",
                    self.STATE, str(data["error"])[:200],
                )
                break
            rows = data.get("rows", []) if isinstance(data, dict) else []
            if not rows:
                break
            out.extend(rows)
            offset += len(rows)
            if len(rows) < page or (max_records and len(out) >= max_records):
                break
        return out


def _parse_retry_after(value) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
