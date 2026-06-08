"""Massachusetts — MassGIS standardized assessor parcels (ArcGIS FeatureServer).

The L3 standardized assessor schema: LOC_ID, OWNER1, SITE_ADDR, plus the
assessor extract fields (TOTAL_VAL, USE_CODE, OWN_ADDR mailing).
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# MA state class/use codes 101–109 are residential.
def _is_residential(use_code: str) -> bool:
    uc = str(use_code or "").strip()[:3]
    return uc.startswith("1") and uc not in ("", "1")


class MassachusettsMassGISHarvester(ArcGISHarvester):
    STATE = "MA"
    SOURCE_LABEL = "MassGIS L3 assessor parcels"
    SERVICE_URL = (
        "https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/"
        "Massachusetts_Property_Tax_Parcels/FeatureServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("LOC_ID") or row.get("MAP_PAR_ID") or "").strip()
        addr = str(row.get("SITE_ADDR") or "").strip()
        if not parcel or not addr:
            return None
        use_code = row.get("USE_CODE")
        if use_code and not _is_residential(use_code):
            return None
        owner = str(row.get("OWNER1") or "").strip()
        mail = str(row.get("OWN_ADDR") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("CITY") or row.get("TOWN") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("ZIP") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("TOTAL_VAL")),
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("LS_DATE") or "").strip()[:10] or None,
        )
