"""Montana — DNRC Statewide Cadastral FeatureServer (ArcGIS).

Source: Montana Department of Natural Resources & Conservation (DNRC) /
        Montana Department of Revenue ORION tax-appraisal database.

Service:  https://gis.dnrc.mt.gov/arcgis/rest/services/DNRALL/Cadastral/FeatureServer/0/query
Coverage: Statewide — all 56 counties (most maintained by MT Dept. of Revenue;
          Ravalli, Silver Bow, Missoula, Flathead, and Yellowstone maintained by
          individual counties and merged by Montana State Library staff).

Column map (source → PropertyRecord):
  PARCELID         → parcel_id
  AddressLine1     → address          (property situs address)
  CityStateZip     → city / zip_code  (parsed: "LIBBY, MT 59923")
  OwnerName        → owner_name
  OwnerCity        → (owner mailing city — used for absentee check)
  OwnerState       → (owner mailing state)
  OwnerZipCode     → zip_code         (owner mailing zip, fallback)
  TotalValue       → estimated_value
  TotalLandValue   → (land component, used when TotalValue == 0)
  TotalBuildingValue → (improvement component)
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# "LIBBY, MT 59923"  or  "HELENA, MT 59601-1234"
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)


def _parse_city_state_zip(raw: str) -> tuple[str, str, str]:
    """Return (city, state, zip) from a 'CITY, ST ZIPCODE' string.  Falls back
    to ('', '', '') if the value is absent or unparseable."""
    raw = (raw or "").strip()
    m = _CITY_STATE_ZIP_RE.match(raw)
    if m:
        return m.group("city").strip().title(), m.group("state"), m.group("zip")
    return "", "", ""


class MontanaCadastralHarvester(ArcGISHarvester):
    """Statewide Montana parcel / assessment data from the DNRC Cadastral
    FeatureServer backed by the MT Department of Revenue ORION database."""

    STATE = "MT"
    SOURCE_LABEL = "MT DNRC Cadastral FeatureServer"

    SERVICE_URL = (
        "https://gis.dnrc.mt.gov/arcgis/rest/services/DNRALL/Cadastral"
        "/FeatureServer/0/query"
    )

    # Only pull parcels that have an owner name and a non-zero assessed value.
    WHERE = "OwnerName IS NOT NULL AND TotalValue > 0"

    OUT_FIELDS = (
        "PARCELID,OwnerName,AddressLine1,CityStateZip,"
        "OwnerCity,OwnerState,OwnerZipCode,"
        "TotalValue,TotalLandValue,TotalBuildingValue"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("PARCELID") or "").strip()
        if not parcel_id or parcel_id == "99999999999999999":
            return None

        owner = str(row.get("OwnerName") or "").strip()
        if not owner:
            return None

        # Property situs address
        address = str(row.get("AddressLine1") or "").strip()

        # Parse "CITY, ST ZIP" for property location
        city_state_zip_raw = str(row.get("CityStateZip") or "").strip()
        prop_city, _prop_state, prop_zip = _parse_city_state_zip(city_state_zip_raw)

        # Owner mailing address — used for absentee detection
        owner_city = str(row.get("OwnerCity") or "").strip()
        owner_state = str(row.get("OwnerState") or "").strip()
        owner_zip = str(row.get("OwnerZipCode") or "").strip()[:10]

        # Assessed / market value
        total_val = to_float(row.get("TotalValue"))
        if total_val == 0.0:
            total_val = (
                to_float(row.get("TotalLandValue"))
                + to_float(row.get("TotalBuildingValue"))
            )

        # Absentee: owner mailing state differs from MT, or mailing zip ≠ property zip
        is_absentee = False
        if owner_state and owner_state.upper() != "MT":
            is_absentee = True
        elif prop_zip and owner_zip and norm(prop_zip[:5]) != norm(owner_zip[:5]):
            is_absentee = True

        distress_flags: list[str] = []
        if is_absentee:
            distress_flags.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=prop_city or owner_city.title(),
            state=self.STATE,
            zip_code=prop_zip or owner_zip,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=total_val,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=distress_flags,
            last_sale_date=None,   # not published in this dataset
        )
