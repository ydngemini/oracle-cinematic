"""
County Assessor Harvester — background worker that scrapes public Delaware County
Assessor and Recorder databases for deeds, mortgages, and historic purchase prices,
injecting raw records into the local Property Graph.

Data Sources:
  - Delaware County Assessor (tax assessments, assessed values)
  - Delaware County Recorder of Deeds (deed transfers, mortgages filed)
  - Public sale records (historic purchase prices, transaction dates)

The harvester runs as an asyncio background task, polling sources on a
configurable interval and streaming discovered records into graph_engine.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import aiohttp

logger = logging.getLogger("oracle.harvester.county_assessor")

ASSESSOR_BASE_URL = "https://www.newcastlede.gov/parcelview"
RECORDER_BASE_URL = "https://recorder.delaware.gov/search"

POLL_INTERVAL_SECONDS = 300
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


class RecordType(str, Enum):
    DEED = "DEED"
    MORTGAGE = "MORTGAGE"
    SALE = "HISTORIC_SALE"
    TAX_ASSESSMENT = "TAX_ASSESSMENT"
    LIEN = "TAX_LIEN"


@dataclass
class PublicRecord:
    record_type: RecordType
    parcel_id: str
    address: str
    owner_name: str
    county: str = "New Castle"
    state: str = "DE"

    assessed_value: Optional[int] = None
    market_value: Optional[int] = None
    sale_price: Optional[int] = None
    sale_date: Optional[str] = None
    mortgage_amount: Optional[int] = None
    mortgage_lender: Optional[str] = None
    deed_book: Optional[str] = None
    deed_page: Optional[str] = None
    recorded_date: Optional[str] = None
    sqft: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    year_built: Optional[int] = None
    lot_size_acres: Optional[float] = None

    raw_payload: dict = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        seed = f"{self.parcel_id}:{self.record_type}:{self.recorded_date or self.sale_date or ''}"
        return hashlib.sha256(seed.encode()).hexdigest()[:20]

    def to_graph_ingest(self) -> dict:
        return {
            "record_type": self.record_type.value,
            "owner_name": self.owner_name,
            "address": self.address,
            "assessed_value": self.assessed_value or 0,
            "market_value": self.market_value or 0,
            "sqft": self.sqft or 0,
            "bedrooms": self.bedrooms or 0,
            "bathrooms": self.bathrooms or 0,
            "county": self.county,
            "state": self.state,
            "equity_pct": self._estimate_equity(),
            "years_owned": self._estimate_years_owned(),
            "purchase_year": self._extract_purchase_year(),
            "mortgage_balance": self.mortgage_amount or 0,
            "sale_price": self.sale_price,
            "sale_date": self.sale_date,
        }

    def _estimate_equity(self) -> int:
        if not self.market_value or not self.mortgage_amount:
            return 50
        equity = ((self.market_value - self.mortgage_amount) / self.market_value) * 100
        return max(0, min(100, int(equity)))

    def _estimate_years_owned(self) -> int:
        if not self.sale_date:
            return 5
        try:
            year = int(self.sale_date[:4])
            return max(0, 2026 - year)
        except (ValueError, IndexError):
            return 5

    def _extract_purchase_year(self) -> int:
        if not self.sale_date:
            return 2015
        try:
            return int(self.sale_date[:4])
        except (ValueError, IndexError):
            return 2015


class CountyAssessorHarvester:
    def __init__(self, graph_engine, websocket=None, cache=None):
        self.graph = graph_engine
        self.websocket = websocket
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._seen_records: set = set()
        self._cache = cache
        self._stats = {"batches": 0, "records_ingested": 0, "errors": 0}

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        )
        logger.info("County Assessor Harvester started")
        try:
            while self._running:
                await self._harvest_cycle()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        finally:
            await self._session.close()

    def stop(self):
        self._running = False
        logger.info("County Assessor Harvester stopping")

    @property
    def stats(self) -> dict:
        return {**self._stats, "unique_records": len(self._seen_records)}

    async def _harvest_cycle(self):
        self._stats["batches"] += 1
        batch_id = self._stats["batches"]

        await self._emit_status(f"SCOUT HARVESTER — cycle #{batch_id}, querying assessor DB")

        deeds = await self._fetch_recent_deeds()
        mortgages = await self._fetch_recent_mortgages()
        assessments = await self._fetch_tax_assessments()
        sales = await self._fetch_historic_sales()

        all_records = deeds + mortgages + assessments + sales
        new_records = [r for r in all_records if r.record_id not in self._seen_records]

        if not new_records:
            await self._emit_status(f"SCOUT HARVESTER — cycle #{batch_id} complete, no new records")
            return

        await self._emit_status(
            f"SCOUT HARVESTER — injecting {len(new_records)} new records into graph"
        )

        for record in new_records:
            try:
                await self.graph.ingest_public_record(record.to_graph_ingest())
                self._seen_records.add(record.record_id)
                self._stats["records_ingested"] += 1
            except Exception as e:
                logger.error(f"Failed to ingest record {record.record_id}: {e}")
                self._stats["errors"] += 1

        await self._emit_status(
            f"SCOUT HARVESTER — {len(new_records)} records ingested (total: {self._stats['records_ingested']})"
        )

    async def _fetch_recent_deeds(self) -> list[PublicRecord]:
        return await self._scrape_endpoint(
            f"{RECORDER_BASE_URL}/deeds",
            params={"county": "new_castle", "days_back": "30", "format": "json"},
            record_type=RecordType.DEED,
            parser=self._parse_deed_record,
        )

    async def _fetch_recent_mortgages(self) -> list[PublicRecord]:
        return await self._scrape_endpoint(
            f"{RECORDER_BASE_URL}/mortgages",
            params={"county": "new_castle", "days_back": "30", "format": "json"},
            record_type=RecordType.MORTGAGE,
            parser=self._parse_mortgage_record,
        )

    async def _fetch_tax_assessments(self) -> list[PublicRecord]:
        return await self._scrape_endpoint(
            f"{ASSESSOR_BASE_URL}/assessments",
            params={"tax_year": "2026", "format": "json"},
            record_type=RecordType.TAX_ASSESSMENT,
            parser=self._parse_assessment_record,
        )

    async def _fetch_historic_sales(self) -> list[PublicRecord]:
        return await self._scrape_endpoint(
            f"{RECORDER_BASE_URL}/sales",
            params={"county": "new_castle", "months_back": "24", "format": "json"},
            record_type=RecordType.SALE,
            parser=self._parse_sale_record,
        )

    async def _scrape_endpoint(
        self, url: str, params: dict, record_type: RecordType, parser
    ) -> list[PublicRecord]:
        if self._session is None:
            logger.error("_scrape_endpoint called before start() — session not initialised")
            return []
        if self._cache is None:
            from data_integrations.cache import get_integration_cache

            self._cache = await get_integration_cache()

        async def fetch_upstream() -> dict:
            for attempt in range(MAX_RETRIES):
                try:
                    async with self._session.get(url, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {"records": data.get("records", data.get("results", []))}
                        if resp.status == 429:
                            wait = 2 ** (attempt + 1)
                            logger.warning("Rate limited on %s, waiting %ss", url, wait)
                            await asyncio.sleep(wait)
                            continue
                        logger.warning("HTTP %s from %s", resp.status, url)
                        return {"records": []}
                except aiohttp.ClientError as exc:
                    logger.error("Request failed (%d/%d): %s", attempt + 1, MAX_RETRIES, exc)
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Unexpected error scraping %s: %s", url, exc)
                    return {"records": []}
            return {"records": []}

        payload = await self._cache.get_or_fetch(
            "county_assessor",
            {"url": url, "params": params, "record_type": record_type.value},
            fetch_upstream,
            ttl=7 * 86_400,
        )
        out: list[PublicRecord] = []
        for item in payload.get("records") or []:
            if not item:
                continue
            try:
                out.append(parser(item))
            except Exception as parse_err:  # noqa: BLE001
                logger.warning("Parser %s skipped malformed item: %s", parser.__name__, parse_err)
        return out

    def _parse_deed_record(self, raw: dict) -> PublicRecord:
        return PublicRecord(
            record_type=RecordType.DEED,
            parcel_id=raw.get("parcel_id", raw.get("pin", "")),
            address=raw.get("property_address", raw.get("address", "")),
            owner_name=raw.get("grantee", raw.get("buyer_name", "")),
            assessed_value=raw.get("assessed_value"),
            market_value=raw.get("consideration", raw.get("sale_amount")),
            sale_price=raw.get("consideration", raw.get("sale_amount")),
            sale_date=raw.get("recorded_date", raw.get("instrument_date")),
            deed_book=raw.get("book"),
            deed_page=raw.get("page"),
            recorded_date=raw.get("recorded_date"),
            sqft=raw.get("living_area", raw.get("sqft")),
            bedrooms=raw.get("bedrooms"),
            bathrooms=raw.get("bathrooms"),
            year_built=raw.get("year_built"),
            lot_size_acres=raw.get("lot_acres"),
            raw_payload=raw,
        )

    def _parse_mortgage_record(self, raw: dict) -> PublicRecord:
        return PublicRecord(
            record_type=RecordType.MORTGAGE,
            parcel_id=raw.get("parcel_id", raw.get("pin", "")),
            address=raw.get("property_address", raw.get("address", "")),
            owner_name=raw.get("borrower", raw.get("mortgagor", "")),
            mortgage_amount=raw.get("mortgage_amount", raw.get("loan_amount")),
            mortgage_lender=raw.get("lender", raw.get("mortgagee")),
            recorded_date=raw.get("recorded_date"),
            raw_payload=raw,
        )

    def _parse_assessment_record(self, raw: dict) -> PublicRecord:
        return PublicRecord(
            record_type=RecordType.TAX_ASSESSMENT,
            parcel_id=raw.get("parcel_id", raw.get("pin", "")),
            address=raw.get("property_address", raw.get("address", "")),
            owner_name=raw.get("owner_name", raw.get("taxpayer", "")),
            assessed_value=raw.get("total_assessed", raw.get("assessed_value")),
            market_value=raw.get("total_market", raw.get("market_value")),
            sqft=raw.get("living_area", raw.get("sqft")),
            bedrooms=raw.get("bedrooms"),
            bathrooms=raw.get("full_baths"),
            year_built=raw.get("year_built"),
            lot_size_acres=raw.get("lot_acres"),
            raw_payload=raw,
        )

    def _parse_sale_record(self, raw: dict) -> PublicRecord:
        return PublicRecord(
            record_type=RecordType.SALE,
            parcel_id=raw.get("parcel_id", raw.get("pin", "")),
            address=raw.get("property_address", raw.get("address", "")),
            owner_name=raw.get("buyer", raw.get("grantee", "")),
            sale_price=raw.get("sale_price", raw.get("consideration")),
            sale_date=raw.get("sale_date", raw.get("recorded_date")),
            market_value=raw.get("sale_price", raw.get("consideration")),
            sqft=raw.get("living_area"),
            bedrooms=raw.get("bedrooms"),
            bathrooms=raw.get("bathrooms"),
            year_built=raw.get("year_built"),
            recorded_date=raw.get("recorded_date"),
            raw_payload=raw,
        )

    async def _emit_status(self, message: str):
        logger.info(message)
        if self.websocket:
            try:
                import json
                await self.websocket.send_text(json.dumps({
                    "type": "STATUS_UPDATE",
                    "agent": message,
                }))
            except Exception:
                pass
