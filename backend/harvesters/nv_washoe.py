"""Nevada — Washoe County Assessor parcels (ArcGIS FeatureServer).

Scope: county:Washoe  (Reno / Sparks metro, ~230 k parcels)
Source: Washoe County Open Data — updated daily from the Assessor's Office.

Endpoint:
  https://wcgisweb.washoecounty.us/arcgis/rest/services/OpenData/OpenData/FeatureServer/0/query

Key column map
  parcel_id       <- PIN          (e.g. "001-020-01")
  address         <- FullAddress  (pre-concatenated situs address)
  city            <- CITY
  zip_code        <- SITUSZIP
  owner_name      <- FIRSTNAME + ' ' + LASTNAME
  estimated_value <- TOTALAPR     (total appraised value; TOTALASS is assessed)
  last_sale_date  <- SALEDATE     (MM/DD/YYYY string)

Absentee detection: mailing address fields (MAILING1/MAILCITY/MAILZIP) are
compared against the situs address.  A mismatch flags the owner as absentee.

Note: Washoe County is Nevada's second-largest county (Reno/Sparks metro, ~500 k
population).  A Nevada-state-wide parcel layer exists on the NDOT ArcGIS server
(gis.dot.nv.gov/.../Statewide_Parcels/MapServer/0) but it carries no valuation
or detailed owner fields — only APN, OwnerName, and situs address.  Washoe is
used here because it is the deepest publicly queryable dataset with owner name +
appraised value + sale date, all required by PropertyRecord.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class NevadaWashoeHarvester(ArcGISHarvester):
    STATE = "NV"
    SOURCE_LABEL = "Washoe County Assessor parcels"
    SERVICE_URL = (
        "https://wcgisweb.washoecounty.us/arcgis/rest/services/"
        "OpenData/OpenData/FeatureServer/0/query"
    )
    # Request only the columns we use — reduces payload size considerably.
    OUT_FIELDS = (
        "PIN,STREETNUM,STREETDIR,STREET,CITY,SITUSZIP,"
        "FIRSTNAME,LASTNAME,"
        "MAILING1,MAILCITY,MAILZIP,"
        "TOTALASS,TOTALAPR,LANDASS,BUILDASS,"
        "SALEDATE,LAND_USE,FullAddress"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        pin = str(row.get("PIN") or "").strip()
        if not pin:
            return None

        # --- owner ---------------------------------------------------------- #
        first = str(row.get("FIRSTNAME") or "").strip()
        last = str(row.get("LASTNAME") or "").strip()
        owner = f"{first} {last}".strip()
        if not owner:
            return None

        # --- situs address -------------------------------------------------- #
        full_addr = str(row.get("FullAddress") or "").strip()
        if not full_addr:
            # fall back to component assembly
            num = str(row.get("STREETNUM") or "").strip()
            direction = str(row.get("STREETDIR") or "").strip()
            street = str(row.get("STREET") or "").strip()
            parts = [p for p in (num, direction, street) if p]
            full_addr = " ".join(parts)

        city = str(row.get("CITY") or "").strip()
        zip_code = str(row.get("SITUSZIP") or "").strip()[:10]

        # --- valuation ------------------------------------------------------ #
        # TOTALAPR = full appraised (market) value; TOTALASS = assessed (taxable)
        estimated_value = to_float(row.get("TOTALAPR")) or to_float(row.get("TOTALASS"))

        # --- absentee detection --------------------------------------------- #
        mail_city = str(row.get("MAILCITY") or "").strip()
        mail_zip = str(row.get("MAILZIP") or "").strip()
        is_absentee = False
        if mail_city and city:
            is_absentee = norm(mail_city) != norm(city)
        elif mail_zip and zip_code:
            is_absentee = norm(mail_zip) != norm(zip_code)

        distress: list[str] = []
        if is_absentee:
            distress.append("absentee_owner")

        # --- sale date ------------------------------------------------------ #
        raw_date = str(row.get("SALEDATE") or "").strip()
        # Source format is MM/DD/YYYY → normalise to YYYY-MM-DD for consistency
        last_sale: Optional[str] = None
        if raw_date and "/" in raw_date:
            parts = raw_date.split("/")
            if len(parts) == 3:
                mm, dd, yyyy = parts
                last_sale = f"{yyyy}-{mm.zfill(2)}-{dd.zfill(2)}"

        return PropertyRecord(
            parcel_id=pin,
            address=full_addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=0.0,  # mortgage balance not available in public data
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=last_sale,
        )
