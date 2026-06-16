"""Colorado — statewide public parcels (ArcGIS FeatureServer).

Source:  Colorado Governor's Office of Information Technology / State GIS
Service: Colorado_Public_Parcels FeatureServer layer 0
         ("Colorado_Public_Parcel_Composite")
URL:     https://gis.colorado.gov/public/rest/services/Address_and_Parcel/
             Colorado_Public_Parcels/FeatureServer/0/query
Coverage: 32+ counties aggregated statewide (scope: statewide)
Fields verified via live fetch 2026-06-14.

Column map:
  parcel_id       <- parcel_id      (Parcel Number)
  address         <- situsAdd       (Street Address)
  city            <- sitAddCty      (City Name)
  zip_code        <- sitAddZip      (Zip Code — often null, fall back to ownAddZip)
  owner_name      <- owner          (Primary Surface Owner)
  estimated_value <- apprValTot     (Parcel Total Value / appraised)
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord


class ColoradoParcelsHarvester(ArcGISHarvester):
    STATE = "CO"
    SOURCE_LABEL = "Colorado Public Parcels (OIT FeatureServer)"

    SERVICE_URL = (
        "https://gis.colorado.gov/public/rest/services/Address_and_Parcel/"
        "Colorado_Public_Parcels/FeatureServer/0/query"
    )

    # Request only what we map — cuts payload size.
    OUT_FIELDS = (
        "parcel_id,owner,owner2,situsAdd,sitAddCty,sitAddZip,"
        "apprValTot,asedValTot,saleDate,salePrice,"
        "ownerAdd,ownAddCty,ownAddZip,countyName"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("parcel_id") or "").strip()
        addr = str(row.get("situsAdd") or "").strip()
        if not parcel or not addr:
            return None

        owner = str(row.get("owner") or "").strip()
        if not owner:
            return None

        city = str(row.get("sitAddCty") or "").strip()

        # sitAddZip is frequently null in this dataset; fall back to owner zip.
        zip_code = (
            str(row.get("sitAddZip") or row.get("ownAddZip") or "").strip()
        )
        # Owner zip values like "802331557" are 9-digit ZIP+4 without hyphen.
        if len(zip_code) > 5:
            zip_code = zip_code[:5]

        # Appraised total value is the best market-value proxy in this feed.
        estimated_value = to_float(row.get("apprValTot"))

        # Absentee detection: compare situs (property) address to owner address.
        owner_addr_norm = norm(str(row.get("ownerAdd") or ""))
        situs_norm = norm(addr)
        is_absentee = bool(owner_addr_norm) and owner_addr_norm != situs_norm

        distress: list[str] = []
        if is_absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=0.0,          # not derivable from this feed alone
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=str(row.get("saleDate") or "").strip()[:10] or None,
        )
