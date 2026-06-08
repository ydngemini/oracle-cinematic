"""Virginia — VGIN statewide parcels (ArcGIS FeatureServer)."""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class VirginiaVGINHarvester(ArcGISHarvester):
    STATE = "VA"
    SOURCE_LABEL = "VGIN statewide parcels"
    SERVICE_URL = (
        "https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/"
        "VA_Parcels/FeatureServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELID") or row.get("GPIN") or row.get("LOCALPARCELID") or "").strip()
        addr = str(row.get("ADDRESS") or row.get("SITEADDRESS") or row.get("PROPADDR") or "").strip()
        if not parcel or not addr:
            return None
        owner = str(row.get("OWNERNAME") or row.get("OWNER") or "").strip()
        mail = str(row.get("MAILADDR") or row.get("OWNERADDR") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("LOCALITY") or row.get("CITY") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("ZIPCODE") or row.get("ZIP") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("ASSESSEDVALUE") or row.get("TOTALVALUE")),
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("LASTSALEDATE") or "").strip()[:10] or None,
        )
