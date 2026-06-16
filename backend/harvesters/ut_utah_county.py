"""Utah — Utah County Assessor parcel data (Socrata / opendata.utah.gov).

Dataset : "OwnerParcel with Label"
Portal  : https://opendata.utah.gov
Resource: aim5-7q5v   (283 430 records as of June 2026; assessment year 2027)
Scope   : county:Utah (covers Provo, Orem, Highland, Spanish Fork, American Fork, etc.)

Column map  (source → PropertyRecord):
  parcelid          → parcel_id
  site_full_address → address
  site_city         → city
  site_zip5         → zip_code
  owner_name        → owner_name
  mkt_cur_value     → estimated_value
  own_city / site_city comparison → is_absentee_owner
  asmt_code_descr   → distress_flags (vacant-land heuristic)
  vesting_doc / parcel_create_date → last_sale_date (not present; None)

Notes:
- No last_sale_date field in source; set to None.
- equity_percent unavailable without debt data; set to 0.0.
- Absentee detection: owner mailing city (own_city) differs from site city AND
  owner state is not UT, or own_city plainly differs from site_city.
- Parcels with empty parcel_id or address are skipped.
- The dataset is Utah County only; statewide UGRC LIR layers omit owner_name.
"""
from __future__ import annotations

from typing import Optional

from .base import SocrataHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord

# Heuristic: flag parcels whose property-type indicates vacant / agricultural land
_VACANT_PROP_TYPES = {"300", "400", "500", "600", "700", "800", "900"}


class UtahCountyHarvester(SocrataHarvester):
    STATE = "UT"
    SOURCE_LABEL = "Utah County Assessor — OwnerParcel (Socrata)"
    RESOURCE_URL = "https://opendata.utah.gov/resource/aim5-7q5v.json"
    # Stable sort on objectid so pagination is deterministic
    SOQL_ORDER = "objectid"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("parcelid") or "").strip()
        address = str(row.get("site_full_address") or "").strip()
        if not parcel_id or not address:
            return None

        owner = str(row.get("owner_name") or "").strip()
        if not owner:
            return None  # skip parcels with no owner info

        city = str(row.get("site_city") or "").strip().title()
        zip_code = str(row.get("site_zip5") or "").strip()[:10]

        # Absentee: mailing city/state differs from site city
        own_city = str(row.get("own_city") or "").strip().upper()
        own_state = str(row.get("own_state") or "UT").strip().upper()
        site_city_upper = city.upper()
        is_absentee = (own_state != "UT") or (
            bool(own_city) and own_city != site_city_upper
        )

        # Distress flags: vacant / agricultural land heuristic
        prop_type = str(row.get("prop_type") or "").strip()
        flags: list[str] = []
        if prop_type in _VACANT_PROP_TYPES:
            flags.append("vacant_land")

        # Tax-exempt residential flag (county marks primary-residence exemptions)
        exempt = str(row.get("exempt_res") or "").strip().upper()
        if not exempt or exempt == "N":
            # non-exempt residential — possible absentee / investment property
            if classify_owner(owner) == "individual" and is_absentee:
                flags.append("non_exempt_absentee")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("mkt_cur_value")),
            equity_percent=0.0,          # no debt data in source
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=None,         # vesting_doc is a doc number, not a date
        )
