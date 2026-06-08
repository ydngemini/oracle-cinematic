"""New York — NYC PLUTO tax-lot extract (Socrata JSON).

NYC Open Data publishes PLUTO (Primary Land Use Tax Lot Output) as a Socrata
resource. Residential lots are filtered by building class (A/B/C/D/R...).
"""
from __future__ import annotations

from typing import Optional

from .base import SocrataHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord

_RESIDENTIAL_CLASS_PREFIXES = ("A", "B", "C", "D", "R", "S")


class NewYorkPlutoHarvester(SocrataHarvester):
    STATE = "NY"
    SOURCE_LABEL = "NYC PLUTO (Socrata)"
    RESOURCE_URL = "https://data.cityofnewyork.us/resource/64uk-42ks.json"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("bbl") or row.get("borough_block_lot") or "").strip()
        addr = str(row.get("address") or "").strip()
        if not parcel or not addr:
            return None
        bldg = str(row.get("bldgclass") or "").strip().upper()
        if bldg and bldg[0] not in _RESIDENTIAL_CLASS_PREFIXES:
            return None  # filter to residential building classes
        owner = str(row.get("ownername") or "").strip()
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("borough") or "New York").strip(),
            state=self.STATE,
            zip_code=str(row.get("zipcode") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("assesstot")),
            equity_percent=0.0,
            # PLUTO carries no owner mailing address — absentee not derivable.
            is_absentee_owner=False,
            distress_flags=[],
            last_sale_date=None,
        )
