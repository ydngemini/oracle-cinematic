"""Chicago — Building (code) Violations (Socrata, KEYLESS).

City of Chicago Open Data resource ``22u3-xenr`` publishes Department of
Buildings code-violation inspections. Filtering to ``violation_status='OPEN'``
yields buildings with unresolved code violations — a distress/motivated-seller
signal that complements the IL Cook County assessor parcel feed (il_cook.py).

KEYLESS — the optional Socrata ``X-App-Token`` (``SODA_APP_TOKEN``) only lifts
the throttle. The dataset has no parcel/PIN, so leads are keyed by the city's
building grouping id (``property_group``), falling back to the normalized
address. Each row → one ``PropertyRecord`` keyed ``CHIV:{building}`` so many
violations on one building collapse to a single idempotent distress lead.
"""
from __future__ import annotations

import os
from typing import Optional

from .base import SocrataHarvester, norm
from .property_adapter import PropertyRecord


class ChicagoBuildingViolationsHarvester(SocrataHarvester):
    STATE = "IL"
    SOURCE_LABEL = "Chicago Building Violations (Socrata 22u3-xenr)"
    RESOURCE_URL = "https://data.cityofchicago.org/resource/22u3-xenr.json"
    SOQL_WHERE = "violation_status='OPEN'"
    SOQL_ORDER = "id"

    async def _get_json(self, url: str, headers: Optional[dict] = None):
        token = os.getenv("SODA_APP_TOKEN")
        if token:
            headers = {**(headers or {}), "X-App-Token": token}
        return await super()._get_json(url, headers=headers)

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        address = str(row.get("address") or "").strip()
        if not address:
            return None
        group = str(row.get("property_group") or "").strip()
        key = group or norm(address)
        if not key:
            return None

        flags = ["code_violation", "chicago_building_violation", "open_violation"]
        descr = str(row.get("violation_description") or "").upper()
        if "DANGEROUS" in descr or "HAZARD" in descr:
            flags.append("dangerous_or_hazardous")
        if "VACANT" in descr:
            flags.append("vacant")

        return PropertyRecord(
            parcel_id=f"CHIV:{key}",
            address=address,
            city="Chicago",
            state=self.STATE,
            zip_code="",                    # dataset carries no ZIP column
            owner_name="",                  # violations carry no owner name
            owner_type="individual",
            estimated_value=0.0,
            equity_percent=0.0,
            is_absentee_owner=False,
            distress_flags=flags,
            last_sale_date=None,
        )
