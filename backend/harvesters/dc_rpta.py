"""District of Columbia — RPTA Owner Polygons (ArcGIS MapServer).

Source: DC GIS / Office of Tax and Revenue RPTA (Real Property Tax Administration)
Portal: https://opendata.dc.gov (Computer Assisted Mass Appraisal / RPTA datasets)
Layer:  Property_and_Land_WebMercator / MapServer / layer 40 — "Owner Polygons"

Coverage : ~137,400 parcels, citywide (DC is a single jurisdiction — no county split)
Scope     : city:Washington DC

Key fields
----------
SSL         — Square, Suffix, Lot — DC's canonical parcel identifier
OWNERNAME   — Owner name (individual, corporate, government)
PREMISEADD  — Full property address string (includes city/state/zip inline)
ADDRESS1    — Owner mailing street (absentee detection)
CITYSTZIP   — Owner mailing city/state/zip
NEWTOTAL    — Proposed total assessed value (land + improvement)
SALEDATE    — Last sale date (epoch-ms integer)
SALEPRICE   — Last recorded sale price
HSTDCODE    — Homestead exemption flag ("1" = owner-occupied)
"""
from __future__ import annotations

import datetime
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# Mailing-address tokens that indicate the owner is outside DC.
_DC_NORM = norm("washington dc")


def _parse_sale_date(epoch_ms) -> Optional[str]:
    """Convert ArcGIS epoch-millisecond integer to ISO-8601 date string."""
    if epoch_ms is None:
        return None
    try:
        ts = int(epoch_ms) / 1000
        return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return None


def _split_zip(citystzip: Optional[str]) -> str:
    """Extract ZIP from 'CITY ST 12345-6789' format."""
    if not citystzip:
        return ""
    parts = str(citystzip).strip().split()
    if parts:
        last = parts[-1]
        # Accept full 5-digit or ZIP+4
        if len(last) >= 5 and (last[:5].isdigit()):
            return last[:5]
    return ""


class DCRPTAHarvester(ArcGISHarvester):
    """DC Office of Tax and Revenue — RPTA Owner Polygons, statewide (~137 k parcels)."""

    STATE = "DC"
    SOURCE_LABEL = "DC RPTA Owner Polygons (opendata.dc.gov)"

    # MapServer layer 40 supports the same /query interface as FeatureServer layers.
    SERVICE_URL = (
        "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
        "Property_and_Land_WebMercator/MapServer/40/query"
    )

    # Only pull parcels with an owner name and a positive assessment.
    WHERE = "OWNERNAME IS NOT NULL AND NEWTOTAL > 0"

    OUT_FIELDS = (
        "SSL,OWNERNAME,PREMISEADD,ADDRESS1,ADDRESS2,CITYSTZIP,"
        "NEWTOTAL,NEWLAND,NEWIMPR,SALEDATE,SALEPRICE,HSTDCODE"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("SSL") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("OWNERNAME") or "").strip()
        if not owner:
            return None

        # Property address — PREMISEADD is the full inline address string.
        premise = str(row.get("PREMISEADD") or "").strip()
        if not premise:
            return None

        # Owner mailing address for absentee detection.
        mail_street = str(row.get("ADDRESS1") or "").strip()
        mail_csz = str(row.get("CITYSTZIP") or "").strip()
        mail_full = norm(f"{mail_street} {mail_csz}")

        # Absentee: mailing address is outside DC (simple norm comparison).
        is_absentee = bool(mail_full) and _DC_NORM not in mail_full

        # Homestead code "1" indicates owner-occupied; corroborates non-absentee.
        homestead = str(row.get("HSTDCODE") or "").strip() == "1"
        if homestead:
            is_absentee = False

        assessed = to_float(row.get("NEWTOTAL"))
        sale_price = to_float(row.get("SALEPRICE"))

        # Equity proxy: sale_price denominator (rough — no mortgage data available).
        equity = 0.0
        if sale_price > 0 and assessed > 0:
            equity = min((assessed / sale_price) * 100.0, 100.0)

        distress: list[str] = []
        if is_absentee:
            distress.append("absentee_owner")
        # Low assessment relative to sale price can signal distress / value gap.
        if sale_price > 0 and assessed < sale_price * 0.6:
            distress.append("below_market_assessment")

        owner_type = classify_owner(owner)

        # Zip should be the property zip, not the owner mailing zip.
        # Prefer parsing from premise address; fall back to CITYSTZIP only when
        # the owner is DC-based (mailing and property are co-located).
        zip_code = _split_zip(premise)
        if not zip_code and not is_absentee:
            zip_code = _split_zip(row.get("CITYSTZIP"))

        return PropertyRecord(
            parcel_id=parcel,
            address=premise,
            city="Washington",
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=owner_type,
            estimated_value=assessed,
            equity_percent=round(equity, 2),
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=_parse_sale_date(row.get("SALEDATE")),
        )
