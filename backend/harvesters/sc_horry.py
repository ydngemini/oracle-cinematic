"""South Carolina — Horry County parcel/assessment data (ArcGIS MapServer).

Source:  Horry County GIS Application — Parcels layer (ID 24)
Portal:  https://www.horrycounty.org/parcelapp/rest/services/HorryCountyGISApp/MapServer
Service: https://www.horrycounty.org/parcelapp/rest/services/HorryCountyGISApp/MapServer/24/query
Records: ~299,848 parcels (as of June 2026)
Scope:   county:Horry  (Myrtle Beach area; no statewide open parcel API found)

Key column map
--------------
  parcel_id      <- TMS          (Tax Map Sheet identifier, e.g. "1810001038")
  address        <- OwnerStreet  (owner mailing street — situs not in this layer)
  city           <- OwnerCity
  zip_code       <- OwnerZipCode (full ZIP+4, e.g. "29526-4306")
  owner_name     <- OwnerName
  estimated_value <- MarketProp  (full market value; AssessedProp is assessment ratio)
  last_sale_date <- SaleDate     (epoch-ms timestamp → ISO date string)

Pagination: resultOffset / resultRecordCount (server max 2 000; supportsPagination=True).
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class SouthCarolinaHorryHarvester(ArcGISHarvester):
    STATE = "SC"
    SOURCE_LABEL = "Horry County GIS parcels"
    SERVICE_URL = (
        "https://www.horrycounty.org/parcelapp/rest/services/"
        "HorryCountyGISApp/MapServer/24/query"
    )
    WHERE = "OwnerName IS NOT NULL AND AssessedProp > 0"
    OUT_FIELDS = (
        "TMS,OwnerName,OwnerStreet,OwnerCity,OwnerState,"
        "OwnerZipCode,AssessedProp,MarketProp,SaleDate,LandUseCode"
    )

    # Land-use codes that signal potential distress / vacancy
    _VACANT_CODES = {"100", "101", "109", "119", "122", "200", "201"}

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("TMS") or "").strip()
        owner = str(row.get("OwnerName") or "").strip()
        if not parcel_id or not owner:
            return None

        street = str(row.get("OwnerStreet") or "").strip()
        city = str(row.get("OwnerCity") or "").strip()
        state_field = str(row.get("OwnerState") or "SC").strip()
        zip_code = str(row.get("OwnerZipCode") or "").strip()[:10]

        market_val = to_float(row.get("MarketProp"))
        assessed_val = to_float(row.get("AssessedProp"))
        # Market value preferred; fall back to assessed if market absent
        estimated_value = market_val if market_val > 0 else assessed_val

        # SC assessment ratio is ~4 % (residential) or ~6 % (commercial)
        # equity_percent is not derivable without mortgage data; set 0.0
        equity_percent = 0.0

        # Absentee: owner state differs from SC, or normalised street differs
        is_absentee = (state_field.upper() != "SC") or (
            bool(street) and norm(street) != norm(city)
        )

        # Sale date: epoch-ms → "YYYY-MM-DD"
        last_sale_date: Optional[str] = None
        raw_sale = row.get("SaleDate")
        if raw_sale is not None:
            try:
                dt = datetime.datetime.utcfromtimestamp(int(raw_sale) / 1000)
                last_sale_date = dt.strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                pass

        # Distress flags
        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        land_use = str(row.get("LandUseCode") or "").strip()
        if land_use in self._VACANT_CODES:
            flags.append("vacant_land")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=street,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=equity_percent,
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=last_sale_date,
        )
