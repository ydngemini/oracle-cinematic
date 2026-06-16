"""Texas — Bexar County Appraisal District parcels (ArcGIS MapServer).

Endpoint:  https://maps.bexar.org/arcgis/rest/services/Parcels/MapServer/0/query
Portal:    ArcGIS MapServer (query interface identical to FeatureServer)
Scope:     county:Bexar  (San Antonio metro, ~700 k parcels)
Auth:      none — fully public, no token required
Vintage:   live BCAD roll (refreshed continuously)

Column map
----------
  PropID   → parcel_id
  Situs    → address   (situs / property street address)
  Owner    → owner_name
  AddrCity → city
  Zip      → zip_code
  TotVal   → estimated_value  (LandVal + ImprVal combined)

AddrLn1 is the owner mailing address line 1; it is compared against the
situs address to detect absentee ownership.  Many records carry "NULL" as a
literal string — those are treated as empty.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# Queryable fields (subset — avoids geometry overhead on large pages).
_FIELDS = "PropID,Situs,Owner,AddrLn1,AddrCity,Zip,LandVal,ImprVal,TotVal"


class TexasBexarHarvester(ArcGISHarvester):
    STATE = "TX"
    SOURCE_LABEL = "Bexar CAD parcels (ArcGIS MapServer)"
    # MapServer/0/query — same REST query surface as FeatureServer/0/query.
    SERVICE_URL = (
        "https://maps.bexar.org/arcgis/rest/services/Parcels/MapServer/0/query"
    )
    WHERE = "TotVal > 0"          # skip zero-value utility/right-of-way slivers
    OUT_FIELDS = _FIELDS

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # --- parcel ID ------------------------------------------------------- #
        parcel = str(int(row["PropID"])) if row.get("PropID") is not None else ""
        if not parcel:
            return None

        # --- situs (property) address ---------------------------------------- #
        situs = str(row.get("Situs") or "").strip()
        if not situs:
            return None

        # --- owner ------------------------------------------------------------ #
        owner = str(row.get("Owner") or "").strip()
        if not owner:
            return None

        # --- mailing address (absentee detection) ----------------------------- #
        mail_ln1 = str(row.get("AddrLn1") or "").strip()
        is_null_mail = not mail_ln1 or mail_ln1.upper() == "NULL"
        absentee = (not is_null_mail) and (norm(mail_ln1) != norm(situs))
        flags: list[str] = []
        if absentee:
            flags.append("absentee_owner")

        # --- value ------------------------------------------------------------ #
        tot = to_float(row.get("TotVal"))
        if tot == 0.0:
            tot = to_float(row.get("LandVal")) + to_float(row.get("ImprVal"))

        return PropertyRecord(
            parcel_id=parcel,
            address=situs,
            city=str(row.get("AddrCity") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("Zip") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=tot,
            equity_percent=0.0,          # not published by BCAD
            is_absentee_owner=absentee,
            distress_flags=flags,
            last_sale_date=None,         # not in this layer
        )
