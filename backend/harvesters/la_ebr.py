"""Louisiana — East Baton Rouge Parish Tax Parcel (ArcGIS MapServer).

Source: EBRGIS / EBR Assessor's Office, Department of Information Services
Service: https://maps.brla.gov/gis/rest/services/Cadastral/Tax_Parcel/MapServer/0/query
Coverage: East Baton Rouge Parish (largest parish by population, ~470K residents)
Fields confirmed live 2026-06-14:
  ASSESSMENT_NUM, PRONO, OWNER, OWNER_ADDRESS, OWNER_CITY_STATE_ZIP,
  PHYSICAL_ADDRESS, SUM_ASSESSED_VALUE, SUM_FAIR_MARKET_VALUE, SALE_YEAR, STATUS
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# Regex to split "BATON ROUGE, LA 70802" → city, state, zip
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<st>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)


def _parse_city_state_zip(raw: str) -> tuple[str, str, str]:
    """Parse 'CITY, ST  ZIPCODE' into (city, state, zip). Returns empty strings on failure."""
    if not raw:
        return "", "", ""
    m = _CITY_STATE_ZIP_RE.match(raw.strip())
    if m:
        return m.group("city").strip(), m.group("st").strip(), m.group("zip").strip()
    # Fallback: return raw as city
    return raw.strip(), "", ""


class LouisianaEBRHarvester(ArcGISHarvester):
    """East Baton Rouge Parish parcel + assessment data via ArcGIS MapServer."""

    STATE = "LA"
    SOURCE_LABEL = "EBR Assessor Tax Parcel (EBRGIS)"

    # MapServer/0 supports query just like FeatureServer/0 — confirmed live.
    SERVICE_URL = (
        "https://maps.brla.gov/gis/rest/services/Cadastral/Tax_Parcel/MapServer/0/query"
    )

    # Only pull active parcels that have an owner and a non-zero assessed value.
    WHERE = "OWNER IS NOT NULL AND SUM_ASSESSED_VALUE > 0"

    OUT_FIELDS = (
        "ASSESSMENT_NUM,PRONO,OWNER,OWNER_ADDRESS,OWNER_CITY_STATE_ZIP,"
        "PHYSICAL_ADDRESS,SUM_ASSESSED_VALUE,SUM_FAIR_MARKET_VALUE,SALE_YEAR,STATUS"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("ASSESSMENT_NUM") or row.get("PRONO") or "").strip()
        addr = str(row.get("PHYSICAL_ADDRESS") or "").strip()
        owner = str(row.get("OWNER") or "").strip()

        if not parcel or not addr or not owner:
            return None

        # Assessed value; fall back to fair-market if assessed is 0.
        assessed = to_float(row.get("SUM_ASSESSED_VALUE"))
        fair_market = to_float(row.get("SUM_FAIR_MARKET_VALUE"))
        value = assessed if assessed else fair_market

        # Parse owner city/state/zip from the combined field.
        csz_raw = str(row.get("OWNER_CITY_STATE_ZIP") or "").strip()
        city, _st, zip_code = _parse_city_state_zip(csz_raw)
        if not city:
            city = "Baton Rouge"          # default for EBR parish records
        if not zip_code:
            zip_code = ""

        # Absentee: mailing address differs from physical site address.
        mail_addr = str(row.get("OWNER_ADDRESS") or "").strip()
        is_absentee = bool(mail_addr) and norm(mail_addr) != norm(addr)

        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        status = str(row.get("STATUS") or "").strip().upper()
        if status and status not in ("AC",):
            # Non-active status (e.g. adjudicated, tax sale) signals distress.
            flags.append("non_active_status")

        # Sale year from SALE_YEAR field (string "2020", "0" means unknown).
        sale_year = str(row.get("SALE_YEAR") or "").strip()
        last_sale = f"{sale_year}-01-01" if sale_year and sale_year != "0" else None

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
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=last_sale,
        )
