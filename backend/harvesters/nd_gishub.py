"""North Dakota — NDGISHUB Parcels TaxRoll (ArcGIS FeatureServer, table layer 1).

Statewide dataset maintained by the ND State Parcel Program.
Source:  https://gishubdata-ndgov.hub.arcgis.com/datasets/NDGOV::parcels/about
Service: https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUB_Parcels/FeatureServer/1/query

The FeatureServer has two layers:
  0 — Parcels          (geometry + basic GIS fields; no owner / value data)
  1 — Parcels_TaxRoll  (tabular; owner name, mailing address, per-category values)

We target layer 1.  TotalValue already aggregates all land + structure categories,
so we use it directly as estimated_value.

Absentee flag: OwnerMailingAddress city differs from PropertyCity, or owner state
is not ND (out-of-state owner).
"""

from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class NorthDakotaGISHubHarvester(ArcGISHarvester):
    STATE = "ND"
    SOURCE_LABEL = "NDGISHUB Parcels TaxRoll (statewide)"
    SERVICE_URL = (
        "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/"
        "NDGISHUB_Parcels/FeatureServer/1/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("UniqueParcelID") or row.get("ParcelID") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("OwnerName") or "").strip()
        if not owner:
            return None

        # Property situs address (not always populated in every county)
        prop_addr = str(row.get("PropertyAddress") or "").strip()
        prop_city = str(row.get("PropertyCity") or row.get("CountyName") or "").strip()
        prop_zip = str(row.get("PropertyZip") or "").strip()

        # Mailing address (always present when owner is populated)
        mail_addr = str(row.get("OwnerMailingAddress") or "").strip()
        mail_city = str(row.get("OwnerCity") or "").strip()
        mail_state = str(row.get("OwnerState") or "").strip().upper()
        mail_zip = str(row.get("OwnerZip") or "").strip()

        # Use mailing address as fallback when situs address is absent
        address = prop_addr or mail_addr
        city = prop_city or mail_city
        zip_code = (prop_zip or mail_zip)[:10]

        # Absentee: owner mails to a different city/state than the property
        if mail_state and mail_state != "ND":
            absentee = True
        elif prop_city and mail_city and norm(prop_city) != norm(mail_city):
            absentee = True
        else:
            absentee = False

        # TotalValue sums all land + structure assessment categories
        value = to_float(row.get("TotalValue"))
        if value == 0.0:
            # Fall back to summing the sub-categories
            value = (
                to_float(row.get("AgriculturalLandValue"))
                + to_float(row.get("ResidentialLandValue"))
                + to_float(row.get("ResidentialStructureValue"))
                + to_float(row.get("CommercialLandValue"))
                + to_float(row.get("CommercialStructureValue"))
            )

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=None,  # TaxRoll table does not carry a deed/sale date
        )
