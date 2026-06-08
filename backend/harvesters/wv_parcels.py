"""West Virginia — WV statewide parcels (ArcGIS FeatureServer).

WV property/parcel layers expose CAMA fields (OWNERNME1, PADDR physical address,
MADDR mailing address, ASSDTTL total assessment).
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class WestVirginiaParcelsHarvester(ArcGISHarvester):
    STATE = "WV"
    SOURCE_LABEL = "WV statewide parcels"
    SERVICE_URL = (
        "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/"
        "WV_Parcels/MapServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("CleanParcelID") or row.get("GISPID") or "").strip()
        addr = str(row.get("FullPhysicalAddress") or "").strip()
        if not parcel or not addr:
            return None
        owner = str(row.get("FullOwnerName") or "").strip()
        mail = str(row.get("FullOwnerAddress") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city="",
            state=self.STATE,
            zip_code="",
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=0.0,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=None,
        )
