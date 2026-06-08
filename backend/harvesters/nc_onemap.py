"""North Carolina — NC OneMap statewide parcels (ArcGIS FeatureServer)."""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class NorthCarolinaOneMapHarvester(ArcGISHarvester):
    STATE = "NC"
    SOURCE_LABEL = "NC OneMap parcels"
    SERVICE_URL = (
        "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/"
        "FeatureServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("parno") or row.get("altparno") or "").strip()
        addr = str(row.get("siteadd") or "").strip()
        if not parcel or not addr:
            return None
        owner = str(row.get("ownname") or "").strip()
        mail = str(row.get("mailadd") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        value = to_float(row.get("parval")) or (
            to_float(row.get("landval")) + to_float(row.get("improvval"))
        )
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("scity") or row.get("mcity") or row.get("cntyname") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("szip") or row.get("mzip") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("saledatetx") or "").strip()[:10] or None,
        )
