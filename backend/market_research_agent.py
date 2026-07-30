"""Nationwide, redistributable aggregate housing-market research agent.

This agent intentionally does not scrape consumer listing pages and does not
claim that aggregate research rows are individual listings. It downloads
publisher-provided research files, validates their origin and size, normalizes
state-level observations, records a SHA-256 provenance digest, and upserts the
results into ``public_market_metrics``.

Default sources:

* Zillow Research: state ZHVI and for-sale inventory CSVs.
* Redfin Data Center: state market tracker TSV/GZIP.
* FHFA: state House Price Index CSV.
* HUD Open Data: current Fair Market Rent feature service, published by HUD.

The source URLs are environment-overridable so a publisher can rotate a
download path without a code release. Overrides must still use an allow-listed
HTTPS host.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from statistics import median
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlencode, urlsplit

logger = logging.getLogger("oracle.market_research")

_MAX_DOWNLOAD_BYTES = max(
    1_000_000,
    min(250_000_000, int(os.getenv("ORACLE_MARKET_DATA_MAX_BYTES", "75000000"))),
)
_TIMEOUT_SECONDS = max(
    10,
    min(180, int(os.getenv("ORACLE_MARKET_DATA_TIMEOUT_SECONDS", "60"))),
)
_ALLOWED_HOSTS = frozenset(
    {
        "files.zillowstatic.com",
        "redfin-public-data.s3.us-west-2.amazonaws.com",
        "services.arcgis.com",
        "www.fhfa.gov",
        "www.huduser.gov",
    }
)
_USER_AGENT = (
    "Neoh-Market-Research/1.0 "
    "(aggregate public datasets; contact configured by operator)"
)

_STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
_CODE_TO_NAME = {code: name for name, code in _STATE_CODES.items() if name != "Columbia"}
_ALL_JURISDICTIONS = frozenset(_CODE_TO_NAME)


@dataclass(frozen=True)
class MarketObservation:
    source_key: str
    metric_key: str
    state_code: str
    geography_name: str
    period_end: date
    value: float
    unit: str
    source_url: str
    dataset_sha256: str
    dataset_updated_at: Optional[datetime]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Download:
    url: str
    body: bytes
    sha256: str
    updated_at: Optional[datetime]


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    url: str
    parser: Callable[[Download, "DatasetSpec"], list[MarketObservation]]
    attribution: str
    downloader: Optional[Callable[[str], Download]] = None


def _number(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace(",", "").replace("$", "")
    if not text or text.upper() in {"NA", "N/A", "NULL", "-"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> Optional[date]:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _state_code(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in _ALL_JURISDICTIONS:
        return upper
    return _STATE_CODES.get(text)


def _metric(
    *,
    spec: DatasetSpec,
    download: Download,
    metric_key: str,
    state_code: str,
    period_end: date,
    value: float,
    unit: str,
    metadata: Optional[dict[str, Any]] = None,
) -> MarketObservation:
    return MarketObservation(
        source_key=spec.key,
        metric_key=metric_key,
        state_code=state_code,
        geography_name=_CODE_TO_NAME[state_code],
        period_end=period_end,
        value=value,
        unit=unit,
        source_url=download.url,
        dataset_sha256=download.sha256,
        dataset_updated_at=download.updated_at,
        metadata={
            "classification": "aggregate_market_research",
            "attribution": spec.attribution,
            **(metadata or {}),
        },
    )


def _latest_observations(
    observations: Iterable[MarketObservation],
) -> list[MarketObservation]:
    """Keep the newest point for each source/metric/jurisdiction series.

    Publisher files contain full history, which is useful for validation but
    should not be rewritten on every refresh. Existing history remains in the
    table and each changed period is appended through the normal upsert key.
    """
    latest: dict[tuple[str, str, str], MarketObservation] = {}
    for row in observations:
        key = (row.source_key, row.metric_key, row.state_code)
        current = latest.get(key)
        if current is None or row.period_end > current.period_end:
            latest[key] = row
    return list(latest.values())


def _parse_zillow_wide(
    download: Download,
    spec: DatasetSpec,
    *,
    metric_key: str,
    unit: str,
) -> list[MarketObservation]:
    reader = csv.DictReader(io.StringIO(download.body.decode("utf-8-sig")))
    if not reader.fieldnames or "RegionName" not in reader.fieldnames:
        raise ValueError(f"{spec.key} returned an unexpected CSV schema")
    period_columns = [
        field for field in reader.fieldnames
        if _date(field) is not None
    ]
    observations: list[MarketObservation] = []
    for row in reader:
        if str(row.get("RegionType") or "").strip().lower() != "state":
            continue
        code = _state_code(row.get("RegionName"))
        if not code:
            continue
        for column in period_columns:
            period = _date(column)
            value = _number(row.get(column))
            if period is None or value is None:
                continue
            observations.append(_metric(
                spec=spec,
                download=download,
                metric_key=metric_key,
                state_code=code,
                period_end=period,
                value=value,
                unit=unit,
                metadata={"publisher_region_id": row.get("RegionID")},
            ))
    return observations


def parse_zillow_zhvi(download: Download, spec: DatasetSpec) -> list[MarketObservation]:
    return _parse_zillow_wide(
        download, spec, metric_key="typical_home_value_zhvi", unit="usd",
    )


def parse_zillow_inventory(download: Download, spec: DatasetSpec) -> list[MarketObservation]:
    return _parse_zillow_wide(
        download, spec, metric_key="for_sale_inventory", unit="homes",
    )


_REDFIN_METRICS = {
    "MEDIAN_SALE_PRICE": ("median_sale_price", "usd"),
    "MEDIAN_LIST_PRICE": ("median_list_price", "usd"),
    "MEDIAN_PPSF": ("median_sale_price_per_sqft", "usd_per_sqft"),
    "HOMES_SOLD": ("homes_sold", "homes"),
    "NEW_LISTINGS": ("new_listings", "homes"),
    "INVENTORY": ("active_inventory", "homes"),
    "MONTHS_OF_SUPPLY": ("months_of_supply", "months"),
    "MEDIAN_DOM": ("median_days_on_market", "days"),
}


def parse_redfin_tracker(download: Download, spec: DatasetSpec) -> list[MarketObservation]:
    raw = gzip.GzipFile(fileobj=io.BytesIO(download.body))
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text, delimiter="\t")
    required = {"REGION_TYPE", "STATE_CODE", "PROPERTY_TYPE", "PERIOD_END"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(f"{spec.key} returned an unexpected TSV schema")
    observations: list[MarketObservation] = []
    for row in reader:
        if str(row.get("REGION_TYPE") or "").lower() != "state":
            continue
        if str(row.get("PROPERTY_TYPE") or "") != "All Residential":
            continue
        code = _state_code(row.get("STATE_CODE"))
        period = _date(row.get("PERIOD_END"))
        if not code or period is None:
            continue
        for source_field, (metric_key, unit) in _REDFIN_METRICS.items():
            value = _number(row.get(source_field))
            if value is None:
                continue
            observations.append(_metric(
                spec=spec,
                download=download,
                metric_key=metric_key,
                state_code=code,
                period_end=period,
                value=value,
                unit=unit,
                metadata={
                    "seasonally_adjusted": (
                        str(row.get("IS_SEASONALLY_ADJUSTED") or "").lower() == "true"
                    ),
                    "publisher_last_updated": row.get("LAST_UPDATED"),
                },
            ))
    return observations


def parse_fhfa_state_hpi(download: Download, spec: DatasetSpec) -> list[MarketObservation]:
    reader = csv.reader(io.StringIO(download.body.decode("utf-8-sig")))
    observations: list[MarketObservation] = []
    for row in reader:
        if len(row) < 4:
            continue
        code = _state_code(row[0])
        value = _number(row[3])
        try:
            year, quarter = int(row[1]), int(row[2])
        except (TypeError, ValueError):
            continue
        if not code or value is None or quarter not in {1, 2, 3, 4}:
            continue
        period = date(year, quarter * 3, 1)
        # Last calendar day of the quarter.
        if quarter == 4:
            period = date(year, 12, 31)
        else:
            period = date.fromordinal(date(year, quarter * 3 + 1, 1).toordinal() - 1)
        observations.append(_metric(
            spec=spec,
            download=download,
            metric_key="fhfa_all_transactions_hpi",
            state_code=code,
            period_end=period,
            value=value,
            unit="index",
            metadata={"year": year, "quarter": quarter, "seasonally_adjusted": False},
        ))
    return observations


def _normalized_header(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "".join(ch for ch in str(key).lower() if ch.isalnum()): value
        for key, value in row.items()
    }


def parse_hud_fmr(download: Download, spec: DatasetSpec) -> list[MarketObservation]:
    """Aggregate current county/metro two-bedroom FMRs to a state median."""
    if download.body.lstrip().startswith(b"{"):
        return _parse_hud_arcgis_fmr(download, spec)

    reader = csv.DictReader(io.StringIO(download.body.decode("utf-8-sig")))
    if not reader.fieldnames:
        raise ValueError(f"{spec.key} returned an empty CSV")
    by_state_year: dict[tuple[str, int], list[float]] = {}
    for source_row in reader:
        row = _normalized_header(source_row)
        code = _state_code(
            row.get("state")
            or row.get("statecode")
            or row.get("stusps")
            or row.get("statealpha")
        )
        year_value = _number(row.get("year") or row.get("fiscalyear") or row.get("fy"))
        rent = _number(
            row.get("fmr2")
            or row.get("twobedroom")
            or row.get("fmr2bedroom")
            or row.get("fmr2br")
        )
        if not code or year_value is None or rent is None:
            continue
        by_state_year.setdefault((code, int(year_value)), []).append(rent)
    observations: list[MarketObservation] = []
    for (code, year), values in by_state_year.items():
        observations.append(_metric(
            spec=spec,
            download=download,
            metric_key="hud_median_two_bedroom_fmr",
            state_code=code,
            period_end=date(year, 10, 1),
            value=float(median(values)),
            unit="usd_per_month",
            metadata={
                "aggregation": "median_of_published_county_and_metro_fmrs",
                "source_observations": len(values),
            },
        ))
    return observations


def _parse_hud_arcgis_fmr(
    download: Download,
    spec: DatasetSpec,
) -> list[MarketObservation]:
    """Normalize HUD's official FY feature service to one median per state/DC."""
    try:
        payload = json.loads(download.body)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{spec.key} returned invalid ArcGIS JSON") from exc
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{spec.key} returned an unexpected ArcGIS schema")

    code_pattern = re.compile(
        rf"(?<![A-Z])({'|'.join(sorted(_ALL_JURISDICTIONS))})(?![A-Z])"
    )
    by_state: dict[str, list[float]] = {}
    for feature in features:
        attributes = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attributes, dict):
            continue
        area_name = str(attributes.get("FMR_AREANAME") or "")
        rent = _number(attributes.get("FMR_2BDR"))
        if not area_name or rent is None:
            continue
        for code in set(code_pattern.findall(area_name.upper())):
            by_state.setdefault(code, []).append(rent)

    observations: list[MarketObservation] = []
    for code, values in by_state.items():
        observations.append(_metric(
            spec=spec,
            download=download,
            metric_key="hud_median_two_bedroom_fmr",
            state_code=code,
            period_end=date(2026, 10, 1),
            value=float(median(values)),
            unit="usd_per_month",
            metadata={
                "aggregation": "median_of_published_fmr_areas",
                "fiscal_year": 2026,
                "source_observations": len(values),
                "publisher_service": "HUD Open Data ArcGIS Feature Service",
            },
        ))
    return observations


def _env_url(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _default_specs() -> list[DatasetSpec]:
    return [
        DatasetSpec(
            key="zillow_research_zhvi",
            url=_env_url(
                "ORACLE_ZILLOW_ZHVI_CSV_URL",
                "https://files.zillowstatic.com/research/public_csvs/zhvi/"
                "State_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
            ),
            parser=parse_zillow_zhvi,
            attribution="Zillow Research",
        ),
        DatasetSpec(
            key="zillow_research_inventory",
            url=_env_url(
                "ORACLE_ZILLOW_INVENTORY_CSV_URL",
                "https://files.zillowstatic.com/research/public_csvs/invt_fs/"
                "State_invt_fs_uc_sfrcondo_sm_month.csv",
            ),
            parser=parse_zillow_inventory,
            attribution="Zillow Research",
        ),
        DatasetSpec(
            key="redfin_market_tracker",
            url=_env_url(
                "ORACLE_REDFIN_MARKET_CSV_URL",
                "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
                "redfin_market_tracker/state_market_tracker.tsv000.gz",
            ),
            parser=parse_redfin_tracker,
            attribution="Redfin Data Center",
        ),
        DatasetSpec(
            key="fhfa_state_hpi",
            url=_env_url(
                "ORACLE_FHFA_HPI_CSV_URL",
                "https://www.fhfa.gov/hpi/download/quarterly_datasets/hpi_at_state.csv",
            ),
            parser=parse_fhfa_state_hpi,
            attribution="Federal Housing Finance Agency",
        ),
        DatasetSpec(
            key="hud_fair_market_rents",
            url=_env_url(
                "ORACLE_HUD_FMR_FEATURE_URL",
                "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
                "Fair_Market_Rents/FeatureServer/0/query",
            ),
            parser=parse_hud_fmr,
            attribution="U.S. Department of Housing and Urban Development",
            downloader=_download_hud_arcgis_sync,
        ),
    ]


def _download_sync(url: str) -> Download:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise ValueError("market dataset URL must use an allow-listed HTTPS publisher host")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/csv,application/octet-stream,*/*"},
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        status = int(getattr(response, "status", 200))
        if status != 200:
            raise RuntimeError(f"publisher returned HTTP {status}")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError("publisher dataset exceeds configured download limit")
        body = response.read(_MAX_DOWNLOAD_BYTES + 1)
        if not body:
            raise RuntimeError("publisher returned an empty dataset")
        if len(body) > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError("publisher dataset exceeds configured download limit")
        updated_at = None
        if response.headers.get("Last-Modified"):
            try:
                updated_at = parsedate_to_datetime(response.headers["Last-Modified"])
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                updated_at = updated_at.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                updated_at = None
    return Download(
        url=url,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        updated_at=updated_at,
    )


def _download_hud_arcgis_sync(url: str) -> Download:
    """Download every HUD FMR feature page from its public official service."""
    parsed = urlsplit(url)
    expected_path = (
        "/VTyQ9soqVukalItT/arcgis/rest/services/"
        "Fair_Market_Rents/FeatureServer/0/query"
    )
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "services.arcgis.com"
        or parsed.path != expected_path
    ):
        raise ValueError("HUD FMR URL must use the allow-listed official feature service")

    features: list[dict[str, Any]] = []
    page_size = 1000
    for offset in range(0, 10_000, page_size):
        query = urlencode({
            "where": "1=1",
            "outFields": "OBJECTID,FMR_AREANAME,FMR_2BDR",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        })
        page = _download_sync(f"{url}?{query}")
        try:
            payload = json.loads(page.body)
        except (TypeError, ValueError) as exc:
            raise ValueError("HUD feature service returned invalid JSON") from exc
        if payload.get("error"):
            raise RuntimeError("HUD feature service returned a query error")
        page_features = payload.get("features")
        if not isinstance(page_features, list):
            raise ValueError("HUD feature service returned an unexpected schema")
        features.extend(page_features)
        if len(page_features) < page_size:
            break
    else:
        raise RuntimeError("HUD feature service exceeded the pagination safety limit")

    if not features:
        raise RuntimeError("HUD feature service returned no FMR records")
    body = json.dumps(
        {"features": features},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(body) > _MAX_DOWNLOAD_BYTES:
        raise RuntimeError("HUD feature dataset exceeds configured download limit")
    return Download(
        url=url,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        updated_at=None,
    )


_UPSERT = """
    INSERT INTO public_market_metrics (
        source_key,metric_key,state_code,geography_name,geography_type,
        period_end,value,unit,source_url,dataset_sha256,dataset_updated_at,
        retrieved_at,metadata
    ) VALUES (
        $1,$2,$3,$4,'state',$5,$6,$7,$8,$9,$10,now(),$11::jsonb
    )
    ON CONFLICT (source_key,metric_key,state_code,period_end) DO UPDATE SET
        geography_name=EXCLUDED.geography_name,
        value=EXCLUDED.value,
        unit=EXCLUDED.unit,
        source_url=EXCLUDED.source_url,
        dataset_sha256=EXCLUDED.dataset_sha256,
        dataset_updated_at=EXCLUDED.dataset_updated_at,
        retrieved_at=now(),
        metadata=EXCLUDED.metadata
"""


class NationwideMarketResearchAgent:
    """Autonomous, bounded sync across public aggregate research publishers."""

    def __init__(
        self,
        specs: Optional[Iterable[DatasetSpec]] = None,
        *,
        concurrency: Optional[int] = None,
        attempts_per_source: int = 1,
        retry_backoff_seconds: float = 0,
    ) -> None:
        self.specs = list(specs or _default_specs())
        configured_concurrency = (
            int(os.getenv("ORACLE_MARKET_DATA_CONCURRENCY", "2"))
            if concurrency is None
            else concurrency
        )
        self._concurrency = max(1, min(4, int(configured_concurrency)))
        self._attempts_per_source = max(1, min(3, int(attempts_per_source)))
        self._retry_backoff_seconds = max(
            0.0, min(30.0, float(retry_backoff_seconds))
        )
        try:
            configured_batch_size = int(
                os.getenv("ORACLE_MARKET_DATA_UPSERT_BATCH_SIZE", "100")
            )
        except ValueError:
            configured_batch_size = 100
        self._upsert_batch_size = max(25, min(500, configured_batch_size))

    async def _collect(
        self,
        spec: DatasetSpec,
        semaphore: asyncio.Semaphore,
    ) -> tuple[DatasetSpec, list[MarketObservation]]:
        for attempt in range(1, self._attempts_per_source + 1):
            try:
                async with semaphore:
                    downloader = spec.downloader or _download_sync
                    download = await asyncio.to_thread(downloader, spec.url)
                rows = await asyncio.to_thread(spec.parser, download, spec)
                if not rows:
                    raise RuntimeError("dataset contained no recognized state observations")
                return spec, rows
            except (TimeoutError, ConnectionError, urllib.error.URLError, OSError):
                if attempt >= self._attempts_per_source:
                    raise
                if self._retry_backoff_seconds:
                    await asyncio.sleep(self._retry_backoff_seconds * attempt)
        raise RuntimeError("source retry loop exited unexpectedly")

    async def sync_once(self, *, persist: bool = True) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(self._concurrency)
        outcomes = await asyncio.gather(
            *(self._collect(spec, semaphore) for spec in self.specs),
            return_exceptions=True,
        )
        observations: list[MarketObservation] = []
        sources: dict[str, dict[str, Any]] = {}
        failures = 0
        for spec, outcome in zip(self.specs, outcomes):
            if isinstance(outcome, Exception):
                failures += 1
                sources[spec.key] = {
                    "status": "failed",
                    "error": f"{type(outcome).__name__}: {str(outcome)[:180]}",
                }
                continue
            _, rows = outcome
            observations.extend(rows)
            covered = sorted({row.state_code for row in rows})
            sources[spec.key] = {
                "status": "ok",
                "observations": len(rows),
                "jurisdictions": len(covered),
                "missing_jurisdictions": sorted(_ALL_JURISDICTIONS - set(covered)),
            }

        persisted_observations = 0
        if persist and observations:
            tenant_id = os.getenv("ORACLE_INGEST_TENANT_ID") or os.getenv(
                "ORACLE_PLATFORM_TENANT_ID",
                "00000000-0000-0000-0000-000000000000",
            )
            from db.connection import tenant_tx
            from tenancy import Role, TenantContext

            ctx = TenantContext(
                agent_id="nationwide-market-research",
                tenant_id=tenant_id,
                role=Role.PLATFORM_ADMIN,
            )
            rows_to_persist = _latest_observations(observations)
            args = [
                (
                    row.source_key,
                    row.metric_key,
                    row.state_code,
                    row.geography_name,
                    row.period_end,
                    row.value,
                    row.unit,
                    row.source_url,
                    row.dataset_sha256,
                    row.dataset_updated_at,
                    json.dumps(row.metadata, separators=(",", ":"), sort_keys=True),
                )
                for row in rows_to_persist
            ]
            # Keep each upsert below asyncpg's bounded command timeout and
            # commit incrementally. A publisher refresh can contain 100k+
            # observations, and a single giant conflict-update transaction is
            # slower to recover and more likely to time out.
            for start in range(0, len(args), self._upsert_batch_size):
                async with tenant_tx(ctx) as conn:
                    await conn.executemany(
                        _UPSERT,
                        args[start:start + self._upsert_batch_size],
                    )
            persisted_observations = len(args)

        covered = sorted({row.state_code for row in observations})
        result: dict[str, Any] = {
            "status": "ok" if failures == 0 else ("partial" if observations else "failed"),
            "sources": sources,
            "observations": len(observations),
            "persisted_observations": persisted_observations,
            "jurisdictions": len(covered),
            "missing_jurisdictions": sorted(_ALL_JURISDICTIONS - set(covered)),
            "persisted": bool(persist and observations),
            "data_classification": "aggregate_market_research",
            "individual_listings": False,
        }
        if failures:
            result["_terminal_state"] = "partial" if observations else "failed"
        return result


async def run_market_research_sync() -> dict[str, Any]:
    # The optional Foundry supervisor can adjust only bounded execution
    # settings and source order. The downloader, publisher hosts, parsers, and
    # persistence path remain deterministic and code-controlled.
    from scraper_supervisor import run_supervised_market_research_sync

    return await run_supervised_market_research_sync()
