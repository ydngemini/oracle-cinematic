"""Wisconsin — SCO Statewide Parcel Map Initiative V11 (ArcGIS FeatureServer).

Source:  Wisconsin State Cartographer's Office (SCO) + DOA Parcel Initiative
Dataset: V1100_WisconsinParcels_2025 — 3.5 M statewide parcel polygons
Scope:   Statewide (all 72 counties)
Schema:  41 tax/parcel attributes; owner name, site address, assessed value,
         estimated fair market value all present in every record.

Endpoint (layer 0):
    https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/
    Wisconsin_Statewide_Parcels/FeatureServer/0/query

Key column map (source → PropertyRecord):
    PARCELID       → parcel_id
    SITEADRESS     → address
    PLACENAME      → city
    ZIPCODE        → zip_code
    OWNERNME1      → owner_name
    CNTASSDVALUE   → estimated_value  (county-assessed total; fallback ESTFMKVALUE)
    PSTLADRESS     → absentee detection (mailing ≠ site)
    PROPCLASS      → distress_flags helper
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class WisconsinSCOHarvester(ArcGISHarvester):
    STATE = "WI"
    SOURCE_LABEL = "WI SCO Statewide Parcels V11"
    SERVICE_URL = (
        "https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/"
        "Wisconsin_Statewide_Parcels/FeatureServer/0/query"
    )

    # Fields to request from the API (keeps payloads smaller than SELECT *)
    OUT_FIELDS = ",".join([
        "PARCELID",
        "OWNERNME1",
        "OWNERNME2",
        "SITEADRESS",
        "PSTLADRESS",
        "PLACENAME",
        "ZIPCODE",
        "CNTASSDVALUE",
        "ESTFMKVALUE",
        "PROPCLASS",
        "CONAME",
    ])

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELID") or "").strip()
        if not parcel:
            return None

        addr = str(row.get("SITEADRESS") or "").strip()
        if not addr:
            return None

        owner = str(row.get("OWNERNME1") or "").strip()
        if not owner:
            owner = str(row.get("OWNERNME2") or "").strip()

        # Mailing address for absentee detection
        mail_addr = str(row.get("PSTLADRESS") or "").strip()
        is_absentee = bool(mail_addr) and norm(mail_addr) != norm(addr)

        # Assessed value; fall back to estimated fair market value
        value = to_float(row.get("CNTASSDVALUE")) or to_float(row.get("ESTFMKVALUE"))

        # Property class "6" = tax-exempt, "7" = forest cropland — flag for distress
        prop_class = str(row.get("PROPCLASS") or "").strip()
        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        if prop_class in ("6",):
            flags.append("tax_exempt")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("PLACENAME") or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("ZIPCODE") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=None,  # V11 schema does not expose a sale date column
        )
