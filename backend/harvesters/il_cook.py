"""Illinois — reconciled Cook County assessor and recorder public records.

Cook County publishes one authoritative PIN across several open Socrata tables:

* 3723-97qp — current parcel address, owner and taxpayer mailing address
* uzyt-m557 — assessed land/building values
* x54s-btds — single/multi-family characteristics
* 3r7i-mrz4 — condominium unit characteristics
* nj4t-kc8j — parcel universe, coordinates and public districts
* wvhk-k5uv — recorded parcel sales

The harvester pages through the current address/owner table and performs
bounded, chunked joins by PIN. It never fills a characteristic from a model.
When a source table omits a value it remains NULL in the public catalog.
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import Any, Optional

from .base import PAGE_SIZE, SocrataHarvester, classify_owner, logger, to_float
from .property_adapter import PropertyRecord

_ADDR_URL = "https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"
_VALUE_URL = "https://datacatalog.cookcountyil.gov/resource/uzyt-m557.json"
_HOUSE_URL = "https://datacatalog.cookcountyil.gov/resource/x54s-btds.json"
_CONDO_URL = "https://datacatalog.cookcountyil.gov/resource/3r7i-mrz4.json"
_UNIVERSE_URL = "https://datacatalog.cookcountyil.gov/resource/nj4t-kc8j.json"
_SALES_URL = "https://datacatalog.cookcountyil.gov/resource/wvhk-k5uv.json"

_ASSESS_MULTIPLIER = 10.0
_PIN_CHUNK = max(10, min(150, int(os.getenv("IL_COOK_PIN_CHUNK", "100"))))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _year(value: Any) -> Optional[int]:
    try:
        parsed = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    return parsed if 1900 <= parsed <= 2200 else None


def _positive(value: Any) -> Optional[float]:
    number = to_float(value)
    return number if number > 0 else None


def _escaped(value: str) -> str:
    return value.replace("'", "''")


class IllinoisCookHarvester(SocrataHarvester):
    """Cook County parcel identity plus every source-published public fact."""

    STATE = "IL"
    SOURCE_KEY = "regional_parcels_il"
    SOURCE_LABEL = (
        "Cook County Assessor — addresses, assessments, characteristics, "
        "parcel universe and recorded sales"
    )
    RESOURCE_URL = _ADDR_URL
    SOQL_ORDER = "pin"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_year: Optional[int] = None
        self._facts: dict[str, dict[str, Any]] = {}

    async def _query(
        self,
        url: str,
        *,
        where: Optional[str] = None,
        select: Optional[str] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> list[dict]:
        params: dict[str, Any] = {}
        if where:
            params["$where"] = where
        if select:
            params["$select"] = select
        if order:
            params["$order"] = order
        if limit is not None:
            params["$limit"] = limit
        if offset is not None:
            params["$offset"] = offset
        result = await self._get_json(f"{url}?{urllib.parse.urlencode(params)}")
        return result if isinstance(result, list) else []

    async def _discover_current_year(self) -> int:
        rows = await self._query(_ADDR_URL, select="max(year) as max_year", limit=1)
        discovered = _year(rows[0].get("max_year")) if rows else None
        if discovered is None:
            raise RuntimeError("Cook County address dataset did not publish a current year")
        self._current_year = discovered
        self.metrics["dataset_year"] = discovered
        return discovered

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        """Page current owner/address rows and join all fact tables per page."""
        url = os.getenv("IL_SOURCE_URL", self.RESOURCE_URL)
        current_year = await self._discover_current_year()
        self._facts = {}
        out: list[dict] = []
        offset = self._page_checkpoint
        while True:
            page_size = min(
                PAGE_SIZE,
                (max_records - len(out)) if max_records else PAGE_SIZE,
            )
            if page_size <= 0:
                break
            rows = await self._query(
                url,
                where=f"year='{current_year}'",
                order=self.SOQL_ORDER,
                limit=page_size,
                offset=offset,
            )
            if not rows:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            await self._prefetch_facts([_text(row.get("pin")) for row in rows])
            out.extend(rows)
            offset += len(rows)
            self._checkpoint_end = offset
            if len(rows) < page_size:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            if max_records and len(out) >= max_records:
                self._checkpoint_complete = False
                break
        return out

    async def lookup_address(self, address: str) -> list[PropertyRecord]:
        """Resolve an exact street address to canonical current Cook PIN rows."""
        normalized = " ".join(str(address or "").upper().split())
        if len(normalized) < 5:
            return []
        current_year = self._current_year or await self._discover_current_year()
        rows = await self._query(
            _ADDR_URL,
            where=(
                f"year='{current_year}' AND "
                f"upper(prop_address_full)='{_escaped(normalized)}'"
            ),
            order="pin",
            limit=50,
        )
        if not rows:
            return []
        await self._prefetch_facts([_text(row.get("pin")) for row in rows])
        return [
            record
            for row in rows
            if (record := self.map_record(row)) is not None
        ]

    async def _prefetch_facts(self, pins: list[str]) -> None:
        unique = sorted({pin for pin in pins if pin})
        current_year = self._current_year
        if not unique or current_year is None:
            return
        for start in range(0, len(unique), _PIN_CHUNK):
            chunk = unique[start:start + _PIN_CHUNK]
            pin_list = ",".join(f"'{_escaped(pin)}'" for pin in chunk)
            current = f"pin in({pin_list}) AND year='{current_year}'"
            any_complete_value = (
                f"pin in({pin_list}) AND "
                "(board_tot IS NOT NULL OR certified_tot IS NOT NULL)"
            )
            tasks = (
                self._query(
                    _VALUE_URL,
                    where=any_complete_value,
                    select="pin,year,class,certified_land,certified_bldg,"
                           "certified_tot,board_land,board_bldg,board_tot",
                    order="pin,year DESC",
                    limit=len(chunk) * 8,
                ),
                self._query(
                    _HOUSE_URL,
                    where=current,
                    order="pin,card",
                    limit=len(chunk) * 8,
                ),
                self._query(
                    _CONDO_URL,
                    where=current,
                    order="pin,card",
                    limit=len(chunk) * 8,
                ),
                self._query(
                    _UNIVERSE_URL,
                    where=current,
                    select="pin,year,class,zip_code,lon,lat,township_name,"
                           "nbhd_code,cook_municipality_name,ward_num,"
                           "chicago_community_area_name,env_flood_fema_sfha,"
                           "school_elementary_district_name,"
                           "school_secondary_district_name",
                    limit=len(chunk) * 2,
                ),
                self._query(
                    _SALES_URL,
                    where=f"pin in({pin_list})",
                    select="pin,year,class,sale_date,sale_price,doc_no,deed_type,"
                           "seller_name,buyer_name,sale_type,is_multisale,"
                           "num_parcels_sale",
                    order="pin,sale_date DESC",
                    limit=len(chunk) * 12,
                ),
            )
            # The shared limiter serializes requests to a polite cadence even
            # though independent table reads are scheduled together.
            values, houses, condos, universes, sales = await asyncio.gather(*tasks)
            for pin in chunk:
                facts = self._facts.setdefault(pin, {})
                facts["value"] = next(
                    (row for row in values if _text(row.get("pin")) == pin),
                    None,
                )
                facts["houses"] = [
                    row for row in houses if _text(row.get("pin")) == pin
                ]
                facts["condos"] = [
                    row for row in condos if _text(row.get("pin")) == pin
                ]
                facts["universe"] = next(
                    (row for row in universes if _text(row.get("pin")) == pin),
                    None,
                )
                facts["sale"] = next(
                    (row for row in sales if _text(row.get("pin")) == pin),
                    None,
                )

    @staticmethod
    def _characteristics(facts: dict[str, Any]) -> dict[str, Any]:
        house_rows = list(facts.get("houses") or [])
        condo_rows = list(facts.get("condos") or [])
        if house_rows:
            rows = house_rows
            building = sum(_positive(row.get("char_bldg_sf")) or 0 for row in rows) or None
            lot = max((_positive(row.get("char_land_sf")) or 0 for row in rows), default=0) or None
            bedrooms = sum(_positive(row.get("char_beds")) or 0 for row in rows)
            rooms = sum(_positive(row.get("char_rooms")) or 0 for row in rows)
            full = sum(_positive(row.get("char_fbath")) or 0 for row in rows)
            half = sum(_positive(row.get("char_hbath")) or 0 for row in rows)
            years = [_year(row.get("char_yrblt")) for row in rows]
            first = rows[0]
            return {
                "kind": "single_multi_family",
                "building_area_sqft": building,
                "lot_area_sqft": lot,
                "bedrooms": bedrooms or None,
                "rooms": rooms or None,
                "bathrooms": (full + half * 0.5) if (full or half) else None,
                "bathrooms_full": full if full or half else None,
                "bathrooms_half": half if full or half else None,
                "year_built": min(year for year in years if year) if any(years) else None,
                "property_class": _text(first.get("class")) or None,
                "land_use": _text(first.get("char_use")) or None,
                "card_count": len(rows),
                "residence_type": _text(first.get("char_type_resd")) or None,
                "construction_quality": _text(first.get("char_cnst_qlty")) or None,
                "apartments": _text(first.get("char_apts")) or None,
                "garage_size": _text(first.get("char_gar1_size")) or None,
                "attic": _text(first.get("char_attic_type")) or None,
                "basement": _text(first.get("char_bsmt")) or None,
                "exterior_wall": _text(first.get("char_ext_wall")) or None,
                "heat": _text(first.get("char_heat")) or None,
                "condition": _text(first.get("char_repair_cnd")) or None,
                "roof": _text(first.get("char_roof_cnst")) or None,
                "central_air": _text(first.get("char_air")) or None,
            }
        if condo_rows:
            rows = condo_rows
            first = rows[0]
            full = sum(_positive(row.get("char_full_baths")) or 0 for row in rows)
            half = sum(_positive(row.get("char_half_baths")) or 0 for row in rows)
            years = [_year(row.get("char_yrblt")) for row in rows]
            return {
                "kind": "condominium",
                "building_area_sqft": (
                    sum(_positive(row.get("char_unit_sf")) or 0 for row in rows) or None
                ),
                "lot_area_sqft": max(
                    (_positive(row.get("char_land_sf")) or 0 for row in rows),
                    default=0,
                ) or None,
                "bedrooms": (
                    sum(_positive(row.get("char_bedrooms")) or 0 for row in rows) or None
                ),
                "rooms": None,
                "bathrooms": (full + half * 0.5) if (full or half) else None,
                "bathrooms_full": full if full or half else None,
                "bathrooms_half": half if full or half else None,
                "year_built": min(year for year in years if year) if any(years) else None,
                "property_class": _text(first.get("class")) or None,
                "land_use": "Condominium",
                "card_count": len(rows),
                "building_square_feet": _positive(first.get("char_building_sf")),
                "building_units": _positive(first.get("char_building_pins")),
                "parking_space": first.get("is_parking_space"),
                "common_area": first.get("is_common_area"),
            }
        return {}

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        pin = _text(row.get("pin"))
        address = _text(row.get("prop_address_full"))
        owner = _text(row.get("owner_address_name"))
        if not pin or not address or not owner or owner.upper() == "CURRENT OWNER":
            return None

        facts = self._facts.get(pin, {})
        value_row = facts.get("value") or {}
        universe = facts.get("universe") or {}
        sale = facts.get("sale") or {}
        characteristics = self._characteristics(facts)
        property_class = (
            _text(characteristics.get("property_class"))
            or _text(value_row.get("class"))
            or _text(universe.get("class"))
            or None
        )

        assessed_total = (
            _positive(value_row.get("board_tot"))
            or _positive(value_row.get("certified_tot"))
        )
        # Cook class-2 residential property is assessed at 10% of market value.
        # Other classes retain the literal assessed total rather than applying
        # a residential ratio to commercial/industrial property.
        estimated_value = 0.0
        valuation_basis = "unavailable"
        if assessed_total is not None:
            if str(property_class or "").startswith("2"):
                estimated_value = assessed_total * _ASSESS_MULTIPLIER
                valuation_basis = "cook_residential_assessed_total_x10"
            else:
                estimated_value = assessed_total
                valuation_basis = "published_assessed_total"

        city = _text(row.get("prop_address_city_name"))
        zip_code = (
            _text(row.get("prop_address_zipcode_1"))
            or _text(universe.get("zip_code"))
        )[:10]
        mail_state = _text(row.get("mail_address_state")).upper()
        mail_city = _text(row.get("mail_address_city_name")).upper()
        is_absentee = (mail_state not in {"", "IL"}) or (
            bool(mail_city and city) and mail_city != city.upper()
        )

        distress: list[str] = []
        if estimated_value == 0.0:
            distress.append("no_assessed_value")
        if is_absentee:
            distress.append("absentee_owner")

        sale_price = _positive(sale.get("sale_price"))
        sale_date = _text(sale.get("sale_date"))[:10] or None
        assessed_year = _year(value_row.get("year"))
        current_year = self._current_year
        metadata = {
            "datasets": {
                "address_owner": "3723-97qp",
                "assessed_value": "uzyt-m557",
                "house_characteristics": "x54s-btds",
                "condo_characteristics": "3r7i-mrz4",
                "parcel_universe": "nj4t-kc8j",
                "parcel_sales": "wvhk-k5uv",
            },
            "address_year": current_year,
            "assessment_year": assessed_year,
            "valuation_basis": valuation_basis,
            "assessed_land": (
                _positive(value_row.get("board_land"))
                or _positive(value_row.get("certified_land"))
            ),
            "assessed_building": (
                _positive(value_row.get("board_bldg"))
                or _positive(value_row.get("certified_bldg"))
            ),
            "assessed_total": assessed_total,
            "characteristics": characteristics,
            "latest_sale": {
                "price": sale_price,
                "date": sale_date,
                "document_number": _text(sale.get("doc_no")) or None,
                "deed_type": _text(sale.get("deed_type")) or None,
                "seller": _text(sale.get("seller_name")) or None,
                "buyer": _text(sale.get("buyer_name")) or None,
                "sale_type": _text(sale.get("sale_type")) or None,
                "multi_parcel_sale": sale.get("is_multisale"),
                "parcel_count": _positive(sale.get("num_parcels_sale")),
            },
            "geography": {
                "township": _text(universe.get("township_name")) or None,
                "neighborhood": _text(universe.get("nbhd_code")) or None,
                "municipality": _text(universe.get("cook_municipality_name")) or None,
                "ward": _text(universe.get("ward_num")) or None,
                "community_area": _text(
                    universe.get("chicago_community_area_name")
                ) or None,
                "fema_special_flood_hazard_area": universe.get("env_flood_fema_sfha"),
                "elementary_school": _text(
                    universe.get("school_elementary_district_name")
                ) or None,
                "secondary_school": _text(
                    universe.get("school_secondary_district_name")
                ) or None,
            },
        }

        return PropertyRecord(
            parcel_id=pin,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=distress,
            last_sale_date=sale_date,
            county="Cook",
            bedrooms=characteristics.get("bedrooms"),
            bathrooms=characteristics.get("bathrooms"),
            rooms=characteristics.get("rooms"),
            year_built=characteristics.get("year_built"),
            property_class=property_class,
            last_sale_price=sale_price,
            lot_area_sqft=characteristics.get("lot_area_sqft"),
            building_area_sqft=characteristics.get("building_area_sqft"),
            land_use=characteristics.get("land_use"),
            latitude=_positive(universe.get("lat")),
            longitude=(
                to_float(universe.get("lon"))
                if universe.get("lon") not in (None, "") else None
            ),
            dataset_version=(
                f"address:{current_year};assessment:{assessed_year or 'unavailable'};"
                f"characteristics:{current_year}"
            ),
            source_metadata=metadata,
        )
