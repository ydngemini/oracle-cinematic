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
        "PlanningCadastre/DE_StateParcels/FeatureServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PIN") or row.get("OBJECTID") or "").strip()
        if not parcel:
            return None
        county = str(row.get("COUNTY") or "").strip()
        return PropertyRecord(
            parcel_id=parcel,
            address=f"PIN {parcel}",
            city=county,
            state=self.STATE,
            zip_code="",
            owner_name="",
            owner_type="unknown",
            estimated_value=0.0,
            equity_percent=0.0,
            is_absentee_owner=False,
            distress_flags=[],
            last_sale_date=None,
        )
