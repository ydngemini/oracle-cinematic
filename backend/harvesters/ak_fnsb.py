"""Alaska — Fairbanks North Star Borough (FNSB) Tax Parcels (ArcGIS FeatureServer).

Source:  Fairbanks North Star Borough Assessor
         https://services.arcgis.com/f4rR7WnIfGBdVYFd/arcgis/rest/services/Tax_Parcels/FeatureServer/0
Scope:   county:Fairbanks North Star Borough  (~63 835 parcels, Tax_Year 2025)

Column map
----------
  parcel_id       <- PAN              (integer parcel account number)
  address         <- Mailing_Address  (mailing street; SITUS_NAME is situs when available)
  city            <- CityStateZip     (parsed — e.g. "FAIRBANKS, AK 99709-4609")
  zip_code        <- CityStateZip     (parsed)
  owner_name      <- Owner1           (+ Owner2/Owner3 appended when populated)
  estimated_value <- Total_Value      (assessed total = Land_Value + Improvements)
  last_sale_date  <- not published in this dataset (None)

Note: CityStateZip is a single combined field.  We split on the last two whitespace-
separated tokens to extract the zip, then strip the state abbreviation to get city.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# Regex to pull zip (5- or 9-digit) from the trailing portion of CityStateZip.
_ZIP_RE = re.compile(r"(\d{5})(?:[- ]\d{4})?$")
# Regex to strip the two-letter state abbreviation that precedes the zip.
_ST_ABBR_RE = re.compile(r"\bAK\b", re.IGNORECASE)


def _parse_city_zip(raw: str) -> tuple[str, str]:
    """Split 'FAIRBANKS AK 99709-4609' → ('FAIRBANKS', '99709')."""
    raw = (raw or "").strip()
    zip_code = ""
    m = _ZIP_RE.search(raw)
    if m:
        zip_code = m.group(1)
        raw = raw[: m.start()].strip()
    # Remove state abbreviation
    raw = _ST_ABBR_RE.sub("", raw).strip().rstrip(",").strip()
    return raw, zip_code


class AlaskaFNSBHarvester(ArcGISHarvester):
    """Fairbanks North Star Borough tax-parcel assessments (AK)."""

    STATE = "AK"
    SOURCE_LABEL = "FNSB Tax Parcels (ArcGIS)"

    # Confirmed live 2025-06-14.  63 835 records, max 2 000/page.
    SERVICE_URL = (
        "https://services.arcgis.com/f4rR7WnIfGBdVYFd/arcgis/rest/"
        "services/Tax_Parcels/FeatureServer/0/query"
    )

    # Only pull the columns we actually use — reduces payload size.
    OUT_FIELDS = (
        "PAN,Owner1,Owner2,Owner3,Mailing_Address,CityStateZip,"
        "Total_Value,Land_Value,Improvements,Assessing_Primary_Use,Tax_Year"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # ── Parcel ID ──────────────────────────────────────────────────────────
        pan = row.get("PAN")
        parcel_id = str(pan).strip() if pan is not None else ""
        if not parcel_id or parcel_id == "None":
            return None

        # ── Owner ──────────────────────────────────────────────────────────────
        o1 = str(row.get("Owner1") or "").strip()
        o2 = str(row.get("Owner2") or "").strip()
        o3 = str(row.get("Owner3") or "").strip()
        owner_name = " / ".join(filter(None, [o1, o2, o3]))
        if not owner_name:
            return None  # no owner → skip

        # ── Address ────────────────────────────────────────────────────────────
        addr = str(row.get("Mailing_Address") or "").strip()
        # Fall back to an empty string; downstream still receives the parcel.
        city_raw = str(row.get("CityStateZip") or "").strip()
        city, zip_code = _parse_city_zip(city_raw)

        # ── Value ──────────────────────────────────────────────────────────────
        total = to_float(row.get("Total_Value"))
        land = to_float(row.get("Land_Value"))
        impr = to_float(row.get("Improvements"))
        estimated_value = total if total else (land + impr)

        # ── Absentee / distress ────────────────────────────────────────────────
        # Absentee: mailing zip differs from AK, OR mailing city not Fairbanks area.
        mailing_in_ak = "AK" in city_raw.upper()
        is_absentee = not mailing_in_ak or (
            bool(addr) and bool(city) and norm(city) not in (
                "fairbanks", "northpole", "ester", "fox", "salcha",
                "tworivers", "deltajunction", "nenana",
            )
        )

        distress: list[str] = []
        if is_absentee:
            distress.append("absentee_owner")

        use = str(row.get("Assessing_Primary_Use") or "").strip().lower()
        if "vacant" in use:
            distress.append("vacant_land")
        if "exempt" in use:
            distress.append("tax_exempt")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner_name,
            owner_type=classify_owner(owner_name),
            estimated_value=estimated_value,
            equity_percent=0.0,   # mortgage data not published in this dataset
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=None,  # sale history not in this endpoint
        )
