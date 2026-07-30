from __future__ import annotations

from harvesters.pa_philly_opa import PennsylvaniaPhillyOPAHarvester


def test_philadelphia_opa_maps_all_published_core_property_facts():
    harvester = PennsylvaniaPhillyOPAHarvester(
        "00000000-0000-0000-0000-000000000000",
        cache=object(),
    )
    record = harvester.map_record(
        {
            "parcel_number": "012345600",
            "location": "1234 MARKET ST",
            "owner_1": "EXAMPLE",
            "owner_2": "OWNER",
            "mailing_street": "9 ELSEWHERE AVE",
            "zip_code": "19107",
            "market_value": "525000",
            "sale_date": "2022-05-18",
            "sale_price": "410000",
            "number_of_bedrooms": "3",
            "number_of_bathrooms": "2.5",
            "number_of_rooms": "7",
            "year_built": "1920",
            "total_livable_area": "1840",
            "total_area": "2100",
            "category_code_description": "Single Family",
        }
    )

    assert record is not None
    assert record.city == "Philadelphia"
    assert record.county == "Philadelphia"
    assert record.zip_code == "19107"
    assert record.estimated_value == 525_000
    assert record.last_sale_price == 410_000
    assert record.bedrooms == 3
    assert record.bathrooms == 2.5
    assert record.rooms == 7
    assert record.year_built == 1920
    assert record.building_area_sqft == 1840
    assert record.lot_area_sqft == 2100
