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
        "https://services.wvgis.wvu.edu/arcgis/rest/services/Cadastral/"
        "WV_Parcels/MapServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARID") or row.get("PARCELID") or "").strip()
        addr = str(row.get("PADDR") or row.get("SITEADDR") or "").strip()
        if not parcel or not addr:
            return None
        owner = " ".join(filter(None, [row.get("OWNERNME1"), row.get("OWNERNME2")])).strip()
        mail = str(row.get("MADDR") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("CITYNAME") or row.get("MUNI") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("ZIP") or row.get("ZIPCODE") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("ASSDTTL") or row.get("TOTAL_VAL")),
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("SALEDT") or "").strip()[:10] or None,
        )
