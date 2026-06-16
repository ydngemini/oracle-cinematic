"""Oklahoma — Oklahoma County Assessor Tax Parcels (ArcGIS FeatureServer).

Source:  OK County GIS Hub (ArcGIS Online, org: euhkr1dAJeQBIjV0)
Service: TaxParcelsPublics_view / layer 0 — TaxParcelPublic
URL:     https://services8.arcgis.com/euhkr1dAJeQBIjV0/arcgis/rest/services/
         TaxParcelsPublics_view/FeatureServer/0/query
Scope:   county:Oklahoma  (Oklahoma County — largest county in OK, ~800k pop,
         seat: Oklahoma City)

Key column map
--------------
  parcel_id       <- PARCELNB_1   (formatted: pin field also available)
  address         <- location      (situs / property street address)
  city            <- locationcity
  zip_code        <- zipcode       (mailing zip; situs zip not always separate)
  owner_name      <- name1         (primary owner line)
  estimated_value <- currentmarket (current market value as assessed)
  last_sale_date  <- saledate      (year integer) + RecordedDate (date)

Absentee detection: mailing address (mailingaddress1 + city) vs situs (location).
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

_SERVICE = (
    "https://services8.arcgis.com/euhkr1dAJeQBIjV0/arcgis/rest/services/"
    "TaxParcelsPublics_view/FeatureServer/0/query"
)

_FIELDS = ",".join([
    "PARCELNB_1",
    "pin",
    "name1",
    "name2",
    "mailingaddress1",
    "city",
    "state",
    "zipcode",
    "location",
    "locationcity",
    "currentmarket",
    "currentassessed",
    "saledate",
    "RecordedDate",
    "SalePrice",
])


class OklahomaCountyHarvester(ArcGISHarvester):
    """Oklahoma County (OK) assessor tax parcels — ArcGIS FeatureServer."""

    STATE = "OK"
    SOURCE_LABEL = "Oklahoma County Assessor Tax Parcels"
    SERVICE_URL = _SERVICE
    WHERE = "name1 IS NOT NULL AND currentmarket > 0"
    OUT_FIELDS = _FIELDS

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELNB_1") or row.get("pin") or "").strip()
        situs = str(row.get("location") or "").strip()
        if not parcel or not situs:
            return None

        owner = str(row.get("name1") or "").strip()
        if not owner:
            return None

        # Owner-2 line (spouse / co-owner) — appended for classify_owner accuracy
        owner2 = str(row.get("name2") or "").strip()
        full_owner = f"{owner} {owner2}".strip() if owner2 else owner

        mail_street = str(row.get("mailingaddress1") or "").strip()
        mail_city   = str(row.get("city") or "").strip()
        mail_full   = f"{mail_street} {mail_city}".strip()

        # Absentee: mailing address differs meaningfully from situs
        absentee = bool(mail_full) and norm(mail_full) != norm(situs)

        market_val = to_float(row.get("currentmarket"))
        assessed   = to_float(row.get("currentassessed"))

        # Equity proxy — Oklahoma uses 11 % of market as assessed; no mortgage data
        equity_pct = 0.0

        # Sale date: RecordedDate (ISO date) preferred; fall back to saledate (year int)
        recorded = row.get("RecordedDate")
        sale_yr  = row.get("saledate")
        if recorded:
            last_sale = str(recorded)[:10]
        elif sale_yr:
            last_sale = f"{int(sale_yr)}-01-01"
        else:
            last_sale = None

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel,
            address=situs,
            city=str(row.get("locationcity") or mail_city or "").strip(),
            state=self.STATE,
            zip_code=str(row.get("zipcode") or "").strip()[:10],
            owner_name=full_owner,
            owner_type=classify_owner(full_owner),
            estimated_value=market_val,
            equity_percent=equity_pct,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=last_sale,
        )
