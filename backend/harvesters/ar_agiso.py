"""Arkansas — AGISO statewide parcel/assessment dataset (ArcGIS FeatureServer).

Source:  Arkansas Geographic Information Systems Office (AGISO)
         Planning_Cadastre / PARCEL_POLYGON_CAMP — Layer 6
         https://gis.arkansas.gov/arcgis/rest/services/FEATURESERVICES/Planning_Cadastre/FeatureServer/6/query

Coverage: statewide (all 75 AR counties); CAMA data refreshed semi-annually.
Owner field populated for the vast majority of parcels.

Column map
----------
  parcel_id       <- parcelid
  address         <- adrlabel  (pre-built label combining adrnum + predir + pstrnam + pstrtype)
  city            <- adrcity
  zip_code        <- adrzip5
  owner_name      <- ownername
  estimated_value <- totalvalue  (assessor market value; use assessvalue as fallback)
"""

from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord


class ArkansasAGISOHarvester(ArcGISHarvester):
    STATE = "AR"
    SOURCE_LABEL = "AR AGISO PARCEL_POLYGON_CAMP"
    SERVICE_URL = (
        "https://gis.arkansas.gov/arcgis/rest/services/FEATURESERVICES/"
        "Planning_Cadastre/FeatureServer/6/query"
    )
    # Require owner name and at least a nominal assessed value so we have
    # something useful to underwrite.
    WHERE = "ownername IS NOT NULL AND totalvalue > 0"
    OUT_FIELDS = (
        "parcelid,ownername,adrlabel,adrnum,predir,pstrnam,pstrtype,"
        "adrcity,adrzip5,assessvalue,totalvalue,county"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("parcelid") or "").strip()
        owner = str(row.get("ownername") or "").strip()
        if not parcel or not owner:
            return None

        # adrlabel is the pre-built concatenation ("2637  CR 213 "); strip extra spaces.
        addr = " ".join(str(row.get("adrlabel") or "").split())
        if not addr:
            # Fallback: build from components
            parts = [
                str(row.get("adrnum") or ""),
                str(row.get("predir") or ""),
                str(row.get("pstrnam") or ""),
                str(row.get("pstrtype") or ""),
            ]
            addr = " ".join(p for p in parts if p).strip()

        city = str(row.get("adrcity") or "").strip()
        zip_code = str(int(row["adrzip5"])) if row.get("adrzip5") else ""

        # totalvalue is assessor market value; assessvalue is the assessed
        # portion (typically 20 % of market for residential in AR).
        total_val = to_float(row.get("totalvalue"))
        assess_val = to_float(row.get("assessvalue"))
        estimated_value = total_val if total_val > 0 else assess_val

        # Rough equity proxy: no mortgage data in this layer, so leave at 0.
        equity_percent = 0.0

        distress: list[str] = []
        owner_type = classify_owner(owner)

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=owner_type,
            estimated_value=estimated_value,
            equity_percent=equity_percent,
            is_absentee_owner=False,   # No mailing-address field in this layer
            distress_flags=distress,
            last_sale_date=None,       # No sale-date column in this layer
        )
