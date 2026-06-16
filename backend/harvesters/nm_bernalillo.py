"""New Mexico — Bernalillo County Assessor parcels (ArcGIS MapServer).

Source:  Bernalillo County Assessor public map service
         https://assessormap.bernco.gov/server/rest/services/GIS/ASROnline_Public_Map/MapServer/22/query
Scope:   county:Bernalillo  (Albuquerque metro — largest county in NM, ~280 k parcels)
Fields confirmed via live fetch 2026-06-14:
  UPC          → parcel_id
  SITUSADD     → address  (property situs street)
  SITUSCITY    → city
  SITUSZIP     → zip_code
  OWNER        → owner_name
  TOTVALUE     → estimated_value  (total assessed value, land + improvements)
  OWNADD       → owner mailing address  (absentee-owner detection)
  OWNCITY      → owner mailing city
  OWNZIPCODE   → owner mailing zip

Note: the endpoint is an ArcGIS MapServer layer query, which is structurally
identical to a FeatureServer layer query — same pagination, same JSON envelope.
ArcGISHarvester handles both transparently.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class NewMexicoBernalilloHarvester(ArcGISHarvester):
    STATE = "NM"
    SOURCE_LABEL = "Bernalillo County Assessor parcels"
    SERVICE_URL = (
        "https://assessormap.bernco.gov/server/rest/services/GIS/"
        "ASROnline_Public_Map/MapServer/22/query"
    )
    OUT_FIELDS = (
        "UPC,PID,OWNER,SITUSADD,SITUSCITY,SITUSZIP,"
        "OWNADD,OWNCITY,OWNZIPCODE,LANDVALUE,IMPTVALUE,TOTVALUE,TAXYR"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("UPC") or row.get("PID") or "").strip()
        addr = str(row.get("SITUSADD") or "").strip()
        if not parcel or not addr:
            return None

        owner = str(row.get("OWNER") or "").strip()
        if not owner:
            return None

        # Absentee detection: compare situs zip to owner mailing zip.
        situs_zip = str(row.get("SITUSZIP") or "").strip()
        own_zip = str(row.get("OWNZIPCODE") or "").strip()
        own_addr = str(row.get("OWNADD") or "").strip()
        absentee = bool(own_zip) and own_zip != situs_zip

        # TOTVALUE = LANDVALUE + IMPTVALUE (both always present in this layer).
        value = to_float(row.get("TOTVALUE")) or (
            to_float(row.get("LANDVALUE")) + to_float(row.get("IMPTVALUE"))
        )

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("SITUSCITY") or "").strip(),
            state=self.STATE,
            zip_code=situs_zip[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=None,  # not exposed in this layer
        )
