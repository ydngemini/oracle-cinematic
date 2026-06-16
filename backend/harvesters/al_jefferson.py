"""Alabama — Jefferson County BOE parcels (ArcGIS MapServer).

Source: Jefferson County GIS / Board of Equalization CAMA data
Portal: https://jccgis.jccal.org/server/rest/services/BOE/JCParcelsWMS/MapServer/0/query
Scope: county:Jefferson  (largest county in Alabama; statewide parcel layer not publicly available)

Key column map
--------------
parcel_id       <- PARCELID
address         <- ADDR_PSPR  (property situs address pre-street suffix parsed)
city            <- Property_City
zip_code        <- ZIP
owner_name      <- OWNERNAME
estimated_value <- AssdValue
mailing_*       <- PROP_MAIL / CITYMAIL / ZIP_MAIL / STATE_Mail  (absentee detection)
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class AlabamaJeffersonHarvester(ArcGISHarvester):
    STATE = "AL"
    SOURCE_LABEL = "Jefferson County BOE parcels (CAMA)"

    # MapServer query endpoint — same ArcGIS REST query protocol as FeatureServer
    SERVICE_URL = (
        "https://jccgis.jccal.org/server/rest/services/BOE/JCParcelsWMS/MapServer/0/query"
    )

    # Only pull records that have an owner name and a non-zero assessed value
    WHERE = "OWNERNAME IS NOT NULL AND AssdValue IS NOT NULL AND AssdValue > 0"

    OUT_FIELDS = (
        "PARCELID,OWNERNAME,ADDR_PSPR,Property_City,ZIP,"
        "PROP_MAIL,CITYMAIL,ZIP_MAIL,STATE_Mail,"
        "AssdValue,PrevParcelTotal,MaintDate"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELID") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("OWNERNAME") or "").strip()
        if not owner:
            return None

        # Situs (property) address
        situs = str(row.get("ADDR_PSPR") or "").strip()
        prop_city = str(row.get("Property_City") or "").strip()
        prop_zip = str(row.get("ZIP") or "").strip()[:10]

        # Mailing address — used for absentee detection
        mail_addr = str(row.get("PROP_MAIL") or "").strip()
        mail_city = str(row.get("CITYMAIL") or "").strip()
        mail_zip = str(row.get("ZIP_MAIL") or "").strip()[:10]
        mail_state = str(row.get("STATE_Mail") or "").strip().upper()

        # Absentee: mailing address differs from situs OR owner is out-of-state
        absentee = False
        if mail_addr and situs:
            absentee = norm(mail_addr) != norm(situs)
        if not absentee and mail_state and mail_state not in ("AL", ""):
            absentee = True

        # Assessed value (Alabama taxes on ~20 % of appraised — store what we have)
        assd = to_float(row.get("AssdValue"))
        # PrevParcelTotal is the prior year total assessed; use as fallback
        if assd == 0.0:
            assd = to_float(row.get("PrevParcelTotal"))

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        last_maint = str(row.get("MaintDate") or "").strip()
        last_sale: Optional[str] = last_maint[:10] if last_maint else None

        return PropertyRecord(
            parcel_id=parcel,
            address=situs or f"PARCEL {parcel}",
            city=prop_city,
            state=self.STATE,
            zip_code=prop_zip,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=assd,
            equity_percent=0.0,   # raw assessment data; ARV/equity computed downstream
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=last_sale,
        )
