"""Kentucky — Boone County PVA Tax Parcels (ArcGIS MapServer).

Source:  Boone County GIS / Northern Kentucky GIS Consortium
Service: https://secure.boonecountygis.com/server/rest/services/ParcelLayers/MapServer/0/query
Layer 0: Tax Parcels — parcel_id, owner name, site address, assessed value, sale date.
Scope:   county:Boone  (largest readily-available public KY PVA parcel service)

Key column map
--------------
parcel_id       → PIDN
owner_name      → PRCLOWNR1
address         → SITEADD1  (falls back to: SITENUM + STREETNAME + SITEPOST)
city            → CITYNAME
zip_code        → SITEZIP
estimated_value → ASSESSVALU
last_sale_date  → SALEDATE1
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class KentuckyBoonePVAHarvester(ArcGISHarvester):
    STATE = "KY"
    SOURCE_LABEL = "Boone County KY PVA Tax Parcels"
    # MapServer layer 0 — Tax Parcels; supports standard ArcGIS query params
    SERVICE_URL = (
        "https://secure.boonecountygis.com/server/rest/services/"
        "ParcelLayers/MapServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PIDN") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("PRCLOWNR1") or "").strip()
        if not owner:
            return None

        # Site address: prefer pre-assembled SITEADD1; build from parts if absent
        addr = str(row.get("SITEADD1") or "").strip()
        if not addr:
            parts = [
                str(row.get("SITENUM") or "").strip(),
                str(row.get("STREETNAME") or "").strip(),
                str(row.get("SITEPOST") or "").strip(),
            ]
            addr = " ".join(p for p in parts if p)
        if not addr:
            return None

        city = str(row.get("CITYNAME") or row.get("SITEPOST") or "").strip()
        zip_code = str(row.get("SITEZIP") or "").strip()[:10]

        value = to_float(row.get("ASSESSVALU"))

        # Absentee detection: mailing address differs from site address
        mail = str(row.get("MAILADD1") or "").strip()
        mail_city = str(row.get("MAILPOST") or "").strip()
        absentee = bool(mail) and (
            norm(mail) != norm(addr) or norm(mail_city) != norm(city)
        )

        flags: list[str] = []
        if absentee:
            flags.append("absentee_owner")

        # Sale date: stored as ISO string "YYYY-MM-DD HH:MM:SS" — trim to date
        raw_date = str(row.get("SALEDATE1") or "").strip()
        last_sale = raw_date[:10] if raw_date else None

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=flags,
            last_sale_date=last_sale,
        )
