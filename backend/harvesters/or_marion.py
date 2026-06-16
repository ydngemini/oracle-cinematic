"""Oregon — Marion County TaxParcel_Assessment (ArcGIS FeatureServer).

Dataset:  Marion County Assessor — TaxParcel_Assessment
Scope:    county:Marion  (~115,942 taxlots as of 2026-06)
Portal:   ArcGIS FeatureServer (services1.arcgis.com)
Endpoint: https://services1.arcgis.com/sYGZnQPdJ0azuLyn/arcgis/rest/services/
          TaxParcel_Assessment/FeatureServer/0/query

Key column map  (real source name → PropertyRecord field):
  TAXLOT      → parcel_id
  SITUS       → address       (site/situs address string)
  OWNERCSZ    → city          (parsed: "CITY, ST, ZIP" → city token)
  zip_code    → parsed from OWNERCSZ or TAXLOT prefix
  OWNERNAME   → owner_name
  ASSDVAL     → estimated_value  (assessed value in USD)
  INSTDATE    → last_sale_date   (epoch ms → YYYY-MM-DD)

Notes:
  - SITUS sometimes blank for rural/agricultural lots; records still ingested
    using mailing address components from OWNERADDR/OWNERCSZ.
  - OWNERCSZ format is "CITY, ST, ZIP" — we split on ", " to extract city and
    zip independently.
  - RMVTOTAL (Real Market Value) is also available; we use ASSDVAL for
    estimated_value (the tax-relevant assessed figure) and store nothing for
    RMVTOTAL since PropertyRecord has one value slot.
  - Marion County is the 4th-largest county in Oregon and the state capital
    (Salem) county — representative mid-size sample until a statewide ORMAP
    FeatureServer with owner data becomes publicly available.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# Only pull fields we actually use — keeps response payloads lean.
_OUT_FIELDS = (
    "TAXLOT,TAXACCT,SITUS,OWNERADDR,OWNERCSZ,OWNERNAME,"
    "ASSDVAL,RMVTOTAL,INSTDATE,PROPCLASS"
)

# Marion County sits between lat 44.6–45.1 N, long 122.2–123.3 W;
# all parcels are OR.
_STATE = "OR"


def _parse_ownercsz(ownercsz: str) -> tuple[str, str]:
    """Return (city, zip) extracted from a 'CITY, ST, ZIP' string.

    Examples:
      'SALEM, OR, 97301'   → ('SALEM', '97301')
      'AURORA, OR, 97002'  → ('AURORA', '97002')
      'HUBBARD, OR, 97032' → ('HUBBARD', '97032')
    Falls back to ('', '') on malformed input.
    """
    parts = [p.strip() for p in ownercsz.split(",")]
    city = parts[0] if len(parts) >= 1 else ""
    # zip is last token; strip any trailing chars that aren't digits
    zip_raw = parts[-1] if len(parts) >= 3 else ""
    zip_code = re.sub(r"[^\d]", "", zip_raw)[:10]
    return city, zip_code


def _epoch_to_date(epoch_ms) -> Optional[str]:
    """Convert ArcGIS epoch-milliseconds timestamp to 'YYYY-MM-DD' string."""
    if epoch_ms is None:
        return None
    try:
        import datetime
        ts = int(epoch_ms) / 1000
        return datetime.date.fromtimestamp(ts).isoformat()
    except Exception:
        return None


class OregonMarionHarvester(ArcGISHarvester):
    """Marion County, Oregon parcel/assessment data via ArcGIS FeatureServer."""

    STATE = _STATE
    SOURCE_LABEL = "Marion County OR TaxParcel_Assessment"
    SERVICE_URL = (
        "https://services1.arcgis.com/sYGZnQPdJ0azuLyn/arcgis/rest/services/"
        "TaxParcel_Assessment/FeatureServer/0/query"
    )
    WHERE = "OWNERNAME IS NOT NULL AND ASSDVAL > 0"
    OUT_FIELDS = _OUT_FIELDS

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("TAXLOT") or row.get("TAXACCT") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("OWNERNAME") or "").strip()
        if not owner:
            return None

        # Situs (property site address) — may be blank for rural lots.
        situs = str(row.get("SITUS") or "").strip()
        # Fall back to owner mailing street when situs is absent.
        address = situs or str(row.get("OWNERADDR") or "").strip()

        ownercsz = str(row.get("OWNERCSZ") or "").strip()
        city, zip_code = _parse_ownercsz(ownercsz) if ownercsz else ("", "")

        assessed = to_float(row.get("ASSDVAL"))
        rmv = to_float(row.get("RMVTOTAL"))
        # Prefer assessed value; fall back to RMV if assessed is zero.
        estimated_value = assessed if assessed > 0 else rmv

        owner_type = classify_owner(owner)

        # Absentee check: mailing address differs from situs and is out-of-county.
        mail_street = str(row.get("OWNERADDR") or "").strip()
        mail_state = ""
        if ownercsz:
            parts = [p.strip() for p in ownercsz.split(",")]
            mail_state = parts[1].upper() if len(parts) >= 2 else ""
        is_absentee = bool(situs and mail_street and norm(mail_street) != norm(situs)) or (
            mail_state not in ("", _STATE)
        )

        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        # Low-value assessed trigger (< $5 k likely exempt/ag/zero-value lot)
        if 0 < estimated_value < 5_000:
            flags.append("low_assessed_value")

        return PropertyRecord(
            parcel_id=parcel,
            address=address,
            city=city,
            state=_STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=owner_type,
            estimated_value=estimated_value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=_epoch_to_date(row.get("INSTDATE")),
        )
