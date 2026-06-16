"""Washington — Snohomish County Assessor parcels (ArcGIS FeatureServer).

Source:  Snohomish County Open Data Portal (GIS Hub)
Service: https://services6.arcgis.com/z6WYi9VRHfgwgtyW/arcgis/rest/services/Parcels/FeatureServer/0
Portal:  https://snohomish-county-open-data-portal-snoco-gis.hub.arcgis.com/datasets/kingcounty::parcels
Scope:   county:Snohomish  (~180 k+ active parcels, refreshed 3x/week, TAX_YEAR 2026)

Field mapping:
  PARCEL_ID   → parcel_id
  SITUSLINE1  → address   (situs / property address line 1)
  SITUSCITY   → city
  SITUSZIP    → zip_code
  OWNERNAME   → owner_name
  MKTTL       → estimated_value  (total market value = MKLND + MKIMP)
  TAXPRNAME   → cross-check for absentee detection
  TAXPRCITY / TAXPRSTATE / TAXPRZIP → mailing address for absentee test
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord


class WASnohomishHarvester(ArcGISHarvester):
    STATE = "WA"
    SOURCE_LABEL = "Snohomish County Assessor parcels"
    SERVICE_URL = (
        "https://services6.arcgis.com/z6WYi9VRHfgwgtyW/arcgis/rest/services"
        "/Parcels/FeatureServer/0/query"
    )
    # Request only the fields we consume — keeps payloads lean.
    OUT_FIELDS = ",".join([
        "PARCEL_ID", "OWNERNAME",
        "SITUSLINE1", "SITUSCITY", "SITUSSTATE", "SITUSZIP",
        "MKLND", "MKIMP", "MKTTL",
        "TAXPRNAME", "TAXPRCITY", "TAXPRSTATE", "TAXPRZIP",
        "USECODE", "TAX_YEAR", "STATUS",
    ])
    # Only active parcels
    WHERE = "STATUS = 'A'"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("PARCEL_ID") or "").strip()
        if not parcel_id:
            return None

        owner_name = str(row.get("OWNERNAME") or "").strip()
        if not owner_name:
            return None  # skip records with no owner — can't score leads

        # Situs (property) address
        address = str(row.get("SITUSLINE1") or "").strip()
        city = str(row.get("SITUSCITY") or "").strip()
        zip_code = str(row.get("SITUSZIP") or "").strip()
        state = str(row.get("SITUSSTATE") or self.STATE).strip() or self.STATE

        # Market / assessed value  (MKTTL = MKLND + MKIMP, pre-summed by the source)
        estimated_value = to_float(row.get("MKTTL") or 0)
        if estimated_value <= 0:
            # Fall back to summing land + improvement if MKTTL is absent/zero
            estimated_value = to_float(row.get("MKLND") or 0) + to_float(row.get("MKIMP") or 0)

        owner_type = classify_owner(owner_name)

        # Absentee owner: mailing city/state/zip differs from situs
        tax_city = str(row.get("TAXPRCITY") or "").strip().upper()
        tax_state = str(row.get("TAXPRSTATE") or "").strip().upper()
        tax_zip = str(row.get("TAXPRZIP") or "").strip()[:5]
        situs_city = city.upper()
        situs_zip = zip_code[:5]
        is_absentee = bool(
            (tax_city and tax_city != situs_city)
            or (tax_zip and situs_zip and tax_zip != situs_zip)
            or (tax_state and tax_state not in ("WA", ""))
        )

        # Distress signals available from assessment roll
        distress_flags: list[str] = []
        if estimated_value > 0 and to_float(row.get("MKIMP") or 0) == 0:
            distress_flags.append("vacant_land")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state=state,
            zip_code=zip_code,
            owner_name=owner_name,
            owner_type=owner_type,
            estimated_value=estimated_value,
            equity_percent=0.0,   # no mortgage data in this source
            is_absentee_owner=is_absentee,
            distress_flags=distress_flags,
            last_sale_date=None,  # sale history in separate PARCEL_SALES3YR layer
        )
