"""Vermont — VCGI statewide parcels joined with Tax Department Grand List (ArcGIS FeatureServer).

Source:  Vermont Center for Geographic Information (VCGI)
         FS_VCGI_OPENDATA_Cadastral_VTPARCELS_poly_standardized_parcels_SP_v1
Portal:  https://geodata.vermont.gov/  (ArcGIS org: BkFxaEFNwHqX3tAw)
Scope:   Statewide — all Vermont municipalities (~245k+ active parcels)
Updated: Annually (Grand List year); geometry updated more frequently.

Column map
----------
parcel_id       <- PARCID   (e.g. "01-0178.1") + SPAN fallback
address         <- E911ADDR (911 civic address; falls back to ADDRGL1 mailing)
city            <- TNAME    (Grand-List town name, properly capitalised)
zip_code        <- ZIPGL    (mailing ZIP)
owner_name      <- OWNER1   (primary owner; OWNER2 appended when present)
estimated_value <- REAL_FLV (full listed real value from Grand List)
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord


class VermontVCGIHarvester(ArcGISHarvester):
    STATE = "VT"
    SOURCE_LABEL = "VCGI statewide parcels (Grand List)"

    # ArcGIS FeatureServer — layer 0 is the active-parcel polygon layer.
    SERVICE_URL = (
        "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/ArcGIS/rest/services/"
        "FS_VCGI_OPENDATA_Cadastral_VTPARCELS_poly_standardized_parcels_SP_v1/"
        "FeatureServer/0/query"
    )

    # Only pull records that have a real owner and a non-zero assessed value.
    WHERE = "OWNER1 IS NOT NULL AND REAL_FLV > 0"

    OUT_FIELDS = (
        "PARCID,SPAN,OWNER1,OWNER2,"
        "ADDRGL1,ADDRGL2,CITYGL,STGL,ZIPGL,"
        "E911ADDR,TNAME,TOWN,"
        "REAL_FLV,LAND_LV,IMPRV_LV"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # --- Parcel ID ---
        parcel_id = str(row.get("PARCID") or row.get("SPAN") or "").strip()
        if not parcel_id:
            return None

        # --- Owner ---
        owner1 = str(row.get("OWNER1") or "").strip()
        owner2 = str(row.get("OWNER2") or "").strip()
        owner_name = f"{owner1} & {owner2}" if owner2 else owner1
        if not owner_name:
            return None

        # --- Address (prefer 911 civic; fall back to mailing) ---
        e911 = str(row.get("E911ADDR") or "").strip()
        mail1 = str(row.get("ADDRGL1") or "").strip()
        address = e911 if e911 else mail1

        # --- City / ZIP ---
        city = str(row.get("TNAME") or row.get("TOWN") or row.get("CITYGL") or "").strip()
        zip_code = str(row.get("ZIPGL") or "").strip()

        # --- Values ---
        estimated_value = to_float(row.get("REAL_FLV"))
        land_value = to_float(row.get("LAND_LV"))
        imprv_value = to_float(row.get("IMPRV_LV"))

        # Equity heuristic: improvement ratio (high land / low improvement = distressed).
        # VT Grand List has no mortgage data, so equity_percent is left 0 (unknown).
        equity_percent = 0.0

        # --- Absentee detection: mailing city differs from town ---
        mail_city = str(row.get("CITYGL") or "").strip().upper()
        town_upper = (row.get("TNAME") or row.get("TOWN") or "").upper()
        mail_state = str(row.get("STGL") or "").strip().upper()
        is_absentee = (mail_state != "VT") or (bool(mail_city) and mail_city != town_upper)

        # --- Distress flags ---
        distress_flags: list[str] = []
        if imprv_value == 0 and estimated_value > 0:
            distress_flags.append("vacant_land")
        if estimated_value > 0 and imprv_value > 0 and (imprv_value / estimated_value) < 0.15:
            distress_flags.append("low_improvement_ratio")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner_name,
            owner_type=classify_owner(owner_name),
            estimated_value=estimated_value,
            equity_percent=equity_percent,
            is_absentee_owner=is_absentee,
            distress_flags=distress_flags,
            last_sale_date=None,  # Not present in this layer; join PTTR_Parcels_Sales_Join for sales dates
        )
