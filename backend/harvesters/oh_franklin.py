"""Ohio — Franklin County Auditor CAMA parcels (ArcGIS FeatureServer).

Scope: county:Franklin (Columbus metro — largest OH county, ~550k parcels).
Source: Franklin County Auditor open-data ArcGIS FeatureServer, Tax Parcel layer.
  https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures/Parcel_Features/FeatureServer/0/query

Column map (source → PropertyRecord):
  PARCELID          → parcel_id
  SITEADDRESS       → address        (property site address)
  CVTTXDSCRP        → city           (tax district / municipality description)
  ZIPCD             → zip_code
  OWNERNME1         → owner_name
  TOTVALUEBASE      → estimated_value (base total assessed value)
  SALEDATE          → last_sale_date  (epoch-ms, converted to YYYY-MM-DD)
  PSTLADDRES        → mailing street  (absentee detection vs SITEADDRESS)
  OWNEROCCUPIED     → is_absentee_owner fallback ("N" → True, "Y" → False)

Distress flags raised:
  - "absentee_owner"  when OWNEROCCUPIED == "N" or mailing addr differs from site addr
  - "low_value"       when TOTVALUEBASE > 0 and < 30_000 (land-only / vacant lots)
  - "corporate_owner" when classify_owner() returns "corporate"
  - "trust_owner"     when classify_owner() returns "trust"
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord


class OhioFranklinHarvester(ArcGISHarvester):
    STATE = "OH"
    SOURCE_LABEL = "Franklin County Auditor CAMA (ArcGIS)"

    # ArcGIS FeatureServer — Tax Parcel layer (layer 0)
    SERVICE_URL = (
        "https://gis.franklincountyohio.gov/hosting/rest/services/"
        "ParcelFeatures/Parcel_Features/FeatureServer/0/query"
    )

    # Only pull records that carry an owner name (skip road ROW / utility slivers)
    WHERE = "OWNERNME1 IS NOT NULL AND PARCELID IS NOT NULL"

    OUT_FIELDS = ",".join([
        "PARCELID",
        "OWNERNME1",
        "SITEADDRESS",
        "ZIPCD",
        "CVTTXDSCRP",       # municipality / tax district → city proxy
        "TOTVALUEBASE",
        "LNDVALUEBASE",
        "BLDVALUEBASE",
        "SALEDATE",
        "PSTLADDRES",       # mailing street (absentee detection)
        "PSTLCITYSTZIP",    # mailing city+state+zip
        "OWNEROCCUPIED",    # "Y" / "N"
    ])

    # ---------------------------------------------------------------------- #
    # Record mapping                                                          #
    # ---------------------------------------------------------------------- #
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("PARCELID") or "").strip()
        if not parcel_id:
            return None

        owner = str(row.get("OWNERNME1") or "").strip()
        if not owner:
            return None

        address = str(row.get("SITEADDRESS") or "").strip()

        # Municipality name lives in CVTTXDSCRP (e.g. "COLUMBUS CITY-COLUMBUS CSD").
        # Trim to the first token before the dash for a readable city name.
        raw_city = str(row.get("CVTTXDSCRP") or "").strip()
        city = raw_city.split("-")[0].strip().title() if raw_city else "Franklin County"

        zip_code = str(row.get("ZIPCD") or "").strip()[:10]

        # Assessed value — use TOTVALUEBASE; fall back to land+building sum.
        total_val = to_float(row.get("TOTVALUEBASE"))
        if total_val == 0.0:
            total_val = to_float(row.get("LNDVALUEBASE")) + to_float(row.get("BLDVALUEBASE"))

        # Last sale date — ArcGIS epoch-ms integer.
        last_sale: Optional[str] = None
        raw_sale = row.get("SALEDATE")
        if raw_sale is not None:
            try:
                epoch_s = int(raw_sale) / 1000
                last_sale = datetime.datetime.utcfromtimestamp(epoch_s).strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                pass

        # Absentee detection: prefer the explicit OWNEROCCUPIED flag;
        # fall back to comparing normalised mailing vs site address strings.
        owner_occ = str(row.get("OWNEROCCUPIED") or "").strip().upper()
        if owner_occ == "Y":
            is_absentee = False
        elif owner_occ == "N":
            is_absentee = True
        else:
            mail_addr = str(row.get("PSTLADDRES") or "").strip()
            is_absentee = bool(mail_addr) and norm(mail_addr) != norm(address)

        owner_type = classify_owner(owner)

        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        if 0 < total_val < 30_000:
            flags.append("low_value")
        if owner_type == "corporate":
            flags.append("corporate_owner")
        elif owner_type == "trust":
            flags.append("trust_owner")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=owner_type,
            estimated_value=total_val,
            equity_percent=0.0,     # not available in CAMA; computed downstream
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=last_sale,
        )
