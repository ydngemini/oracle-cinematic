"""Michigan — Kent County Equalization parcel/assessment data (ArcGIS MapServer).

Source:  Kent County Bureau of Equalization / GIS
Service: https://gis.kentcountymi.gov/agisprod/rest/services/ParcelsWithCondos/MapServer/0/query
Scope:   county:Kent  (~231 k tax parcels; Grand Rapids metro)
Fields confirmed live 2026-06-14:
  PPN, PNUM, OWNERNAME1, OWNERNAME2, OWNERADDRESS, OWNERCITY, OWNERZIPCODE,
  PROPERTYADDRESS, PROPADDRESSCITY, PROPADDRESSSTATE_ZIPCODE,
  PROPERTYCLASS, SEVTRIBUNAL1, TAXABLETRIBUNAL1

SEVTRIBUNAL1 = State Equalized Value (50 % of True Cash Value per Michigan law).
We store SEV * 2 as estimated_value so it reflects market value, not the
capped equalized figure.  TAXABLETRIBUNAL1 is the taxable / capped value.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class MichiganKentHarvester(ArcGISHarvester):
    STATE = "MI"
    SOURCE_LABEL = "Kent County Equalization parcels"

    # MapServer layer 0 — "Parcels With Condos"
    SERVICE_URL = (
        "https://gis.kentcountymi.gov/agisprod/rest/services/"
        "ParcelsWithCondos/MapServer/0/query"
    )

    # Only real tax parcels (excludes right-of-way / non-assessable slivers)
    WHERE = "TAXPARCEL_FLAG = 'Yes'"
    OUT_FIELDS = (
        "PPN,PNUM,OWNERNAME1,OWNERNAME2,OWNERADDRESS,OWNERCITY,OWNERZIPCODE,"
        "PROPERTYADDRESS,PROPADDRESSCITY,PROPADDRESSSTATE_ZIPCODE,"
        "PROPERTYCLASS,SEVTRIBUNAL1,TAXABLETRIBUNAL1"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # PNUM is the human-readable parcel number (e.g. "41-01-05-200-051").
        # PPN is a floating-point encoding of the same; prefer PNUM.
        parcel = str(row.get("PNUM") or row.get("PPN") or "").strip()
        prop_addr = str(row.get("PROPERTYADDRESS") or "").strip()
        if not parcel or not prop_addr:
            return None

        owner = str(row.get("OWNERNAME1") or "").strip()
        owner2 = str(row.get("OWNERNAME2") or "").strip()
        if owner2 and owner2 != owner:
            owner = f"{owner} / {owner2}".strip(" /")
        if not owner:
            return None

        city = str(row.get("PROPADDRESSCITY") or "").strip()

        # PROPADDRESSSTATE_ZIPCODE is "MI49330" — extract the trailing 5 digits
        state_zip = str(row.get("PROPADDRESSSTATE_ZIPCODE") or "").strip()
        zip_code = state_zip[-5:] if len(state_zip) >= 5 else ""

        # Michigan SEV = 50 % of True Cash Value by statute; multiply by 2 for market est.
        sev = to_float(row.get("SEVTRIBUNAL1"))
        estimated_value = sev * 2.0

        taxable = to_float(row.get("TAXABLETRIBUNAL1"))

        # Equity is unknowable without lien data — default 0 (underwriter fills this).
        equity_percent = 0.0

        # Absentee: owner mailing address differs from property address.
        owner_addr = str(row.get("OWNERADDRESS") or "").strip()
        owner_city = str(row.get("OWNERCITY") or "").strip()
        absentee = bool(owner_addr) and (
            norm(owner_addr) != norm(prop_addr) or norm(owner_city) != norm(city)
        )

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")
        # Taxable value significantly below SEV can indicate long-held property
        # (Proposal A cap) — not distress per se, but a motivated-seller signal.
        if sev > 0 and taxable > 0 and (taxable / sev) < 0.6:
            distress.append("high_proposal_a_cap")

        return PropertyRecord(
            parcel_id=parcel,
            address=prop_addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=equity_percent,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=None,   # not exposed in this service layer
        )
