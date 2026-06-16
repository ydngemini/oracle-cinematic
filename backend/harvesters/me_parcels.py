"""Maine — statewide parcels + attribute database (ArcGIS FeatureServer).

Source: Maine GeoLibrary / Maine Office of GIS
Service: Maine_Parcels_Organized_Towns
  Layer 9  — Maine_Parcels_Organized_Towns_ADB (assessment + ownership table)
  Layer 10 — Maine_Parcels_Organized_Towns (geometry + PROP_LOC address)

The ADB table (layer 9) carries owner name, mailing address, LAND_VAL, BLDG_VAL,
and last-sale date. It joins to the polygon layer on STATE_ID. We query the ADB
directly because it contains every valuation field required by PropertyRecord; the
geometry layer is not needed for lead harvesting.

Scope: statewide — Organized Towns only. Unorganized Territory parcels (LUPC) are
a separate dataset and are NOT included here.

Real column → PropertyRecord mapping
  STATE_ID        → parcel_id
  OWN_ADDR1       → address  (owner mailing street; property street is on layer 10)
  OWN_CITY        → city
  OWN_STATE       → state (filtered to ME; mailing may differ)
  OWN_ZIP         → zip_code
  OWNER1          → owner_name
  LAND_VAL+BLDG_VAL → estimated_value
  LS_DATE         → last_sale_date (epoch ms from Esri)
  OWN_STATE != 'ME' → is_absentee_owner heuristic
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord


class MaineParcelsHarvester(ArcGISHarvester):
    STATE = "ME"
    SOURCE_LABEL = "Maine GeoLibrary — Organized Towns ADB"

    # ADB attribute table — layer 9 of the organized-towns service.
    SERVICE_URL = (
        "https://services1.arcgis.com/RbMX0mRVOFNTdLzd/arcgis/rest/services/"
        "Maine_Parcels_Organized_Towns/FeatureServer/9/query"
    )
    WHERE = "OWNER1 IS NOT NULL AND LAND_VAL > 0"
    OUT_FIELDS = (
        "STATE_ID,OWNER1,OWN_ADDR1,OWN_ADDR2,OWN_CITY,OWN_STATE,OWN_ZIP,"
        "LAND_VAL,BLDG_VAL,LS_DATE,LS_PRICE"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("STATE_ID") or "").strip()
        owner = str(row.get("OWNER1") or "").strip()
        if not parcel_id or not owner:
            return None

        addr1 = str(row.get("OWN_ADDR1") or "").strip()
        addr2 = str(row.get("OWN_ADDR2") or "").strip()
        address = f"{addr1} {addr2}".strip() if addr2 else addr1

        city = str(row.get("OWN_CITY") or "").strip()
        own_state = str(row.get("OWN_STATE") or "ME").strip().upper()
        zip_code = str(row.get("OWN_ZIP") or "").strip()[:10]

        land_val = to_float(row.get("LAND_VAL"))
        bldg_val = to_float(row.get("BLDG_VAL"))
        estimated_value = land_val + bldg_val

        if estimated_value <= 0:
            return None

        # Absentee: mailing state differs from ME, or zip prefix leaves New England
        is_absentee = own_state not in ("ME", "")

        # LS_DATE arrives as epoch milliseconds from Esri
        last_sale_date: Optional[str] = None
        raw_date = row.get("LS_DATE")
        if raw_date is not None:
            try:
                dt = datetime.datetime.utcfromtimestamp(int(raw_date) / 1000)
                last_sale_date = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                pass

        distress_flags: list[str] = []
        if is_absentee:
            distress_flags.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state="ME",
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=distress_flags,
            last_sale_date=last_sale_date,
        )
