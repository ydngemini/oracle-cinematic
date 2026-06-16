"""Iowa — Linn County Real Estate Parcel (ArcGIS FeatureServer).

Scope: county:Linn  (Cedar Rapids metro, ~120 k parcels)
Source: Linn County GIS Open Data Portal
Service: https://services.arcgis.com/i14SLLmXo7Hn9vNc/arcgis/rest/services/RealEstateParcel/FeatureServer/0/query

Column map
----------
parcel_id       <- GPN
address         <- SitusAddress
city            <- SitusCity
zip_code        <- SitusZip
owner_name      <- OwnerDeed
owner_type      <- classify_owner(OwnerDeed)
estimated_value <- ValueAssessedTotal
is_absentee_owner <- OwnerCity/OwnerState/OwnerZip != SitusCity/SitusZip
last_sale_date  <- RecorderDate (epoch ms -> ISO date)
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord


class IowaLinnHarvester(ArcGISHarvester):
    STATE = "IA"
    SOURCE_LABEL = "Linn County IA Real Estate Parcel"
    SERVICE_URL = (
        "https://services.arcgis.com/i14SLLmXo7Hn9vNc/arcgis/rest/services/"
        "RealEstateParcel/FeatureServer/0/query"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("GPN") or "").strip()
        if not parcel:
            return None

        addr = str(row.get("SitusAddress") or "").strip()
        city = str(row.get("SitusCity") or "").strip()
        zip_code = str(row.get("SitusZip") or "").strip()[:10]

        owner = str(row.get("OwnerDeed") or "").strip()
        if not owner:
            return None

        owner_city = str(row.get("OwnerCity") or "").strip()
        owner_state = str(row.get("OwnerState") or "").strip()
        owner_zip = str(row.get("OwnerZip") or "").strip()

        # Absentee: mailing address differs from situs city/zip
        absentee = bool(owner_city) and (
            norm(owner_city) != norm(city)
            or norm(owner_zip[:5]) != norm(zip_code[:5])
            or (owner_state and owner_state.upper() != self.STATE)
        )

        value = to_float(row.get("ValueAssessedTotal"))

        # RecorderDate is Unix epoch milliseconds
        sale_date: Optional[str] = None
        rd = row.get("RecorderDate")
        if rd is not None:
            try:
                sale_date = datetime.datetime.utcfromtimestamp(
                    int(rd) / 1000
                ).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                sale_date = None

        flags = []
        if absentee:
            flags.append("absentee_owner")

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
            last_sale_date=sale_date,
        )
