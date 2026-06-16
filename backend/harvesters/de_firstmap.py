"""Delaware — New Castle County parcels + ownership (ArcGIS MapServer).

Source: New Castle County GIS (gis.nccde.org)
Service: BaseMaps/Base_Layers/MapServer
  Layer 0 — Parcels (situs address + owner name + owner mailing + property class)

The statewide FirstMap DE_StateParcels service is cadastral-geometry only — its
layers expose nothing but PIN/ACRES/COUNTY (no owner, no value, no real address),
so it yields no usable property records. Delaware ownership data lives in the
county assessment offices, not the statewide service. New Castle County's public
parcels layer carries the real situs address (ADDRESS/PROPCITY/PROPSTATE/PROPZIP),
the owner of record (CNTCTLAST), and the owner mailing address (OWNADDR/OWN*),
which is everything PropertyRecord needs except a published assessed dollar value
(NCC does not expose assessed value in its public GIS).

Scope: New Castle County only (statewide owner/value data is not published by DE).

Quirk: this MapServer layer only accepts outFields="*"; any explicit field subset
400s ("Failed to execute query"). OUT_FIELDS must stay "*" — the base archetype's
resultOffset pagination works fine alongside the wildcard.

Real column → PropertyRecord mapping
  PARCELNO (fallback PRCLID) → parcel_id
  ADDRESS                    → address  (situs street; whitespace-normalized)
  PROPCITY                   → city
  PROPSTATE                  → state (situs; always DE for NCC)
  PROPZIP                    → zip_code (Esri pads "19803-    "; trimmed)
  CNTCTLAST                  → owner_name (owner of record)
  OWNSTATE                   → is_absentee_owner heuristic (mailing != DE)
  PROPCLASS                  → distress/owner-type hints
  (no assessed value published by NCC) → estimated_value = 0.0
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class DelawareFirstMapHarvester(ArcGISHarvester):
    STATE = "DE"
    SOURCE_LABEL = "New Castle County parcels + ownership"
    SERVICE_URL = (
        "https://gis.nccde.org/agsserver/rest/services/"
        "BaseMaps/Base_Layers/MapServer/0/query"
    )
    # Primary-address parcels only (one situs row per parcel). This layer rejects
    # any explicit outFields subset, so OUT_FIELDS must remain the wildcard.
    WHERE = "PRIMADDR='Y'"
    OUT_FIELDS = "*"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCELNO") or row.get("PRCLID") or "").strip()
        owner = str(row.get("CNTCTLAST") or "").strip()
        if not parcel or not owner:
            return None

        # Situs street arrives space-padded ("1405     SMITH BRIDGE RD  ").
        address = " ".join(str(row.get("ADDRESS") or "").split())
        if not address:
            return None

        city = str(row.get("PROPCITY") or "").strip().title()
        zip_code = str(row.get("PROPZIP") or "").strip().rstrip("-").strip()[:10]

        # NCC publishes no assessed dollar value in its public GIS.
        estimated_value = to_float(row.get("ASSESS"))

        # Absentee: owner mailing state differs from DE.
        own_state = str(row.get("OWNSTATE") or "").strip().upper()
        is_absentee = own_state not in ("DE", "")

        distress_flags: list[str] = []
        if is_absentee:
            distress_flags.append("absentee_owner")
        if "EXEMPT" in str(row.get("PROPCLASS") or "").upper():
            distress_flags.append("tax_exempt")

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
            distress_flags=distress_flags,
            last_sale_date=None,
        )
