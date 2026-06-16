"""Idaho — IDL WhiteStar statewide parcels (ArcGIS FeatureServer).

Source: Idaho Department of Lands GIS Program
Endpoint: https://gis1.idl.idaho.gov/arcgis/rest/services/Portal/WhiteStar_Parcels/FeatureServer/0/query
Coverage: Statewide — all 44 Idaho counties (multi-county composite).
Owner field: owner1 (+ owner2 for co-owners).
Value field: totalvalue (assessed total); landval / improvval available as components.
Absentee detection: property state (propstate) vs. mail state (mailstate) mismatch,
  OR property city/zip vs. mail city/zip when both are ID.
Last-sale date: latsaldate (Unix ms epoch from ArcGIS Date field).
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class IdahoWhiteStarHarvester(ArcGISHarvester):
    STATE = "ID"
    SOURCE_LABEL = "IDL WhiteStar statewide parcels"
    SERVICE_URL = (
        "https://gis1.idl.idaho.gov/arcgis/rest/services/Portal/"
        "WhiteStar_Parcels/FeatureServer/0/query"
    )

    # Fields to pull from the service (keeps payload lean).
    _OUT_FIELDS = ",".join([
        "apn", "propfuladd", "propcity", "propstate", "propzip",
        "owner1", "owner2",
        "mailfuladd", "mailcity", "mailstate", "mailzip",
        "totalvalue", "landval", "improvval",
        "latsaldate",
    ])

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("apn") or "").strip()
        addr   = str(row.get("propfuladd") or "").strip()
        owner  = str(row.get("owner1") or "").strip()

        # Skip records with no parcel ID or no owner name.
        if not parcel or not owner:
            return None

        # Assessed value: prefer totalvalue; fall back to sum of components.
        value = to_float(row.get("totalvalue")) or (
            to_float(row.get("landval")) + to_float(row.get("improvval"))
        )

        # Absentee-owner detection: compare property address vs. mailing address.
        prop_city  = str(row.get("propcity")  or "").strip()
        prop_state = str(row.get("propstate") or "").strip()
        prop_zip   = str(row.get("propzip")   or "").strip()
        mail_addr  = str(row.get("mailfuladd") or "").strip()
        mail_city  = str(row.get("mailcity")   or "").strip()
        mail_state = str(row.get("mailstate")  or "").strip()
        mail_zip   = str(row.get("mailzip")    or "").strip()

        # Out-of-state mail → definitely absentee.
        out_of_state = bool(mail_state) and mail_state.upper() != "ID"
        # Same state but different city/zip → likely absentee.
        different_local = (
            bool(mail_addr)
            and (norm(mail_city) != norm(prop_city) or norm(mail_zip) != norm(prop_zip))
        )
        is_absentee = out_of_state or different_local

        # Last-sale date: ArcGIS returns Unix ms epoch for Date fields.
        last_sale: Optional[str] = None
        raw_date = row.get("latsaldate")
        if raw_date:
            try:
                last_sale = datetime.datetime.utcfromtimestamp(
                    int(raw_date) / 1000
                ).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                last_sale = None

        distress: list[str] = []
        if is_absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=prop_city,
            state=self.STATE,
            zip_code=prop_zip[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=last_sale,
        )
