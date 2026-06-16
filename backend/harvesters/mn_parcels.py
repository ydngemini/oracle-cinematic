"""Minnesota — MN Geospatial Commons statewide opt-in parcels (ArcGIS FeatureServer).

Source:  https://gisdata.mn.gov/dataset/plan-parcels-open
Service: https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_mngeo/plan_parcels_open/FeatureServer/1/query
Coverage: ~59 opt-in counties; statewide compilation maintained by MnGeo.
Assessed-value field: emv_total (Estimated Market Value, total).
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# Regex that splits "Marshall, MN 56258" style own_add_l2 into city / state / zip.
_CITY_STATE_ZIP = re.compile(
    r"^(?P<city>[^,]+),\s*(?P<st>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)


def _parse_own_add_l2(raw: str) -> tuple[str, str]:
    """Return (city, zip) from 'City, ST ZZZZZ' or empty strings."""
    m = _CITY_STATE_ZIP.match((raw or "").strip())
    if m:
        return m.group("city").strip(), m.group("zip")
    return "", ""


class MinnesotaGeospatialHarvester(ArcGISHarvester):
    STATE = "MN"
    SOURCE_LABEL = "MN Geospatial Commons parcels (plan_parcels_open)"
    SERVICE_URL = (
        "https://enterprise.gisdata.mn.gov/aghost/rest/services/"
        "us_mn_state_mngeo/plan_parcels_open/FeatureServer/1/query"
    )

    # Only pull fields we actually use — keeps response payloads smaller.
    OUT_FIELDS = (
        "county_pin,state_pin,anumber,st_name,st_pos_typ,st_pos_dir,"
        "zip,postcomm,ctu_name,co_name,owner_name,owner_more,"
        "own_add_l1,own_add_l2,emv_total,sale_date,sale_value,"
        "homestead,tax_exempt"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # ------------------------------------------------------------------ #
        # Parcel ID — prefer state_pin (county-prefixed) over county_pin.
        # ------------------------------------------------------------------ #
        parcel = (
            str(row.get("state_pin") or row.get("county_pin") or "").strip()
        )
        if not parcel:
            return None

        # ------------------------------------------------------------------ #
        # Street address — assemble from parsed components.
        # ------------------------------------------------------------------ #
        num = row.get("anumber")
        street = str(row.get("st_name") or "").strip()
        st_type = str(row.get("st_pos_typ") or "").strip()
        st_dir = str(row.get("st_pos_dir") or "").strip()
        parts = [p for p in [str(num) if num else "", street, st_type, st_dir] if p]
        address = " ".join(parts).strip()
        if not address:
            return None

        # ------------------------------------------------------------------ #
        # City / ZIP.
        # ------------------------------------------------------------------ #
        zip_code = str(row.get("zip") or "").strip()[:10]
        # postcomm = postal community name; ctu_name = city/township/unorg.
        city = (
            str(row.get("postcomm") or row.get("ctu_name") or row.get("co_name") or "").strip()
        )
        # Fallback: parse own_add_l2 ("City, MN ZZZZZ") when direct fields empty.
        if not city or not zip_code:
            raw_l2 = str(row.get("own_add_l2") or "")
            c2, z2 = _parse_own_add_l2(raw_l2)
            if not city:
                city = c2
            if not zip_code:
                zip_code = z2

        # ------------------------------------------------------------------ #
        # Owner.
        # ------------------------------------------------------------------ #
        owner = str(row.get("owner_name") or "").strip()
        # Append owner_more (secondary line like "C/O Agent Name") if present.
        more = str(row.get("owner_more") or "").strip()
        if more:
            owner = f"{owner} {more}".strip()

        # ------------------------------------------------------------------ #
        # Absentee-owner flag — compare property address with owner address.
        # ------------------------------------------------------------------ #
        own_addr = str(row.get("own_add_l1") or "").strip()
        is_absentee = bool(own_addr) and norm(own_addr) != norm(address)

        # ------------------------------------------------------------------ #
        # Value — emv_total is Estimated Market Value (all components).
        # ------------------------------------------------------------------ #
        estimated_value = to_float(row.get("emv_total"))

        # ------------------------------------------------------------------ #
        # Distress flags.
        # ------------------------------------------------------------------ #
        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        homestead = str(row.get("homestead") or "").lower()
        if homestead in ("no", "n", "false", "0"):
            flags.append("non_homestead")
        if str(row.get("tax_exempt") or "").strip().lower() in ("yes", "y", "true", "1"):
            flags.append("tax_exempt")

        # ------------------------------------------------------------------ #
        # Sale date — ArcGIS returns epoch-ms integers for Date fields.
        # ------------------------------------------------------------------ #
        sale_raw = row.get("sale_date")
        last_sale_date: Optional[str] = None
        if sale_raw is not None:
            try:
                import datetime
                ts = int(sale_raw) / 1000
                last_sale_date = datetime.date.fromtimestamp(ts).isoformat()
            except Exception:
                last_sale_date = str(sale_raw)[:10] or None

        return PropertyRecord(
            parcel_id=parcel,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=last_sale_date,
        )
