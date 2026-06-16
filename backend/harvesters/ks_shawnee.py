"""Kansas — Shawnee County Appraiser parcels (ArcGIS MapServer, queryable).

Source: Shawnee County GIS / Appraiser's Office
Endpoint: https://gis.sncoapps.us/arcgis2/rest/services/Appraiser/AppraisalDataPro/MapServer/4/query
Coverage: Shawnee County, KS (county seat: Topeka) — ~78,000 parcels
Update cadence: weekly (per county GIS data-layers page)

Column map (real field → PropertyRecord):
  PID          → parcel_id
  PADDRESS     → address
  PADDRESS2    → city + zip_code  (format: " <City>, KS  <ZIP>")
  ONAME        → owner_name
  TOTVAL       → estimated_value   (LDVAL land + BLDGVAL building combined)
  QUICKREFID   → secondary parcel ref (stored in distress_flags context if needed)

No separate mailing-address field is exposed publicly, so absentee_owner is
detected by checking whether ONAME is a clearly non-residential entity that
doesn't match a residential address pattern (corporate/trust → absentee).
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# PADDRESS2 example: " Harveyville, KS  66431"
_PADDRESS2_RE = re.compile(
    r"^\s*(?P<city>[^,]+),\s*KS\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$",
    re.IGNORECASE,
)


def _parse_city_zip(paddress2: str) -> tuple[str, str]:
    """Extract city and ZIP from the combined PADDRESS2 field."""
    m = _PADDRESS2_RE.match((paddress2 or "").strip())
    if m:
        return m.group("city").strip().title(), m.group("zip").strip()
    return "", ""


class KansasShawneeHarvester(ArcGISHarvester):
    """Shawnee County, KS Appraiser — ArcGIS MapServer layer 4 (Current Parcels)."""

    STATE = "KS"
    SOURCE_LABEL = "Shawnee County Appraiser MapServer"

    # MapServer/4 supports standard ArcGIS query parameters identical to FeatureServer.
    SERVICE_URL = (
        "https://gis.sncoapps.us/arcgis2/rest/services/Appraiser/"
        "AppraisalDataPro/MapServer/4/query"
    )

    # Request only the fields we consume — faster transfer, avoids geometry bloat.
    OUT_FIELDS = (
        "PID,QUICKREFID,PARCELNUM,ONAME,"
        "PADDRESS,PADDRESS2,"
        "LDVAL,BLDGVAL,TOTVAL"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PID") or row.get("QUICKREFID") or row.get("PARCELNUM") or "").strip()
        addr = str(row.get("PADDRESS") or "").strip()
        if not parcel or not addr:
            return None

        owner = str(row.get("ONAME") or "").strip()
        if not owner:
            return None

        city, zip_code = _parse_city_zip(str(row.get("PADDRESS2") or ""))

        # TOTVAL = LDVAL + BLDGVAL (appraised market value under KS law).
        # Fall back to summing components if TOTVAL is absent/zero.
        value = to_float(row.get("TOTVAL"))
        if not value:
            value = to_float(row.get("LDVAL")) + to_float(row.get("BLDGVAL"))

        owner_type = classify_owner(owner)
        # Absentee: public layer has no separate mailing address, so we flag
        # corporate/trust owners as likely absentee — a conservative signal.
        is_absentee = owner_type in ("corporate", "trust")

        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        if value == 0.0:
            flags.append("no_value")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=owner_type,
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=None,  # not exposed in this layer
        )
