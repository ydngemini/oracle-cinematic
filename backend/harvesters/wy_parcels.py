"""Wyoming — Statewide Parcels FeatureServer (ArcGIS).

Source: Wyoming Property Tax Division / Wyoming Enterprise Technology Services
        (ETS), assembled in collaboration with all 23 County Assessors and
        published as the "Wyoming Statewide Parcel Viewer".

Service:  https://services3.arcgis.com/r0iJ85SKZ4zAzz3P/arcgis/rest/services/
          Wyoming_Parcels_for_2026/FeatureServer/0/query
Coverage: Statewide — 373,666 parcels (332,990 with a non-zero actual/market
          value). This is the same authoritative layer that backs the public
          gis.wyo.gov/parcels viewer, so it is the canonical statewide source.

Column map (source → PropertyRecord):
  parcelnb     → parcel_id            (14-digit statewide parcel number)
  accountno    → parcel_id fallback   (assessor account, e.g. "R0002864")
  locationad   → address              (property situs street address)
  jurisdicti   → (county; no situs city is published)
  ownername1   → owner_name
  ownername2   → (secondary owner / "ATTN:" line — ignored for owner_type)
  mailstate    → (owner mailing state — absentee signal)
  mailcity     → (owner mailing city)
  mailzipcod   → zip_code fallback    (owner mailing zip; no situs zip published)
  actualvalu   → estimated_value      (full actual/market value)
  assessedva   → (statutory assessed value ≈ 9.5% of actual — fallback only)

Notes
-----
* Wyoming publishes no situs *city* or *zip* — only the street line
  (`locationad`). City/zip are therefore left blank rather than guessed; the
  parcel number + street + county is enough to key and enrich a lead.
* ~74% of *all* rows are unimproved/ag shells with no owner, value, or situs.
  The server-side `actualvalu > 0` filter drops those (situs fill jumps from
  26% → 72%), so a bounded run reaches the per-state cap without paging the
  entire 373k-row dataset. Rows that still lack a situs street are skipped.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord


class WyomingParcelsHarvester(ArcGISHarvester):
    """Statewide Wyoming parcel / assessment data from the ETS Property Tax
    Division Parcels FeatureServer."""

    STATE = "WY"
    SOURCE_LABEL = "WY ETS Statewide Parcels FeatureServer (2026)"

    SERVICE_URL = (
        "https://services3.arcgis.com/r0iJ85SKZ4zAzz3P/arcgis/rest/services"
        "/Wyoming_Parcels_for_2026/FeatureServer/0/query"
    )

    # Only valued parcels — excludes the unimproved/ag shells that carry no
    # owner, value, or situs address (and would otherwise be skipped anyway).
    WHERE = "actualvalu > 0"

    OUT_FIELDS = (
        "parcelnb,accountno,jurisdicti,ownername1,ownername2,"
        "mailaddres,mailcity,mailstate,mailzipcod,locationad,"
        "actualvalu,assessedva"
    )

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        # Parcel key: prefer the statewide parcel number, fall back to account no.
        parcel_id = str(row.get("parcelnb") or "").strip()
        if not parcel_id:
            parcel_id = str(row.get("accountno") or "").strip()
        if not parcel_id:
            return None

        # Situs street address (Wyoming publishes no situs city/zip).
        address = str(row.get("locationad") or "").strip()
        if not address:
            return None  # unusable as a property lead — counted as skipped

        owner = str(row.get("ownername1") or "").strip()
        if not owner:
            return None

        # Market value: actualvalu is full market value; fall back to grossing up
        # the statutory assessed value (~9.5% of actual) only if actual is 0.
        est_value = to_float(row.get("actualvalu"))
        if est_value == 0.0:
            est_value = to_float(row.get("assessedva")) / 0.095 if row.get("assessedva") else 0.0

        # Absentee: owner's mailing state is outside Wyoming. (No situs city is
        # published, so a city-level comparison isn't possible.)
        mail_state = str(row.get("mailstate") or "").strip().upper()
        is_absentee = bool(mail_state) and mail_state != "WY"

        distress_flags: list[str] = []
        if is_absentee:
            distress_flags.append("absentee_owner")

        # No situs zip is published. For an owner-occupied (non-absentee) WY
        # parcel the mailing zip is a safe proxy for the property zip; for an
        # absentee owner it would be the *owner's* zip, so leave it blank.
        situs_zip = "" if is_absentee else str(row.get("mailzipcod") or "").strip()[:10]

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city="",  # no situs city published by WY
            state=self.STATE,
            zip_code=situs_zip,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=est_value,
            equity_percent=0.0,  # not derivable from assessment alone; Scout enriches
            is_absentee_owner=is_absentee,
            distress_flags=distress_flags,
            last_sale_date=None,  # not published in this dataset
        )
