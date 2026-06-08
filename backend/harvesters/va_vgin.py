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
        parcel = str(row.get("PARCELID") or row.get("VGIN_QPID") or "").strip()
        if not parcel:
            return None
        locality = str(row.get("LOCALITY") or "").strip()
        return PropertyRecord(
            parcel_id=parcel,
            address=f"PIN {parcel}",
            city=locality,
            state=self.STATE,
            zip_code="",
            owner_name="",
            owner_type="individual",  # VGIN exposes no owner field; default per schema
            estimated_value=0.0,
            equity_percent=0.0,
            is_absentee_owner=False,
            distress_flags=[],
            last_sale_date=None,
        )
