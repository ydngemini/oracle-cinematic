"""Checkpointed ArcGIS FeatureServer connector with mandatory caching."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from data_integrations.base import DataSource, RateLimiter, RetryConfig


@dataclass(frozen=True)
class ArcGISPage:
    records: list[dict[str, Any]]
    next_offset: Optional[int]
    exceeded_transfer_limit: bool


class ArcGISConnector(DataSource):
    def __init__(
        self,
        *,
        source_name: str,
        service_url: str,
        cache,
        out_fields: str = "*",
        where: str = "1=1",
        page_size: int = 1_000,
    ) -> None:
        if not service_url.startswith("https://"):
            raise ValueError("ArcGIS service_url must use HTTPS")
        if not source_name or not source_name.replace("_", "").isalnum():
            raise ValueError("source_name must be alphanumeric/underscore")
        self.source_name = source_name
        self.service_url = service_url.rstrip("/")
        self.out_fields = out_fields
        self.where = where
        self.page_size = max(1, min(2_000, page_size))
        super().__init__(
            rate_limiter=RateLimiter(min_interval=1.0, jitter=0.25),
            retry_config=RetryConfig(max_attempts=5),
            cache=cache,
        )

    async def fetch(self, **kwargs: Any) -> Optional[dict]:
        offset = max(0, int(kwargs.get("offset", 0)))
        params = {
            "where": self.where,
            "outFields": self.out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": self.page_size,
            "f": "json",
        }
        data = await self._get_json(
            f"{self.service_url}/query?{urllib.parse.urlencode(params)}",
            timeout=30,
        )
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"ArcGIS error {data['error'].get('code')}")
        return data if isinstance(data, dict) else None

    def normalize(self, raw: dict) -> dict:
        records = []
        for feature in raw.get("features") or []:
            if not isinstance(feature, Mapping):
                continue
            record = dict(feature.get("attributes") or {})
            geometry = feature.get("geometry")
            if isinstance(geometry, Mapping):
                record["_geometry"] = dict(geometry)
            records.append(record)
        return {
            "records": records,
            "exceeded_transfer_limit": bool(raw.get("exceededTransferLimit")),
        }

    def _cache_ttl(self) -> int:
        return 7 * 86_400

    async def fetch_page(self, offset: int = 0) -> ArcGISPage:
        data = await self.get(f"offset:{offset}", offset=offset)
        data = data or {"records": [], "exceeded_transfer_limit": False}
        records = list(data.get("records") or [])
        exceeded = bool(data.get("exceeded_transfer_limit"))
        next_offset = offset + len(records) if records and (exceeded or len(records) >= self.page_size) else None
        return ArcGISPage(records, next_offset, exceeded)

    async def harvest(self, *, checkpoint: int = 0, max_records: Optional[int] = None) -> dict:
        records: list[dict[str, Any]] = []
        offset: Optional[int] = max(0, checkpoint)
        while offset is not None:
            page = await self.fetch_page(offset)
            remaining = None if max_records is None else max_records - len(records)
            records.extend(page.records if remaining is None else page.records[:remaining])
            if max_records is not None and len(records) >= max_records:
                offset = offset + len(page.records)
                break
            offset = page.next_offset
        return {
            "records": records,
            "checkpoint": offset,
            "complete": offset is None,
            "cache": self._cache.metrics(),
        }
