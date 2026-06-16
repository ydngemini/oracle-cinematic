"""Florida — FDOR Cadastral 2025 statewide parcels (ArcGIS FeatureServer).

Source : Florida Geospatial Open Data Portal (FGIO)
         https://geodata.floridagio.gov/datasets/FGIO::florida-statewide-parcels
Service: https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/
         Florida_Statewide_Cadastral/FeatureServer/0/query
Scope  : statewide — all 67 Florida counties, ~10.8 M parcels
Updated: annually (August) by Florida Department of Revenue

Column map (source → PropertyRecord):
  PARCEL_ID  → parcel_id
  PHY_ADDR1  → address          (physical/situs street address)
  PHY_CITY   → city
  PHY_ZIPCD  → zip_code
  OWN_NAME   → owner_name
  OWN_STATE  → absentee check   (owner state != "FL")
  JV         → estimated_value  (Just Value / market value)
  SALE_YR1 + SALE_MO1 → last_sale_date
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# Only pull the columns we need — reduces transfer size on a 10 M-row dataset.
_OUT_FIELDS = ",".join([
    "PARCEL_ID",
    "OWN_NAME",
    "OWN_STATE",
    "PHY_ADDR1",
    "PHY_CITY",
    "PHY_ZIPCD",
    "JV",           # Just Value  (market / assessed value)
    "SALE_PRC1",    # Most-recent sale price
    "SALE_YR1",     # Sale year
    "SALE_MO1",     # Sale month (string "01"–"12" or " ")
])


class FloridaFDORHarvester(ArcGISHarvester):
    STATE = "FL"
    SOURCE_LABEL = "FGIO FDOR Cadastral 2025 statewide parcels"
    SERVICE_URL = (
        "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
        "Florida_Statewide_Cadastral/FeatureServer/0/query"
    )
    WHERE = "OWN_NAME IS NOT NULL AND PHY_ADDR1 IS NOT NULL"
    OUT_FIELDS = _OUT_FIELDS

    # ------------------------------------------------------------------
    # Record mapper
    # ------------------------------------------------------------------
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCEL_ID") or "").strip()
        addr   = str(row.get("PHY_ADDR1") or "").strip()
        if not parcel or not addr:
            return None

        owner    = str(row.get("OWN_NAME") or "").strip()
        city     = str(row.get("PHY_CITY") or "").strip()
        zip_raw  = row.get("PHY_ZIPCD")
        zip_code = str(int(zip_raw)).zfill(5) if zip_raw else ""

        # Just Value is the Florida DOR's market/assessed value.
        jv = to_float(row.get("JV"))

        # Absentee: owner's state of record is not FL.
        own_state = str(row.get("OWN_STATE") or "").strip().upper()
        absentee  = bool(own_state) and own_state != "FL"

        # Last sale date: combine SALE_YR1 (numeric) + SALE_MO1 (string).
        yr  = row.get("SALE_YR1")
        mo  = str(row.get("SALE_MO1") or "").strip()
        if yr and int(yr) > 1900 and mo and mo.strip():
            last_sale = f"{int(yr)}-{mo.strip().zfill(2)}"
        else:
            last_sale = None

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id      = parcel,
            address        = addr,
            city           = city,
            state          = self.STATE,
            zip_code       = zip_code[:10],
            owner_name     = owner,
            owner_type     = classify_owner(owner),
            estimated_value= jv,
            equity_percent = 0.0,        # DOR data has no debt/mortgage field
            is_absentee_owner = absentee,
            distress_flags = distress,
            last_sale_date = last_sale,
        )
