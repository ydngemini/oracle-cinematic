"""Live Delaware Valley parcel adapter (DE/PA/NJ/MD), no synthetic rows."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .base import BaseHarvester
from .de_firstmap import DelawareFirstMapHarvester
from .md_sdat_ingest import MdSdatHarvester
from .nj_modiv import NewJerseyModIVHarvester
from .pa_philly_opa import PennsylvaniaPhillyOPAHarvester
from .property_adapter import PropertyHarvester, PropertyRecord

logger = logging.getLogger("oracle.harvester.regional")

_REGIONAL = {
    "DE": DelawareFirstMapHarvester,
    "PA": PennsylvaniaPhillyOPAHarvester,
    "NJ": NewJerseyModIVHarvester,
    "MD": MdSdatHarvester,
}


def state_for_zip(zip_code: str) -> Optional[str]:
    value = str(zip_code or "").strip()
    if len(value) != 5 or not value.isdigit():
        return None
    number = int(value)
    if 19700 <= number <= 19999:
        return "DE"
    if 15000 <= number <= 19699:
        return "PA"
    if 7000 <= number <= 8999:
        return "NJ"
    if 20600 <= number <= 21999:
        return "MD"
    return None


class RegionalParcelAdapter(PropertyHarvester):
    """PropertyHarvester compatibility wrapper over real regional sources."""

    def __init__(self, tenant_id: Optional[str] = None, max_records: int = 2_000):
        self.tenant_id = tenant_id or os.getenv(
            "ORACLE_INGEST_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        )
        self.max_records = max(1, min(10_000, max_records))

    async def fetch_by_zip_async(self, zip_code: str) -> list[PropertyRecord]:
        state = state_for_zip(zip_code)
        if state is None:
            return []
        cls = _REGIONAL[state]
        if state == "MD":
            harvester = cls(self.tenant_id, agent_id="regional-md")
            return await asyncio.to_thread(harvester.fetch_by_zip, zip_code)

        harvester = cls(self.tenant_id, agent_id=f"regional-{state.lower()}")
        if not isinstance(harvester, BaseHarvester):
            return []
        rows = await harvester.fetch_raw(self.max_records)
        records = []
        for row in rows:
            record = harvester.map_record(row)
            if record and str(record.zip_code).startswith(zip_code):
                records.append(record)
        return harvester.aggregate_records(records)

    def fetch_by_zip(self, zip_code: str) -> list[PropertyRecord]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.fetch_by_zip_async(zip_code))
        raise RuntimeError(
            "RegionalParcelAdapter.fetch_by_zip must run in a worker thread; "
            "use fetch_by_zip_async from async code"
        )

    def fetch_cash_buyers(self, state: str, min_purchases: int = 3) -> list[dict]:
        # Cash-buyer ranking lives in the source-backed entity graph.  Returning
        # an empty set is preferable to inventing entities or purchase counts.
        logger.info(
            "Cash-buyer lookup deferred to entity graph (state=%s min=%d)",
            state,
            min_purchases,
        )
        return []


__all__ = ["RegionalParcelAdapter", "state_for_zip"]
