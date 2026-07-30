from __future__ import annotations

from harvesters.il_cook import IllinoisCookHarvester


def test_cook_record_combines_current_assessor_characteristics_and_sale():
    harvester = IllinoisCookHarvester(
        "00000000-0000-0000-0000-000000000000",
        cache=object(),
    )
    harvester._current_year = 2026
    harvester._facts = {
        "19142090130000": {
            "value": {
                "pin": "19142090130000",
                "year": "2025",
                "class": "203",
                "board_land": "1827",
                "board_bldg": "21173",
                "board_tot": "23000",
            },
            "houses": [{
                "class": "203",
                "char_yrblt": "1925",
                "char_bldg_sf": "1728",
                "char_land_sf": "3654",
                "char_beds": "4",
                "char_rooms": "8",
                "char_fbath": "2",
                "char_hbath": "0",
                "char_use": "Single-Family",
            }],
            "condos": [],
            "universe": {
                "zip_code": "60629",
                "lat": "41.7904917279",
                "lon": "-87.7080199184",
            },
            "sale": {
                "sale_date": "2010-06-01T00:00:00.000",
                "sale_price": "160000",
                "buyer_name": "RANGEL MANUEL",
            },
        }
    }
    record = harvester.map_record({
        "pin": "19142090130000",
        "prop_address_full": "5639 S HOMAN AVE",
        "prop_address_city_name": "CHICAGO",
        "prop_address_zipcode_1": "60629",
        "owner_address_name": "MANUEL RANGEL",
        "mail_address_state": "IL",
        "mail_address_city_name": "CHICAGO",
    })

    assert record is not None
    assert record.parcel_id == "19142090130000"
    assert record.owner_name == "MANUEL RANGEL"
    assert record.county == "Cook"
    assert record.estimated_value == 230_000
    assert record.last_sale_price == 160_000
    assert record.last_sale_date == "2010-06-01"
    assert record.bedrooms == 4
    assert record.bathrooms == 2
    assert record.rooms == 8
    assert record.year_built == 1925
    assert record.building_area_sqft == 1728
    assert record.lot_area_sqft == 3654
    assert record.dataset_version == "address:2026;assessment:2025;characteristics:2026"
