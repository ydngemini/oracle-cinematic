"""Pennsylvania — Philadelphia OPA property assessments (CARTO SQL API).

Philadelphia's Office of Property Assessment publishes the full property file at
phl.carto.com as `opa_properties_public` — a real, documented SQL endpoint.
"""
from __future__ import annotations

from typing import Optional

from .base import CartoHarvester, classify_owner, to_float, norm
from .property_adapter import PropertyRecord


class PennsylvaniaPhillyOPAHarvester(CartoHarvester):
    STATE = "PA"
    SOURCE_LABEL = "Philadelphia OPA (CARTO)"
    CARTO_DOMAIN = "https://phl.carto.com"
    TABLE = "opa_properties_public"
    SELECT = (
        "parcel_number, location, owner_1, owner_2, mailing_street, mailing_city_state, "
        "mailing_zip, market_value, sale_date, sale_price, category_code_description"
    )
    # Residential categories only.
    WHERE = "category_code_description ILIKE '%RESIDENTIAL%'"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("parcel_number") or "").strip()
        addr = str(row.get("location") or "").strip()
        if not parcel or not addr:
            return None
        owner = " ".join(filter(None, [row.get("owner_1"), row.get("owner_2")])).strip()
        mail = str(row.get("mailing_street") or "").strip()
        absentee = bool(mail) and norm(mail) != norm(addr)
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("mailing_city_state") or "Philadelphia").strip(),
            state=self.STATE,
            zip_code=str(row.get("mailing_zip") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("market_value")),
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=["absentee_owner"] if absentee else [],
            last_sale_date=str(row.get("sale_date") or "").strip()[:10] or None,
        )
