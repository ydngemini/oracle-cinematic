"""Rhode Island — City of Providence Parcels w/ CAMA (ArcGIS FeatureServer).

Source:  Providence GIS Hub (PVDGIS_Admin)
Service: https://services6.arcgis.com/wv9mHoqblhTsnqdG/arcgis/rest/services/
             Parcels_wCAMA/FeatureServer/0/query
Scope:   city:Providence  (~44,128 parcels as of June 2026)

Key column mapping
  parcel_id        <- PROPID          (e.g. "059-0291-0000")
  address          <- ParcAddress     (e.g. "49 Homer St")
  city             <- MuniName        (always "Providence")
  zip_code         <- ZIP_POSTAL
  owner_name       <- Owner1
  estimated_value  <- AssessedValueTotal  (string dollars, no $)
  last_sale_date   <- SaleDate        (epoch-ms → YYYY-MM-DD)

Absentee-owner detection: mailing address (OwnerAddress + OwnerCity + OwnerState)
is normalised and compared against the parcel address + "providence ri"; mismatch
flags the record as absentee.
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class RhodeIslandProvidenceHarvester(ArcGISHarvester):
    STATE = "RI"
    SOURCE_LABEL = "Providence CAMA parcels (PVDGIS)"
    SERVICE_URL = (
        "https://services6.arcgis.com/wv9mHoqblhTsnqdG/arcgis/rest/services/"
        "Parcels_wCAMA/FeatureServer/0/query"
    )

    # Only pull fields we actually use — keeps pages smaller.
    OUT_FIELDS = (
        "PROPID,CAMA_ID,ParcAddress,ZIP_POSTAL,MuniName,"
        "Owner1,OwnerAddress,OwnerCity,OwnerState,Owner_Zip,"
        "AssessedValueTotal,AssessedValueLand,AssessedValueBuildings,"
        "SaleDate,SalePrice,StateUse,MuniUseCode,MuniUseCodeDesc"
    )

    # ------------------------------------------------------------------ #
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PROPID") or row.get("CAMA_ID") or "").strip()
        addr   = str(row.get("ParcAddress") or "").strip()
        if not parcel or not addr:
            return None

        owner  = str(row.get("Owner1") or "").strip()
        city   = str(row.get("MuniName") or "Providence").strip()
        zip_   = str(row.get("ZIP_POSTAL") or "").strip()[:10]

        # Assessed value is stored as a string (e.g. "226900").
        value  = to_float(row.get("AssessedValueTotal")) or (
            to_float(row.get("AssessedValueLand")) +
            to_float(row.get("AssessedValueBuildings"))
        )

        # Absentee-owner: mailing address diverges from parcel address.
        mail_street = str(row.get("OwnerAddress") or "").strip()
        mail_city   = str(row.get("OwnerCity")    or "").strip()
        mail_state  = str(row.get("OwnerState")   or "").strip()
        mail_key    = norm(f"{mail_street}{mail_city}{mail_state}")
        prop_key    = norm(f"{addr}{city}ri")
        absentee    = bool(mail_key) and mail_key != prop_key

        # SaleDate comes in as epoch-milliseconds (int) or None.
        sale_raw  = row.get("SaleDate")
        sale_date: Optional[str] = None
        if sale_raw:
            try:
                sale_date = datetime.datetime.utcfromtimestamp(
                    int(sale_raw) / 1000
                ).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                sale_date = None

        # Distress flags from use-code / absentee signal.
        flags: list[str] = []
        if absentee:
            flags.append("absentee_owner")
        use_desc = str(row.get("MuniUseCodeDesc") or "").lower()
        if "vacant" in use_desc:
            flags.append("vacant_land")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,        # no mortgage balance in source
            is_absentee_owner=absentee,
            distress_flags=flags,
            last_sale_date=sale_date,
        )
