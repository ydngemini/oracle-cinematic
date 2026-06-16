"""Georgia — Fulton County Tax Parcels 2024 (ArcGIS FeatureServer).

Source:  Fulton County Board of Assessors via the ARC Open Data & Mapping Hub
Dataset: Tax_Parcels_2024
Service: https://services1.arcgis.com/AQDHTHDrZzfsFsB5/arcgis/rest/services/Tax_Parcels_2024/FeatureServer/0/query
Scope:   county:Fulton  (~300 k parcels; largest county in Georgia by population)
Fields:
  ParcelID    → parcel_id
  Address     → address   (property situs address, street only)
  Owner       → owner_name
  OwnerAddr2  → "CITY ST ZIP" mailing line; parsed for city + zip_code
  TotAppr     → estimated_value  (total appraised / fair-market value)
  TotAssess   → assessed value used to back-compute equity_percent
  (no separate sale date field in this layer)

Note: Georgia law requires assessed value = 40% of fair-market value.
TotAppr is the county's FMV estimate; TotAssess ≈ 0.40 × TotAppr.
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# "ALPHARETTA GA 30004-1483"  or  "ATLANTA GA 30303"
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?)\s+GA\s+(?P<zip>\d{5}(?:-\d{4})?)$",
    re.IGNORECASE,
)


def _parse_owner_addr2(raw: str) -> tuple[str, str]:
    """Return (city, zip) from the 'CITY ST ZIP' OwnerAddr2 field.
    Returns ('', '') when the pattern doesn't match (non-GA mailing address,
    blanks, etc.)."""
    s = (raw or "").strip()
    m = _CITY_STATE_ZIP_RE.match(s)
    if m:
        return m.group("city").strip().title(), m.group("zip").strip()
    # Fallback: try to pull a 5-digit zip from the tail.
    zip_match = re.search(r"\b(\d{5}(?:-\d{4})?)\s*$", s)
    return "", zip_match.group(1) if zip_match else ""


class GeorgiaFultonHarvester(ArcGISHarvester):
    """Fulton County GA — Tax Parcels 2024, ArcGIS FeatureServer."""

    STATE = "GA"
    SOURCE_LABEL = "Fulton County Tax Parcels 2024"
    SERVICE_URL = (
        "https://services1.arcgis.com/AQDHTHDrZzfsFsB5/arcgis/rest/services/"
        "Tax_Parcels_2024/FeatureServer/0/query"
    )
    # Fetch only fields we actually use — keeps pages lighter.
    OUT_FIELDS = (
        "ParcelID,Address,Owner,OwnerAddr1,OwnerAddr2,"
        "TotAppr,TotAssess,LUCode,TaxDist"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("ParcelID") or "").strip()
        addr = str(row.get("Address") or "").strip()
        if not parcel or not addr:
            return None

        owner = str(row.get("Owner") or "").strip()
        if not owner:
            return None

        owner_addr1 = str(row.get("OwnerAddr1") or "").strip()
        owner_addr2 = str(row.get("OwnerAddr2") or "").strip()
        city, zip_code = _parse_owner_addr2(owner_addr2)

        # Absentee: mailing street differs from situs street (case-insensitive,
        # alphanumeric only so punctuation gaps don't cause false positives).
        absentee = bool(owner_addr1) and norm(owner_addr1) != norm(addr)

        # Appraised (FMV) value — primary valuation signal.
        appr = to_float(row.get("TotAppr"))
        assess = to_float(row.get("TotAssess"))
        # Equity proxy: Georgia does not expose mortgage data, so we default
        # to 0.0 — the downstream underwriter fills this from HMDA/lien data.
        equity = 0.0

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")
        # LUCode 300–399 = vacant land; can be a distress signal in urban areas.
        lu = str(row.get("LUCode") or "")
        if lu.startswith("3"):
            distress.append("vacant_land")

        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=appr if appr > 0.0 else assess / 0.4,
            equity_percent=equity,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=None,  # not present in this layer
        )
