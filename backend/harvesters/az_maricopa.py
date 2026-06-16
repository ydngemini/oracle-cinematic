"""Arizona — Maricopa County Assessor parcels (ArcGIS MapServer).

Source:  Maricopa County Assessor's Office — MaricopaDynamicQueryService
Layer:   3 (Parcels)
Scope:   county:Maricopa  (~1.4 M active parcels)
Portal:  ArcGIS MapServer (not FeatureServer — same REST query interface)

Key column map
--------------
PropertyRecord field   → source column
parcel_id              → APN_DASH  (formatted "101-01-001C")
address                → PHYSICAL_ADDRESS
city                   → PHYSICAL_CITY
zip_code               → PHYSICAL_ZIP
owner_name             → OWNER_NAME
estimated_value        → FCV_CUR   (Full Cash Value, current year, comma-formatted string)
last_sale_date         → SALE_DATE ("MM/DD/YYYY" string or null)

Absentee-owner logic: mailing state (MAIL_STATE) != "AZ" OR
norm(MAIL_ADDRESS) != norm(PHYSICAL_ADDRESS).
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# Base query URL — MapServer layer 3 on the Maricopa County Assessor GIS server.
# Verified live 2026-06-14; returns features with exceededTransferLimit when
# more than 1 000 records are requested (server default maxRecordCount = 1 000).
_SERVICE = (
    "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/"
    "MaricopaDynamicQueryService/MapServer/3/query"
)

_OUT_FIELDS = ",".join([
    "APN", "APN_DASH",
    "OWNER_NAME",
    "PHYSICAL_ADDRESS", "PHYSICAL_CITY", "PHYSICAL_ZIP",
    "FCV_CUR",
    "SALE_DATE",
    "MAIL_ADDRESS", "MAIL_CITY", "MAIL_STATE",
])


class ArizonaMaricopaHarvester(ArcGISHarvester):
    STATE = "AZ"
    SOURCE_LABEL = "Maricopa County Assessor parcels"
    SERVICE_URL = _SERVICE
    WHERE = "APN IS NOT NULL"
    OUT_FIELDS = _OUT_FIELDS

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("APN_DASH") or row.get("APN") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("OWNER_NAME") or "").strip()
        if not owner:
            return None  # skip parcels with no owner name

        addr = str(row.get("PHYSICAL_ADDRESS") or "").strip()
        city = str(row.get("PHYSICAL_CITY") or "").strip()
        zip_code = str(row.get("PHYSICAL_ZIP") or "").strip()[:10]

        # FCV_CUR is a right-padded comma-formatted string, e.g. "  10,205,861"
        value = to_float(row.get("FCV_CUR"))

        # Absentee-owner: out-of-state OR mailing address differs from physical
        mail_state = str(row.get("MAIL_STATE") or "").strip().upper()
        mail_addr = str(row.get("MAIL_ADDRESS") or "").strip()
        absentee = (mail_state not in ("", "AZ")) or (
            bool(addr) and bool(mail_addr) and norm(mail_addr) != norm(addr)
        )

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        sale_raw = str(row.get("SALE_DATE") or "").strip()
        # SALE_DATE comes back as "MM/DD/YYYY" — normalise to ISO
        last_sale: Optional[str] = None
        if sale_raw:
            parts = sale_raw.split("/")
            if len(parts) == 3:
                last_sale = f"{parts[2]}-{parts[0]}-{parts[1]}"
            else:
                last_sale = sale_raw[:10] or None

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,   # no mortgage/lien data in this dataset
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=last_sale,
        )
