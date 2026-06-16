"""Missouri — Jackson County (Independence/Kansas City) tax parcels (ArcGIS FeatureServer).

Source:  City of Independence, Missouri — COI_Parcels_2_view
         Jackson County assessment data, updated weekly.
Scope:   county:Jackson  (largest MO county by parcel count in open ArcGIS)
Service: https://services.arcgis.com/sbDzK061dd6DNPHv/arcgis/rest/services/COI_Parcels_2_view/FeatureServer/0/query

Key columns verified via live fetch 2026-06-14:
  parcel_id          → parcel_id
  Name               → formatted parcel APN (hyphenated)
  owner              → owner name (full name, individual or entity)
  owneraddress       → owner mailing street
  ownercity          → owner mailing city
  ownerstate         → owner mailing state
  ownerzipcode       → owner mailing zip
  SitusAddress       → property site street address
  SitusCity          → property site city
  SitusZipCode       → property site zip
  AssessedValue      → assessed value (string, cast to float)
  Market_Value_Total → market / estimated value (numeric)
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class MissouriJacksonHarvester(ArcGISHarvester):
    STATE = "MO"
    SOURCE_LABEL = "Jackson County MO parcels (COI_Parcels_2_view)"
    SERVICE_URL = (
        "https://services.arcgis.com/sbDzK061dd6DNPHv/arcgis/rest/services/"
        "COI_Parcels_2_view/FeatureServer/0/query"
    )
    OUT_FIELDS = (
        "parcel_id,Name,owner,owneraddress,ownercity,ownerstate,ownerzipcode,"
        "SitusAddress,SitusCity,SitusZipCode,AssessedValue,Market_Value_Total"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("parcel_id") or row.get("Name") or "").strip()
        site_addr = str(row.get("SitusAddress") or "").strip()
        if not parcel or not site_addr:
            return None

        owner = str(row.get("owner") or "").strip()
        if not owner:
            return None

        owner_addr = str(row.get("owneraddress") or "").strip()
        owner_city = str(row.get("ownercity") or "").strip()
        owner_zip = str(row.get("ownerzipcode") or "").strip()[:10]

        site_city = str(row.get("SitusCity") or "").strip()
        site_zip = str(row.get("SitusZipCode") or "").strip()[:10]

        # Absentee: mailing address differs from site address (normalized comparison).
        absentee = bool(owner_addr) and norm(owner_addr) != norm(site_addr)

        # Prefer Market_Value_Total; fall back to AssessedValue.
        market_val = to_float(row.get("Market_Value_Total"))
        assessed_val = to_float(row.get("AssessedValue"))
        estimated_value = market_val if market_val > 0 else assessed_val

        # Missouri assessed value is typically ~19% of market for residential.
        # Use ratio only when both are available and sensible.
        equity_pct = 0.0
        if market_val > 0 and assessed_val > 0 and assessed_val < market_val:
            equity_pct = round((1.0 - assessed_val / market_val) * 100, 2)

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel,
            address=site_addr,
            city=site_city,
            state=self.STATE,
            zip_code=site_zip,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=equity_pct,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=None,  # not present in COI_Parcels_2_view
        )
