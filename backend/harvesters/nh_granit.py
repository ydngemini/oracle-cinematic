"""New Hampshire — NHGRANIT / NH DRA statewide parcel mosaic (ArcGIS FeatureServer).

Source  : NH GRANIT / NH Department of Revenue Administration Parcel Mosaic
Portal  : ArcGIS FeatureServer (services9.arcgis.com — public, no token required)
Item ID : 30a8f9616d5842a8946e89be12d6dc01  (CAD_ParcelMosaicSecure, publicly readable)
Layer   : 0  — Parcel Points (full assessment attributes incl. owner + TaxTotal)
Scope   : statewide (~500 k+ parcels across all NH municipalities)
Coverage: owner name, mailing address, assessed land + building + total values,
          land-use code, structure attributes, parcel ID (PID).

Column map (source → PropertyRecord):
  PID            → parcel_id
  StreetAddress  → address
  Town           → city
  MailingZip     → zip_code
  OwnerName      → owner_name
  TaxTotal       → estimated_value   (total assessed value, land + building + features)
  TaxLand        → (equity signal: land fraction of TaxTotal)
  MailingCity    → (absentee check vs Town)
  MailingState   → (absentee check — non-NH mailing → absentee)
"""

from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, norm, to_float
from .property_adapter import PropertyRecord

# Public FeatureServer — item 30a8f9616d5842a8946e89be12d6dc01
# Layer 0: Parcel Points (all attribute fields exposed without token)
_SERVICE = (
    "https://services9.arcgis.com/wnvDDrXX8EouLkZP/arcgis/rest/services/"
    "CAD_ParcelMosaicSecure/FeatureServer/0/query"
)

_OUT_FIELDS = ",".join([
    "PID", "StreetAddress", "Town", "MailingCity", "MailingZip", "MailingState",
    "OwnerName", "CoOwnerName", "TaxTotal", "TaxLand", "TaxBldg",
    "SLU", "LandUseCode", "IsStateOwned",
])


class NewHampshireGRANITHarvester(ArcGISHarvester):
    """Statewide NH parcel + assessment data from the NH DRA Parcel Mosaic."""

    STATE = "NH"
    SOURCE_LABEL = "NH GRANIT / DRA Parcel Mosaic (ArcGIS)"
    SERVICE_URL = _SERVICE
    WHERE = "1=1"
    OUT_FIELDS = _OUT_FIELDS

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        pid = str(row.get("PID") or "").strip()
        addr = str(row.get("StreetAddress") or "").strip()
        if not pid or not addr:
            return None

        owner = str(row.get("OwnerName") or "").strip()
        if not owner:
            return None

        town = str(row.get("Town") or "").strip()
        mail_city = str(row.get("MailingCity") or "").strip()
        mail_state = str(row.get("MailingState") or "").strip()
        zip_raw = str(row.get("MailingZip") or "").strip()
        # NH zip codes sometimes arrive as "03052-" — strip trailing dash
        mailing_zip = zip_raw.rstrip("-").strip()[:10]

        # Absentee: mailing state is not NH, or mailing city differs from property town
        is_absentee = (
            (mail_state and mail_state.upper() not in ("NH", "N H"))
            or (mail_city and norm(mail_city) != norm(town))
        )

        # MailingZip is the OWNER's zip. NH publishes no situs zip, so for an
        # owner-occupied parcel it is a safe proxy and for an absentee owner it
        # is the owner's address, not the property's. Same rule as
        # wy_parcels.py. `city` is unaffected — it comes from Town, which is
        # the property's.
        zip_code = "" if is_absentee else mailing_zip

        # State-owned parcels are not investor targets
        is_state = str(row.get("IsStateOwned") or "").strip().lower() in ("1", "true", "yes")

        value = to_float(row.get("TaxTotal"))
        land_val = to_float(row.get("TaxLand"))
        # equity_percent: land as fraction of total assessed (proxy only; no debt data)
        equity_pct = round((land_val / value * 100.0) if value else 0.0, 2)

        flags: list[str] = []
        if is_absentee:
            flags.append("absentee_owner")
        if is_state:
            flags.append("state_owned")
        if value == 0.0:
            flags.append("zero_assessment")

        return PropertyRecord(
            parcel_id=pid,
            address=addr,
            city=town,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=equity_pct,
            is_absentee_owner=is_absentee,
            distress_flags=flags,
            last_sale_date=None,  # DRA Parcel Mosaic does not expose deed/sale date
        )
