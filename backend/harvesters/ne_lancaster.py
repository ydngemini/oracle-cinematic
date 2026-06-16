"""Nebraska — Lancaster County (Lincoln) Tax Parcels (ArcGIS FeatureServer).

Source:  City of Lincoln / Lancaster County Assessor GIS
Portal:  https://gis.lincoln.ne.gov/public/rest/services/Assessor/TaxParcels/FeatureServer
Layer 0: TaxParcels — 225 000+ parcels, updated continuously by the Assessor's office.
Scope:   county:Lancaster  (most populous Nebraska county; no statewide owner-name layer exists)

Column map
----------
parcel_id       <- PARCELID
address         <- SITEADDRESS
city            <- PSTLCITY
zip_code        <- PSTLZIP5
owner_name      <- OWNERNME1  (OWNERNME2 appended when present)
estimated_value <- CNTASSDVAL (current assessed value)
last_sale_date  <- LGLSTARTDT (legal start / acquisition date; best proxy available)

Absentee detection: postal address (PSTLADDRESS) normalised against site address.
Distress flags:
  - 'absentee_owner'  when mailing ≠ site address
  - 'low_value'       when assessed value < $10 000 (likely vacant / unbuildable)
"""

from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord

# Base query endpoint — confirmed live 2026-06-14.
_SERVICE_URL = (
    "https://gis.lincoln.ne.gov/public/rest/services/Assessor/TaxParcels/FeatureServer/0/query"
)

# Only pull fields we actually use; keeps pages smaller.
_OUT_FIELDS = ",".join([
    "PARCELID",
    "SITEADDRESS",
    "PSTLADDRESS",
    "PSTLCITY",
    "PSTLSTATE",
    "PSTLZIP5",
    "OWNERNME1",
    "OWNERNME2",
    "CNTASSDVAL",
    "LNDVALUE",
    "USECD",
    "LGLSTARTDT",
])


class NebraskaLancasterHarvester(ArcGISHarvester):
    """Lancaster County (Lincoln, NE) assessor parcels via ArcGIS FeatureServer."""

    STATE = "NE"
    SOURCE_LABEL = "Lancaster County NE TaxParcels"
    SERVICE_URL = _SERVICE_URL
    WHERE = "PARCELID IS NOT NULL AND CNTASSDVAL > 0"
    OUT_FIELDS = _OUT_FIELDS

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELID") or "").strip()
        if not parcel:
            return None

        site_addr = str(row.get("SITEADDRESS") or "").strip()
        if not site_addr:
            return None

        # Owner: combine both name fields when a second owner exists.
        owner1 = str(row.get("OWNERNME1") or "").strip()
        owner2 = str(row.get("OWNERNME2") or "").strip()
        owner = f"{owner1} & {owner2}" if owner2 else owner1
        if not owner:
            return None

        city = str(row.get("PSTLCITY") or "").strip()
        zip_code = str(row.get("PSTLZIP5") or "").strip()[:10]

        # Assessed value (float).
        value = to_float(row.get("CNTASSDVAL"))

        # Absentee: mailing address normalised vs site address.
        postal = str(row.get("PSTLADDRESS") or "").strip()
        is_absentee = bool(postal) and norm(postal) != norm(site_addr)

        distress: list[str] = []
        if is_absentee:
            distress.append("absentee_owner")
        if 0 < value < 10_000:
            distress.append("low_value")

        # Last sale / legal start date — stored as epoch-ms by ArcGIS; convert to ISO string.
        lgl_raw = row.get("LGLSTARTDT")
        last_sale: Optional[str] = None
        if lgl_raw is not None:
            try:
                import datetime
                ts = int(lgl_raw) / 1000
                last_sale = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                last_sale = str(lgl_raw)[:10] if lgl_raw else None

        return PropertyRecord(
            parcel_id=parcel,
            address=site_addr,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=value,
            equity_percent=0.0,       # no mortgage data in source
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=last_sale,
        )
