"""New Jersey — MOD-IV / NJGIN statewide parcels (ArcGIS FeatureServer)."""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class NewJerseyModIVHarvester(ArcGISHarvester):
    STATE = "NJ"
    SOURCE_LABEL = "NJGIN MOD-IV parcels"
    SERVICE_URL = (
        "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/ArcGIS/rest/services/"
        "Parcels_Composite_NJ_WM/FeatureServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("GIS_PIN") or row.get("PAMS_PIN") or row.get("PCL_MUN") or "").strip()
        addr = str(row.get("PROP_LOC") or row.get("ST_ADDRESS") or "").strip()
        if not parcel or not addr:
            return None
        owner = str(row.get("OWNER_NAME") or "").strip()
        # MOD-IV mailing address fields: MAIL_ADD / OWN_MAIL are the owner mailing
        # street, separate from ST_ADDRESS (the property's own street address).
        # Falling back to ST_ADDRESS caused addr==mail false negatives when PROP_LOC absent.
        mail = str(row.get("MAIL_ADD") or row.get("OWN_MAIL") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        # MOD-IV splits value into land + improvement.
        value = to_float(row.get("NET_VALUE")) or (
            to_float(row.get("LAND_VAL")) + to_float(row.get("IMPRVT_VAL"))
        )
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("MUN_NAME") or row.get("CITY_STATE") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("ZIP_CODE") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("DEED_DATE") or "").strip()[:10] or None,
        )
