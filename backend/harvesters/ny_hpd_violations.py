"""New York City — HPD Housing Maintenance Code Violations (Socrata, KEYLESS).

NYC Open Data resource ``wvxf-dwi5`` is one of the catalog's highest-ROI distress
sources: ~2.9M *open* housing-code violations, each carrying the building's BBL
(parcel), address, ZIP, severity class, and rent-impairing flag. An open
violation is a direct motivated-seller signal (deferred maintenance + landlord
pressure), so this harvester turns the feed into distress leads.

KEYLESS — a Socrata ``X-App-Token`` only lifts the throttle and is optional
(set ``SODA_APP_TOKEN`` to use one). Each row → one ``PropertyRecord`` keyed by
``HPDV:{bbl}`` so re-harvests upsert one distress lead per building (idempotent
via leads UNIQUE(tenant_id, parcel_id) from 0018) *without* clobbering the NYC
PLUTO parcel rows, which are keyed by the raw BBL.
"""
from __future__ import annotations

import os
from typing import Optional

from .base import SocrataHarvester
from .property_adapter import PropertyRecord


class NYCHPDViolationsHarvester(SocrataHarvester):
    STATE = "NY"
    SOURCE_LABEL = "NYC HPD Open Housing-Code Violations (Socrata wvxf-dwi5)"
    RESOURCE_URL = "https://data.cityofnewyork.us/resource/wvxf-dwi5.json"
    SOQL_WHERE = "violationstatus='Open'"
    SOQL_ORDER = "violationid"

    async def _get_json(self, url: str, headers: Optional[dict] = None):
        token = os.getenv("SODA_APP_TOKEN")
        if token:
            headers = {**(headers or {}), "X-App-Token": token}
        return await super()._get_json(url, headers=headers)

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        bbl = str(row.get("bbl") or "").strip()
        house = str(row.get("housenumber") or "").strip()
        street = str(row.get("streetname") or "").strip()
        address = " ".join(p for p in (house, street) if p).strip()
        if not bbl or not address:
            return None

        flags = ["code_violation", "hpd_open_violation"]
        vclass = str(row.get("class") or "").strip().upper()
        if vclass == "C":          # immediately hazardous (e.g. no heat, lead, mold)
            flags.append("hazardous_violation_class_c")
        elif vclass == "B":        # hazardous
            flags.append("hazardous_violation_class_b")
        if str(row.get("rentimpairing") or "").strip().upper() == "Y":
            flags.append("rent_impairing")

        return PropertyRecord(
            parcel_id=f"HPDV:{bbl}",
            address=address,
            city=str(row.get("boro") or "New York").strip().title(),
            state=self.STATE,
            zip_code=str(row.get("zip") or "").strip()[:10],
            owner_name="",                 # HPD violations carry no owner name
            owner_type="individual",
            estimated_value=0.0,
            equity_percent=0.0,
            is_absentee_owner=False,
            distress_flags=flags,
            last_sale_date=None,
        )
