"""South Dakota — Pennington County (Rapid City) Tax Parcels (ArcGIS FeatureServer).

Source:  Rapid City / Pennington County GIS Open Data
Service: https://gis.rcgov.org/server/rest/services/OpenData/TaxParcels/FeatureServer/0/query
Scope:   county:Pennington  (~SD's 2nd-largest county by population, home of Rapid City)

Key columns
-----------
PIN               → parcel_id
PropAddress       → address  (property situs address)
GranteeCity       → city     (owner mailing city; used as proxy when situs city absent)
GranteeZip        → zip_code
GranteeLastName   → owner_name (combined with Grantee1stName for individual owners)
GranteeFullAddr   → owner mailing street (absentee detection)
ValueTotal        → estimated_value
OwnerOccFlag      → "Y"/"N" owner-occupancy flag
GranteeInstDate   → last_sale_date  (MMDDYY string, normalised to YYYY-MM-DD)
LandUse           → land-use code (A=agricultural, R=residential, C=commercial, …)
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# GranteeInstDate arrives as a 6-char "MMDDYY" string, e.g. "101521" → 2021-10-15.
def _parse_inst_date(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    mm, dd, yy = s[:2], s[2:4], s[4:]
    # Two-digit year: 00-30 → 2000s, 31-99 → 1900s (covers typical deed dates)
    century = "20" if int(yy) <= 30 else "19"
    return f"{century}{yy}-{mm}-{dd}"


class SouthDakotaPenningtonHarvester(ArcGISHarvester):
    STATE = "SD"
    SOURCE_LABEL = "Pennington County SD TaxParcels (OpenData FeatureServer)"
    SERVICE_URL = (
        "https://gis.rcgov.org/server/rest/services/OpenData/TaxParcels/FeatureServer/0/query"
    )
    # Only pull parcels that have a situs address populated.
    WHERE = "PropAddress IS NOT NULL AND PropAddress <> ''"
    OUT_FIELDS = (
        "PIN,PropAddress,GranteeLastName,Grantee1stName,GranteeFullAddr,"
        "GranteeCity,GranteeState,GranteeZip,ValueTotal,OwnerOccFlag,"
        "GranteeInstDate,LandUse"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PIN") or "").strip()
        addr = str(row.get("PropAddress") or "").strip()
        if not parcel or not addr:
            return None

        # Build owner name: "SMITH JOHN" for individuals, company name otherwise.
        last = str(row.get("GranteeLastName") or "").strip()
        first = str(row.get("Grantee1stName") or "").strip()
        owner = f"{last} {first}".strip() if first else last
        if not owner:
            return None

        mail_addr = str(row.get("GranteeFullAddr") or "").strip()
        city = str(row.get("GranteeCity") or "").strip()
        zip_code = str(row.get("GranteeZip") or "").strip()[:10]

        # Absentee: owner mailing address doesn't normalise to the situs address.
        absentee = bool(mail_addr) and norm(mail_addr) != norm(addr)
        # OwnerOccFlag == "Y" overrides our heuristic — treat as non-absentee.
        if str(row.get("OwnerOccFlag") or "").strip().upper() == "Y":
            absentee = False

        value = to_float(row.get("ValueTotal"))

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")
        land_use = str(row.get("LandUse") or "").strip().upper()
        if land_use == "A":
            distress.append("agricultural")  # farm land — often high-equity motivated sellers

        sale_date = _parse_inst_date(str(row.get("GranteeInstDate") or ""))

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,          # not in source; computed downstream
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=sale_date,
        )
