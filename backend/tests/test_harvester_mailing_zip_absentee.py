"""A mailing ZIP or city alone does not make an owner absentee.

al_jefferson and mo_jackson read the owner's mailing city and ZIP and never
compared them; ruff found the unread values when the static gate was widened
to F841 (audit 35-03 phase 2). Comparing the ZIP was tried and measured on
8000 live rows per county: it flagged owner-occupants (AL: 47 of 2595
same-street-line parcels, 37 of those street lines a single parcel
county-wide, because the AL ZIP field is a municipality code; MO: 87 of 3164,
64 with the same city on both sides). So the values are not compared: the
street line and the out-of-state rules stand, and a cross-municipality rule
needs a county-wide street-line index.
"""

from __future__ import annotations

import pytest

from harvesters.al_jefferson import AlabamaJeffersonHarvester
from harvesters.mo_jackson import MissouriJacksonHarvester

_TENANT = "11111111-1111-1111-1111-111111111111"


def _al(*, mail_addr="100 OAK ST", mail_zip="35209", prop_zip="35209", mail_state="AL"):
    return AlabamaJeffersonHarvester(_TENANT).map_record({
        "PARCELID": "22 00 01 1 001 001.000",
        "OWNERNAME": "SMITH JOHN",
        "ADDR_PSPR": "100 OAK ST",
        "Property_City": "HOOVER",
        "ZIP": prop_zip,
        "PROP_MAIL": mail_addr,
        "CITYMAIL": "BIRMINGHAM",
        "ZIP_MAIL": mail_zip,
        "STATE_Mail": mail_state,
        "AssdValue": "30000",
    })


def _mo(*, owner_addr="100 OAK ST", owner_zip="64050", site_zip="64050"):
    return MissouriJacksonHarvester(_TENANT).map_record({
        "parcel_id": "26-123-45-67-00-0-00-000",
        "owner": "SMITH JOHN",
        "owneraddress": owner_addr,
        "ownercity": "KANSAS CITY",
        "ownerzipcode": owner_zip,
        "SitusAddress": "100 OAK ST",
        "SitusCity": "INDEPENDENCE",
        "SitusZipCode": site_zip,
        "Market_Value_Total": 200000,
        "AssessedValue": "38000",
    })


@pytest.mark.parametrize("build", [_al, _mo], ids=["al_jefferson", "mo_jackson"])
def test_the_same_street_line_in_another_zip_is_not_flagged_absentee(build):
    record = build(mail_zip="35180", prop_zip="35126") if build is _al else build(owner_zip="64015", site_zip="64014")
    assert record.is_absentee_owner is False
    assert "absentee_owner" not in record.distress_flags


@pytest.mark.parametrize("build", [_al, _mo], ids=["al_jefferson", "mo_jackson"])
def test_an_owner_at_the_property_is_not_absentee_although_the_city_names_differ(build):
    record = build()
    assert record.is_absentee_owner is False


@pytest.mark.parametrize("build", [_al, _mo], ids=["al_jefferson", "mo_jackson"])
def test_a_different_mailing_street_line_is_absentee(build):
    record = build(mail_addr="9 ELM AVE") if build is _al else build(owner_addr="9 ELM AVE")
    assert record.is_absentee_owner is True
    assert "absentee_owner" in record.distress_flags


def test_an_out_of_state_mailing_address_is_absentee():
    assert _al(mail_state="GA").is_absentee_owner is True
