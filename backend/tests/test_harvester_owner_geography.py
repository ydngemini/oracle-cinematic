"""A property record must not carry its owner's city or ZIP.

Four harvesters mapped an owner/mailing field into the property's own geography.
tn_shelby's field map said so in the module docstring — `OwnerCityStZip → city +
zip_code` — two lines below a note that OwnerAddress is the *mailing* address.
For an owner-occupied parcel the two coincide; for an absentee owner they do
not, which is how 48 parcels in Dover, Tennessee came to carry 19901, the ZIP
for Dover, Delaware.

wy_parcels.py already stated the correct rule in a comment. These tests hold the
others to it, because the failure is silent: every field is populated and
plausible, and the only symptom is that a ZIP lookup answers with the wrong
state's market.
"""

from __future__ import annotations

import pytest

_TENANT = "11111111-1111-1111-1111-111111111111"

from harvesters.mt_cadastral import MontanaCadastralHarvester
from harvesters.nh_granit import NewHampshireGRANITHarvester
from harvesters.tn_shelby import TennesseeShelbyHarvester


def _tn(owner_addr: str, property_addr: str = "100 MAIN ST"):
    return TennesseeShelbyHarvester(_TENANT).map_record({
        "PARCELID": "001017 00001C",
        "PropertyAddress": property_addr,
        "OwnerName": "SMITH JOHN",
        "OwnerAddress": owner_addr,
        "OwnerCityStZip": "DOVER, DE, 19901",
        "TotalAppraisal": "150000",
    })


def test_an_absentee_owners_zip_is_not_written_as_the_propertys():
    record = _tn(owner_addr="400 N FRONT ST")

    assert record.is_absentee_owner is True
    assert record.zip_code == "", "the owner's ZIP was stored as the property's"
    assert record.city == "", "the owner's city was stored as the property's"
    assert record.state == "TN"


def test_an_owner_occupied_parcel_keeps_the_mailing_geography():
    """When the owner lives there, the mailing city and ZIP *are* the
    property's. Blanking them unconditionally would throw away good data."""
    record = _tn(owner_addr="100 MAIN ST", property_addr="100 MAIN ST")

    assert record.is_absentee_owner is False
    assert record.zip_code == "19901"
    assert record.city == "Dover"


def test_no_default_city_is_invented_for_a_blank_field():
    """The harvester used to fall back to "Memphis". Shelby County contains six
    other municipalities, and the row does not say which one."""
    record = TennesseeShelbyHarvester(_TENANT).map_record({
        "PARCELID": "1", "PropertyAddress": "100 MAIN ST", "OwnerName": "X",
        "OwnerAddress": "999 ELSEWHERE RD", "OwnerCityStZip": "",
        "TotalAppraisal": "1",
    })
    assert record.city == ""


def test_new_hampshire_withholds_the_mailing_zip_from_an_absentee_parcel():
    """NH publishes no situs ZIP at all, so MailingZip is the only candidate —
    which makes gating it on absentee the whole of the correctness."""
    harvester = NewHampshireGRANITHarvester(_TENANT)
    absentee = harvester.map_record({
        "PID": "1", "StreetAddress": "5 ELM ST", "OwnerName": "SMITH JOHN",
        "Town": "NASHUA", "MailingCity": "BOSTON", "MailingState": "MA",
        "MailingZip": "02108-", "TaxTotal": "200000", "TaxLand": "50000",
    })
    resident = harvester.map_record({
        "PID": "2", "StreetAddress": "7 ELM ST", "OwnerName": "JONES ANN",
        "Town": "NASHUA", "MailingCity": "NASHUA", "MailingState": "NH",
        "MailingZip": "03060-", "TaxTotal": "200000", "TaxLand": "50000",
    })

    assert absentee.is_absentee_owner is True and absentee.zip_code == ""
    assert resident.is_absentee_owner is False and resident.zip_code == "03060"


def test_montana_prefers_the_situs_zip_and_will_not_substitute_an_absentee_owners():
    harvester = MontanaCadastralHarvester(_TENANT)
    with_situs = harvester.map_record({
        "PARCELID": "1", "AddressLine1": "10 PINE RD", "CityStateZip": "LIBBY, MT 59923",
        "OwnerName": "SMITH JOHN", "OwnerState": "CA", "OwnerZipCode": "90210",
        "TotalValue": "150000",
    })
    without_situs = harvester.map_record({
        "PARCELID": "2", "AddressLine1": "12 PINE RD", "CityStateZip": "",
        "OwnerName": "SMITH JOHN", "OwnerState": "CA", "OwnerZipCode": "90210",
        "TotalValue": "150000",
    })

    assert with_situs.zip_code == "59923", "the published situs ZIP must win"
    assert without_situs.is_absentee_owner is True
    assert without_situs.zip_code == "", "an absentee owner's ZIP is not the property's"
