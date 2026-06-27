"""Connecticut — CT Open Data real estate sales (Socrata JSON).

data.ct.gov publishes statewide real-estate sales with assessed + sale amounts.
We filter to residential property types via SoQL $where.
"""
from __future__ import annotations

from typing import Optional

from .base import SocrataHarvester, norm, to_float
from .property_adapter import PropertyRecord


class ConnecticutSalesHarvester(SocrataHarvester):
    STATE = "CT"
    SOURCE_LABEL = "CT real estate sales (Socrata)"
    RESOURCE_URL = "https://data.ct.gov/resource/5mzw-sjtu.json"
    SOQL_WHERE = "residentialtype IS NOT NULL"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # `serialnumber` is a per-town, per-year sale sequence — NOT a unique
        # parcel key. Keying leads on it collapsed 5000 fetched sales into ~1254
        # rows via the ON CONFLICT(tenant_id, parcel_id) upsert. This dataset
        # carries no APN, so the property's town+street is the stable identity:
        # it yields one lead per property (~4929 of 5000) and idempotently folds
        # repeat sales of the same home into a single, latest-sale-wins lead.
        town = str(row.get("town") or "").strip()
        addr = str(row.get("address") or "").strip()
        if not addr:
            return None
        parcel = f"CT-{norm(town)}-{norm(addr)}"
        if parcel == "CT--":
            return None
        assessed = to_float(row.get("assessedvalue"))
        sale = to_float(row.get("saleamount"))
        # Crude equity proxy: a sale far below assessment signals a distressed/
        # discounted transfer worth surfacing.
        flags = []
        if sale and assessed and sale < assessed * 0.6:
            flags.append("below_assessment")
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=town,
            state=self.STATE,
            zip_code="",
            owner_name="",  # sales file carries no owner name
            owner_type="individual",
            estimated_value=assessed or sale,
            equity_percent=0.0,
            is_absentee_owner=False,
            distress_flags=flags,
            last_sale_date=str(row.get("daterecorded") or "").strip()[:10] or None,
        )
