"""Checkpointed Socrata/municipal JSON connector with schema-drift checks."""

from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from data_integrations.base import DataSource, RateLimiter, RetryConfig


class MunicipalJSONConnector(DataSource):
    def __init__(
        self,
        *,
        source_name: str,
        resource_url: str,
        cache,
        required_fields: tuple[str, ...],
        order_field: str,
        where: str = "",
        page_size: int = 1_000,
    ) -> None:
        if not resource_url.startswith("https://"):
            raise ValueError("municipal resource_url must use HTTPS")
        self.source_name = source_name
        self.resource_url = resource_url
        self.required_fields = required_fields
        self.order_field = order_field
        self.where = where
        self.page_size = max(1, min(5_000, page_size))
        super().__init__(
            rate_limiter=RateLimiter(min_interval=1.0, jitter=0.25),
            retry_config=RetryConfig(max_attempts=5),
            cache=cache,
        )

    async def fetch(self, **kwargs: Any) -> Optional[dict]:
        cursor = kwargs.get("cursor")
        where = self.where
        if cursor not in (None, ""):
            escaped = str(cursor).replace("'", "''")
            clause = f"{self.order_field}>{escaped}" if escaped.isdigit() else f"{self.order_field}>'{escaped}'"
            where = f"({where}) AND {clause}" if where else clause
        params = {"$limit": self.page_size, "$order": self.order_field}
        if where:
            params["$where"] = where
        raw = await self._get_json(
            f"{self.resource_url}?{urllib.parse.urlencode(params)}", timeout=30
        )
        return {"records": raw} if isinstance(raw, list) else None

    def normalize(self, raw: dict) -> dict:
        records = []
        malformed = 0
        for row in raw.get("records") or []:
            if not isinstance(row, dict) or any(field not in row for field in self.required_fields):
                malformed += 1
                continue
            records.append(dict(row))
        cursor = str(records[-1].get(self.order_field)) if records else None
        return {"records": records, "cursor": cursor, "malformed": malformed}

    def _cache_ttl(self) -> int:
        return 30 * 60

    async def fetch_page(self, cursor: Optional[str] = None) -> dict[str, Any]:
        return await self.get(f"cursor:{cursor or 'start'}", cursor=cursor) or {
            "records": [],
            "cursor": cursor,
            "malformed": 0,
        }
