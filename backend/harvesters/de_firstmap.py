"""Delaware — FirstMap statewide parcels (ArcGIS FeatureServer)."""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class DelawareFirstMapHarvester(ArcGISHarvester):
    STATE = "DE"
    SOURCE_LABEL = "FirstMap statewide parcels"
    SERVICE_URL = (
        "https://enterprise.firstmap.delaware.gov/arcgis/rest/services/"
        "Society/DE_Parcels/MapServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELID") or row.get("PIN") or row.get("OBJECTID") or "").strip()
        addr = str(row.get("PROPADDR") or row.get("SITUS") or row.get("SITE_ADDR") or "").strip()
        if not parcel or not addr:
            return None
        owner = str(row.get("OWNER") or row.get("OWNER_NAME") or "").strip()
        mail = str(row.get("MAILADDR") or row.get("OWNER_ADDR") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("CITY") or row.get("MUNICIPALITY") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("ZIPCODE") or row.get("ZIP") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("ASSESSEDVALUE") or row.get("ASSDVALUE")),
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("SALEDATE") or "").strip() or None,
        )
