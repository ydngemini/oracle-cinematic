"""Indiana — Marion County (Indianapolis) parcels w/ Owner & Assessed Values.

Source:  City of Indianapolis / IndyGIS — MapIndyProperty MapServer layer 10
Portal:  https://data.indy.gov (ArcGIS Open Data, served via gis.indy.gov)
Scope:   county:Marion  (~320 k parcels, updated nightly from Marion County
         Assessor's Office)
Fields of interest:
  STATEPARCELNUMBER   → parcel_id   (state-format: 49-XX-XX-XXX-XXX.XXX-XXX)
  STNUMBER + PRE_DIR + FULL_STNAME → address (reconstructed)
  CITY                → city
  ZIPCODE             → zip_code
  FULLOWNERNAME       → owner_name
  OWNERADDRESS / OWNERCITY / OWNERZIP → mailing address (absentee detection)
  ASSESSORYEAR_TOTALAV → estimated_value  (total assessed value, land + imp.)
  ASSESSORYEAR_LANDTOTAL / ASSESSORYEAR_IMPTOTAL → land / improvement split
  PROPERTY_CLASS      → distress flag context (EXEMPT, VACANT LAND, etc.)
  MODDATE             → last_sale_date proxy (assessor modification date)

Note: this is a MapServer /query endpoint (not FeatureServer) but the ArcGIS
REST response format is identical — features[].attributes — so ArcGISHarvester
handles it transparently.  The SERVICE_URL includes /query so no suffix is
appended by fetch_raw.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# IndyGIS MapServer — public, no auth required.
_BASE = (
    "https://gis.indy.gov/server/rest/services/MapIndy/MapIndyProperty/MapServer/10"
)

_OUT_FIELDS = ",".join([
    "STATEPARCELNUMBER",
    "PARCEL_C",
    "STNUMBER",
    "PRE_DIR",
    "FULL_STNAME",
    "CITY",
    "ZIPCODE",
    "FULLOWNERNAME",
    "ASSESSORYEAR_TOTALAV",
    "ASSESSORYEAR_LANDTOTAL",
    "ASSESSORYEAR_IMPTOTAL",
    "OWNERADDRESS",
    "OWNERCITY",
    "OWNERSTATE",
    "OWNERZIP",
    "PROPERTY_CLASS",
    "MODDATE",
])


class IndianaIndyHarvester(ArcGISHarvester):
    """Marion County, IN — Indianapolis parcel + assessment data."""

    STATE = "IN"
    SOURCE_LABEL = "IndyGIS MapIndyProperty parcels (Marion County)"
    SERVICE_URL = f"{_BASE}/query"
    WHERE = "1=1"
    OUT_FIELDS = _OUT_FIELDS

    # ------------------------------------------------------------------
    # Property classes that signal potential distress / non-standard use
    # ------------------------------------------------------------------
    _DISTRESS_CLASSES = frozenset({
        "VACANT LAND",
        "VACANT RESIDENTIAL",
        "VACANT COMMERCIAL",
        "VACANT INDUSTRIAL",
    })

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # Parcel ID — prefer state-format; fall back to local CAMA ID.
        parcel = str(row.get("STATEPARCELNUMBER") or row.get("PARCEL_C") or "").strip()
        if not parcel:
            return None

        # Reconstruct property address from components.
        stnumber = str(row.get("STNUMBER") or "").strip()
        pre_dir  = str(row.get("PRE_DIR") or "").strip()
        stname   = str(row.get("FULL_STNAME") or "").strip()
        if not stname:
            return None
        addr_parts = [p for p in [stnumber, pre_dir, stname] if p]
        address = " ".join(addr_parts)

        city     = str(row.get("CITY") or "INDIANAPOLIS").strip()
        zip_code = str(row.get("ZIPCODE") or "").strip()[:10]

        owner = str(row.get("FULLOWNERNAME") or "").strip().rstrip(", ")
        if not owner:
            return None

        # Assessed value — total is land + improvement.
        total_av = to_float(row.get("ASSESSORYEAR_TOTALAV"))
        if total_av == 0.0:
            land  = to_float(row.get("ASSESSORYEAR_LANDTOTAL"))
            impr  = to_float(row.get("ASSESSORYEAR_IMPTOTAL"))
            total_av = land + impr

        # Absentee detection: mailing address differs from property address.
        mail_addr = str(row.get("OWNERADDRESS") or "").strip()
        mail_city = str(row.get("OWNERCITY") or "").strip()
        mail_zip  = str(row.get("OWNERZIP") or "").strip()[:5]
        prop_zip  = zip_code[:5]

        absentee = False
        if mail_addr and mail_city:
            addr_norm = norm(address + city + prop_zip)
            mail_norm = norm(mail_addr + mail_city + mail_zip)
            absentee  = addr_norm != mail_norm

        # Distress flags.
        flags: list[str] = []
        if absentee:
            flags.append("absentee_owner")
        prop_class = str(row.get("PROPERTY_CLASS") or "").strip().upper()
        if prop_class in self._DISTRESS_CLASSES:
            flags.append("vacant_land")
        if prop_class == "EXEMPT":
            flags.append("exempt_owner")

        # Last-modified date from assessor (MODDATE is milliseconds epoch or None).
        raw_date = row.get("MODDATE")
        last_sale: Optional[str] = None
        if raw_date:
            try:
                import datetime
                ts = int(raw_date) / 1000
                last_sale = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                # Narrowed to match me_parcels/oh_franklin/sc_horry, which do the
                # identical epoch-ms conversion. A bare `except Exception` here
                # also swallowed ImportError and AttributeError, so a refactor
                # that broke this line would have read as "Indiana simply has no
                # sale dates" rather than as a bug.
                pass

        return PropertyRecord(
            parcel_id=parcel,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=total_av,
            equity_percent=0.0,   # no mortgage/lien data in this source
            is_absentee_owner=absentee,
            distress_flags=flags,
            last_sale_date=last_sale,
        )
