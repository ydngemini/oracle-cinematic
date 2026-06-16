"""California — San Diego County Assessor parcels (ArcGIS MapServer).

Source:  San Diego County GeocoderMerged MapServer, Layer 1
URL:     https://webmaps.sandiego.gov/arcgis/rest/services/GeocoderMerged/MapServer/1/query
Scope:   county:San Diego  (~1 M+ parcels)
License: https://gis-public.sandiegocounty.gov/

Key columns (real names as returned by the service):
  APN            – Assessor's Parcel Number
  OWN_NAME1      – Primary owner name
  OWN_ADDR1      – Owner mailing address line 1
  OWN_ZIP        – Owner mailing ZIP
  SITUS_ADDRESS  – House number (int)
  SITUS_STREET   – Street name
  SITUS_SUFFIX   – Street type (LN, AVE, etc.)
  SITUS_COMMUNITY– City / community name
  SITUS_ZIP      – Situs ZIP (may include +4 suffix)
  ASR_LAND       – Assessed land value
  ASR_IMPR       – Assessed improvement value
  ASR_TOTAL      – Total assessed value (land + impr)
  DOCDATE        – Sale recording date (MMDDYY string)
  DOCNMBR        – Sale document number
  OWNEROCC       – 'Y' if owner-occupied
  OWN_ADDR2      – Owner mailing city/state (used for absentee detection)
"""
from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# DOCDATE is encoded as MMDDYY (e.g. "081713" → 2013-08-17)
_DOCDATE_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})$")


def _parse_docdate(raw: str) -> Optional[str]:
    """Convert MMDDYY → ISO YYYY-MM-DD, or return None on failure."""
    if not raw:
        return None
    m = _DOCDATE_RE.match(str(raw).strip())
    if not m:
        return None
    mm, dd, yy = m.groups()
    # County data spans 1900s–2000s; treat YY < 30 as 20xx, else 19xx
    century = "20" if int(yy) < 30 else "19"
    return f"{century}{yy}-{mm}-{dd}"


class CaliforniaSanDiegoHarvester(ArcGISHarvester):
    """San Diego County Assessor parcels — owner name + assessed value included."""

    STATE = "CA"
    SOURCE_LABEL = "San Diego County GeocoderMerged"
    SERVICE_URL = (
        "https://webmaps.sandiego.gov/arcgis/rest/services/"
        "GeocoderMerged/MapServer/1/query"
    )
    # Fetch only the columns we need to keep payloads lean.
    OUT_FIELDS = (
        "APN,OWN_NAME1,OWN_ADDR1,OWN_ADDR2,OWN_ZIP,"
        "SITUS_ADDRESS,SITUS_STREET,SITUS_SUFFIX,"
        "SITUS_COMMUNITY,SITUS_ZIP,"
        "ASR_LAND,ASR_IMPR,ASR_TOTAL,"
        "DOCDATE,DOCNMBR,OWNEROCC"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("APN") or "").strip()
        if not parcel:
            return None

        # Build situs address string.
        # SITUS_ADDRESS comes back as a numeric int; cast via int() first to
        # avoid ".0" floating-point suffix, then to str.
        raw_no = row.get("SITUS_ADDRESS")
        try:
            house_no = str(int(raw_no)) if raw_no is not None else ""
        except (ValueError, TypeError):
            house_no = str(raw_no or "").strip()
        street   = str(row.get("SITUS_STREET") or "").strip()
        suffix   = str(row.get("SITUS_SUFFIX") or "").strip()
        addr     = " ".join(filter(None, [house_no, street, suffix])).strip()
        if not addr:
            return None

        city    = str(row.get("SITUS_COMMUNITY") or "").strip().title()
        situs_zip = str(row.get("SITUS_ZIP") or "").strip()[:10]
        # SITUS_ZIP may carry +4 (e.g. "92020-8306"); keep as-is for now
        zip_code = situs_zip

        owner   = str(row.get("OWN_NAME1") or "").strip()
        # Absentee: owner mailing city/state doesn't match situs community
        mail_cs = str(row.get("OWN_ADDR2") or "").upper()
        situs_c = city.upper()
        absentee = bool(mail_cs) and situs_c not in mail_cs

        # Use OWNEROCC flag as a stronger absentee signal when available
        if str(row.get("OWNEROCC") or "").upper() == "Y":
            absentee = False

        value = to_float(row.get("ASR_TOTAL")) or (
            to_float(row.get("ASR_LAND")) + to_float(row.get("ASR_IMPR"))
        )

        flags: list[str] = []
        if absentee:
            flags.append("absentee_owner")

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
            is_absentee_owner=absentee,
            distress_flags=flags,
            last_sale_date=_parse_docdate(str(row.get("DOCDATE") or "")),
        )
