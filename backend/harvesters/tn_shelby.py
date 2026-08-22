"""Tennessee — Shelby County (Memphis) parcel + owner assessment data.

Source: City of Memphis / Shelby County Assessor of Property
Portal: maps.memphistn.gov — ArcGIS MapServer
Layer:  Tolemi_Feed / PARCELOWNERPROPERTY (layer 4)
Scope:  county:Shelby  (largest county in TN by population, ~935 k residents)

Field mapping (all real column names confirmed via live fetch 2026-06-14):
  PARCELID        → parcel_id
  PropertyAddress → address
  OwnerName       → owner_name
  OwnerCityStZip  → city + zip_code  (format: "CITY, STATE, ZIP")
  TotalAppraisal  → estimated_value

Notes:
  - OwnerAddress is the mailing/owner address; PropertyAddress is the situs address.
  - Absentee flag: normalised OwnerAddress != normalised PropertyAddress.
  - City field is parsed from OwnerCityStZip; when blank falls back to "Memphis".
  - No last-sale-date field is exposed by this layer.
  - MaxRecordCount = 2000; exceededTransferLimit is True → pagination is active.
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# The MapServer query endpoint (layer 4 = PARCELOWNERPROPERTY).
_ENDPOINT = (
    "https://maps.memphistn.gov/mapping/rest/services/"
    "Tolemi/Tolemi_Feed/MapServer/4/query"
)


def _parse_city_zip(city_state_zip: str) -> tuple[str, str]:
    """Split 'MEMPHIS, TN, 38103' → ('MEMPHIS', '38103').

    Handles edge cases where the field is empty, has only two parts, or the
    zip segment contains a +4 suffix (38103-1234 → 38103).
    """
    parts = [p.strip() for p in city_state_zip.split(",")]
    city = parts[0].title() if parts else ""
    zip_code = ""
    if len(parts) >= 3:
        raw_zip = parts[-1].strip()
        # keep only the 5-digit base
        m = re.search(r"(\d{5})", raw_zip)
        zip_code = m.group(1) if m else raw_zip[:10]
    return city, zip_code


class TennesseeShelbyHarvester(ArcGISHarvester):
    """Shelby County (Memphis), TN — Assessor parcels via ArcGIS MapServer."""

    STATE = "TN"
    SOURCE_LABEL = "Shelby County Assessor (Memphis TN)"

    # ArcGISHarvester reads SERVICE_URL from env TN_SOURCE_URL first, then falls
    # back to this constant.
    SERVICE_URL = _ENDPOINT

    # Restrict to records that have an owner name and a non-zero appraisal so we
    # don't waste INSERT budget on empty / government right-of-way parcels.
    WHERE = "OwnerName IS NOT NULL AND TotalAppraisal > 0"

    # Only pull the six fields we actually use — keeps each page response small.
    OUT_FIELDS = (
        "PARCELID,OwnerName,OwnerAddress,OwnerCityStZip,PropertyAddress,TotalAppraisal"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("PARCELID") or "").strip()
        address = str(row.get("PropertyAddress") or "").strip()
        owner = str(row.get("OwnerName") or "").strip()

        if not parcel_id or not owner:
            return None

        owner_addr = str(row.get("OwnerAddress") or "").strip()
        is_absentee = bool(owner_addr) and norm(owner_addr) != norm(address)

        # OwnerCityStZip is the OWNER's mailing city and ZIP, as this module's
        # own field map says. For an owner-occupied parcel that is also the
        # property's; for an absentee owner it is somewhere else entirely, and
        # writing it into the property record put Dover, Delaware's 19901 onto
        # 48 parcels in Dover, Tennessee. Same rule wy_parcels.py already
        # applies: use it when the owner lives there, leave it blank otherwise.
        city_zip_raw = str(row.get("OwnerCityStZip") or "").strip()
        parsed_city, parsed_zip = _parse_city_zip(city_zip_raw)
        city = "" if is_absentee else parsed_city
        zip_code = "" if is_absentee else parsed_zip
        # Defaulting to "Memphis" asserted a location the row does not carry.
        # Shelby County contains six other municipalities.

        value = to_float(row.get("TotalAppraisal"))

        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        if value == 0.0:
            flags.append("zero_appraisal")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,       # not available from this layer
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=None,      # not exposed by PARCELOWNERPROPERTY layer
        )
