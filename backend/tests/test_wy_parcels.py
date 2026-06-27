"""
Unit tests for harvesters/wy_parcels.py — the Wyoming statewide-parcels harvester.

The Wyoming harvester is an ArcGISHarvester subclass whose only bespoke logic is
``map_record(row) -> PropertyRecord | None``: it keys parcels, requires a situs
street + owner, grosses up assessed value when actual is missing, derives the
absentee signal from the owner's mailing state, and decides whether the mailing
zip may stand in for the (unpublished) situs zip.

These tests pin every branch of that mapping plus the static endpoint contract,
and run a stubbed ``harvest()`` end-to-end (no network, no DB) to confirm the
skip/parse accounting that the base template method drives off ``map_record``.

Run:  python -m pytest backend/tests/test_wy_parcels.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Resolve `harvesters.*` whether pytest is launched from repo root or backend/.
sys.path.insert(0, str(Path(__file__).parent.parent))

from harvesters.base import ArcGISHarvester, classify_owner, to_float  # noqa: E402
from harvesters.property_adapter import PropertyRecord  # noqa: E402
from harvesters.wy_parcels import WyomingParcelsHarvester  # noqa: E402

# A non-null tenant is required by BaseHarvester.__init__ (leads.tenant_id is
# NOT NULL + RLS-checked). Any UUID-shaped string works for in-process mapping.
_TENANT = "00000000-0000-0000-0000-000000000000"


def _base_row(**overrides) -> dict:
    """A fully-populated, valid WY parcel row. Override individual keys per test."""
    row = {
        "parcelnb": "12345678901234",
        "accountno": "R0002864",
        "jurisdicti": "Laramie",
        "ownername1": "JOHN Q PUBLIC",
        "ownername2": "",
        "mailaddres": "PO BOX 7",
        "mailcity": "CHEYENNE",
        "mailstate": "WY",
        "mailzipcod": "82001",
        "locationad": "456 ELK AVE",
        "actualvalu": 250000,
        "assessedva": 23750,
    }
    row.update(overrides)
    return row


@pytest.fixture
def harv() -> WyomingParcelsHarvester:
    return WyomingParcelsHarvester(tenant_id=_TENANT)


# --------------------------------------------------------------------------- #
# Static endpoint / class contract
# --------------------------------------------------------------------------- #
class TestClassContract:
    def test_is_arcgis_subclass(self):
        assert issubclass(WyomingParcelsHarvester, ArcGISHarvester)

    def test_state_and_label(self):
        assert WyomingParcelsHarvester.STATE == "WY"
        assert "Wyoming" in WyomingParcelsHarvester.SOURCE_LABEL or \
            "WY" in WyomingParcelsHarvester.SOURCE_LABEL

    def test_service_url_is_arcgis_query_endpoint(self):
        url = WyomingParcelsHarvester.SERVICE_URL
        assert url.startswith("https://")
        assert "arcgis" in url.lower()
        assert url.rstrip("/").endswith("/query")

    def test_where_filters_to_valued_parcels(self):
        # The server-side filter is what lifts situs fill-rate 26% -> 72%.
        assert WyomingParcelsHarvester.WHERE == "actualvalu > 0"

    def test_out_fields_request_every_column_map_record_reads(self):
        out = WyomingParcelsHarvester.OUT_FIELDS
        for col in (
            "parcelnb", "accountno", "ownername1",
            "mailstate", "mailzipcod", "locationad",
            "actualvalu", "assessedva",
        ):
            assert col in out, f"{col} missing from OUT_FIELDS"

    def test_default_agent_id_derives_from_state(self, harv):
        assert harv.agent_id == "firehose-wy"

    def test_construction_requires_tenant(self):
        with pytest.raises(ValueError):
            WyomingParcelsHarvester(tenant_id="")


# --------------------------------------------------------------------------- #
# Happy path — a complete, in-state owner-occupied row
# --------------------------------------------------------------------------- #
class TestHappyPath:
    def test_returns_property_record(self, harv):
        rec = harv.map_record(_base_row())
        assert isinstance(rec, PropertyRecord)

    def test_core_fields_mapped(self, harv):
        rec = harv.map_record(_base_row())
        assert rec.parcel_id == "12345678901234"
        assert rec.address == "456 ELK AVE"
        assert rec.owner_name == "JOHN Q PUBLIC"
        assert rec.estimated_value == 250000.0

    def test_static_fields(self, harv):
        rec = harv.map_record(_base_row())
        assert rec.state == "WY"
        assert rec.city == ""          # WY publishes no situs city
        assert rec.equity_percent == 0.0
        assert rec.last_sale_date is None

    def test_owner_occupied_is_not_absentee(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY"))
        assert rec.is_absentee_owner is False
        assert rec.distress_flags == []

    def test_owner_occupied_zip_uses_mailing_zip(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY", mailzipcod="82001"))
        assert rec.zip_code == "82001"


# --------------------------------------------------------------------------- #
# Parcel key selection: parcelnb preferred, accountno fallback, else skip
# --------------------------------------------------------------------------- #
class TestParcelKey:
    def test_prefers_parcel_number(self, harv):
        rec = harv.map_record(_base_row(parcelnb="999", accountno="R1"))
        assert rec.parcel_id == "999"

    def test_falls_back_to_account_when_parcelnb_blank(self, harv):
        rec = harv.map_record(_base_row(parcelnb="", accountno="R0002864"))
        assert rec.parcel_id == "R0002864"

    def test_falls_back_when_parcelnb_missing(self, harv):
        row = _base_row(accountno="R7")
        row.pop("parcelnb")
        rec = harv.map_record(row)
        assert rec.parcel_id == "R7"

    def test_numeric_parcelnb_is_stringified(self, harv):
        rec = harv.map_record(_base_row(parcelnb=12345678901234))
        assert rec.parcel_id == "12345678901234"

    def test_whitespace_parcelnb_is_stripped(self, harv):
        rec = harv.map_record(_base_row(parcelnb="  P-42  "))
        assert rec.parcel_id == "P-42"

    def test_integer_zero_parcelnb_falls_back_to_account(self, harv):
        # `0 or ""` -> "" (falsy int collapses), so a 0 parcelnb is treated blank.
        rec = harv.map_record(_base_row(parcelnb=0, accountno="R5"))
        assert rec.parcel_id == "R5"

    def test_skips_when_both_keys_blank(self, harv):
        assert harv.map_record(_base_row(parcelnb="", accountno="")) is None

    def test_skips_when_both_keys_missing(self, harv):
        row = _base_row()
        row.pop("parcelnb")
        row.pop("accountno")
        assert harv.map_record(row) is None

    def test_skips_when_keys_whitespace_only(self, harv):
        assert harv.map_record(_base_row(parcelnb="   ", accountno="  ")) is None


# --------------------------------------------------------------------------- #
# Address (situs street) is mandatory
# --------------------------------------------------------------------------- #
class TestAddress:
    def test_address_is_stripped(self, harv):
        rec = harv.map_record(_base_row(locationad="  456 ELK AVE  "))
        assert rec.address == "456 ELK AVE"

    def test_skips_when_address_blank(self, harv):
        assert harv.map_record(_base_row(locationad="")) is None

    def test_skips_when_address_whitespace_only(self, harv):
        assert harv.map_record(_base_row(locationad="   ")) is None

    def test_skips_when_address_missing(self, harv):
        row = _base_row()
        row.pop("locationad")
        assert harv.map_record(row) is None

    def test_skips_when_address_none(self, harv):
        assert harv.map_record(_base_row(locationad=None)) is None


# --------------------------------------------------------------------------- #
# Owner (ownername1) is mandatory
# --------------------------------------------------------------------------- #
class TestOwner:
    def test_owner_is_stripped(self, harv):
        rec = harv.map_record(_base_row(ownername1="  JANE DOE  "))
        assert rec.owner_name == "JANE DOE"

    def test_skips_when_owner_blank(self, harv):
        assert harv.map_record(_base_row(ownername1="")) is None

    def test_skips_when_owner_missing(self, harv):
        row = _base_row()
        row.pop("ownername1")
        assert harv.map_record(row) is None

    @pytest.mark.parametrize("name,expected_type", [
        ("JOHN Q PUBLIC", "individual"),
        ("ACME HOLDINGS LLC", "corporate"),
        ("BIGCO PROPERTIES INC", "corporate"),
        ("SMITH FAMILY TRUST", "trust"),
        ("ESTATE OF JANE ROE", "trust"),
    ])
    def test_owner_type_classified(self, harv, name, expected_type):
        rec = harv.map_record(_base_row(ownername1=name))
        assert rec.owner_type == expected_type
        # map_record must defer to the shared classifier, not reinvent it.
        assert rec.owner_type == classify_owner(name)


# --------------------------------------------------------------------------- #
# Estimated value: actualvalu primary, assessed/0.095 gross-up fallback
# --------------------------------------------------------------------------- #
class TestEstimatedValue:
    @pytest.mark.parametrize("actualvalu,assessedva,expected", [
        (250000, 23750, 250000.0),          # actual wins, assessed ignored
        ("$250,000", None, 250000.0),       # to_float strips $ and commas
        (0, 9500, 100000.0),                # gross-up: 9500 / 0.095
        ("0", 9500, 100000.0),              # string "0" actual still triggers gross-up
        (None, 9500, 100000.0),             # missing actual -> gross-up
        (0, None, 0.0),                     # no actual, no assessed -> 0
        (0, 0, 0.0),                        # assessed 0 (falsy) -> 0
        (0, "0", 0.0),                      # assessed "0" (truthy) -> to_float -> 0
    ])
    def test_value_resolution(self, harv, actualvalu, assessedva, expected):
        rec = harv.map_record(_base_row(actualvalu=actualvalu, assessedva=assessedva))
        assert rec.estimated_value == pytest.approx(expected)

    def test_actual_value_matches_to_float(self, harv):
        rec = harv.map_record(_base_row(actualvalu="312,500"))
        assert rec.estimated_value == to_float("312,500")

    def test_missing_actual_and_assessed_is_zero(self, harv):
        row = _base_row()
        row.pop("actualvalu")
        row.pop("assessedva")
        rec = harv.map_record(row)
        assert rec.estimated_value == 0.0


# --------------------------------------------------------------------------- #
# Absentee signal + distress flag (driven by owner mailing state vs WY)
# --------------------------------------------------------------------------- #
class TestAbsentee:
    @pytest.mark.parametrize("mailstate,expected", [
        ("WY", False),
        ("wy", False),          # case-folded before compare
        (" wy ", False),        # stripped before compare
        ("CO", True),
        ("co", True),
        (" co ", True),
        ("", False),            # no mailing state -> cannot assert absentee
        ("   ", False),
        (None, False),
    ])
    def test_absentee_flag(self, harv, mailstate, expected):
        rec = harv.map_record(_base_row(mailstate=mailstate))
        assert rec.is_absentee_owner is expected

    def test_absentee_adds_distress_flag(self, harv):
        rec = harv.map_record(_base_row(mailstate="TX"))
        assert rec.distress_flags == ["absentee_owner"]

    def test_resident_has_no_distress_flag(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY"))
        assert rec.distress_flags == []

    def test_missing_mailstate_not_absentee(self, harv):
        row = _base_row()
        row.pop("mailstate")
        rec = harv.map_record(row)
        assert rec.is_absentee_owner is False
        assert rec.distress_flags == []


# --------------------------------------------------------------------------- #
# Situs zip proxy: mailing zip only stands in for a resident (non-absentee) owner
# --------------------------------------------------------------------------- #
class TestSitusZip:
    def test_resident_zip_uses_mailing_zip(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY", mailzipcod="82001"))
        assert rec.zip_code == "82001"

    def test_resident_zip_is_stripped(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY", mailzipcod="  82001  "))
        assert rec.zip_code == "82001"

    def test_resident_zip_truncated_to_ten_chars(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY", mailzipcod="82001-123456"))
        assert rec.zip_code == "82001-1234"
        assert len(rec.zip_code) == 10

    def test_numeric_resident_zip_stringified(self, harv):
        rec = harv.map_record(_base_row(mailstate="WY", mailzipcod=82001))
        assert rec.zip_code == "82001"

    def test_resident_missing_zip_is_blank(self, harv):
        row = _base_row(mailstate="WY")
        row.pop("mailzipcod")
        rec = harv.map_record(row)
        assert rec.zip_code == ""

    def test_absentee_zip_is_blank_even_with_mailing_zip(self, harv):
        # An absentee owner's mailing zip is the *owner's*, not the property's.
        rec = harv.map_record(_base_row(mailstate="CO", mailzipcod="80202"))
        assert rec.zip_code == ""


# --------------------------------------------------------------------------- #
# Full mapped record for an out-of-state (absentee) owner
# --------------------------------------------------------------------------- #
class TestAbsenteeRecordShape:
    def test_absentee_record_full_shape(self, harv):
        rec = harv.map_record(_base_row(
            parcelnb="0123456789",
            ownername1="OUT OF STATE INVESTOR LLC",
            mailstate="FL",
            mailzipcod="33101",
            actualvalu=180000,
            locationad="12 RANGE RD",
        ))
        assert rec.parcel_id == "0123456789"
        assert rec.address == "12 RANGE RD"
        assert rec.state == "WY"
        assert rec.city == ""
        assert rec.zip_code == ""                       # absentee -> no situs proxy
        assert rec.owner_name == "OUT OF STATE INVESTOR LLC"
        assert rec.owner_type == "corporate"
        assert rec.estimated_value == 180000.0
        assert rec.is_absentee_owner is True
        assert rec.distress_flags == ["absentee_owner"]
        assert rec.equity_percent == 0.0
        assert rec.last_sale_date is None


# --------------------------------------------------------------------------- #
# End-to-end harvest() accounting (stubbed fetch, no network, no DB)
# --------------------------------------------------------------------------- #
class TestHarvestIntegration:
    def test_harvest_counts_parsed_and_skipped(self, harv):
        rows = [
            _base_row(),                                  # valid
            _base_row(parcelnb="P2"),                     # valid
            _base_row(parcelnb="", accountno=""),         # skip: no parcel key
            _base_row(locationad=""),                     # skip: no situs
            _base_row(ownername1=""),                     # skip: no owner
        ]

        async def fake_fetch(max_records=None):
            return rows

        harv.fetch_raw = fake_fetch
        metrics = asyncio.run(harv.harvest(persist=False))

        assert metrics["state"] == "WY"
        assert metrics["fetched"] == 5
        assert metrics["parsed"] == 2
        assert metrics["skipped"] == 3
        assert metrics["inserted"] == 0   # persist=False never touches the DB

    def test_harvest_exposes_mapped_records(self, harv):
        rows = [_base_row(parcelnb="A1"), _base_row(parcelnb="A2")]

        async def fake_fetch(max_records=None):
            return rows

        harv.fetch_raw = fake_fetch
        asyncio.run(harv.harvest(persist=False))

        assert [r.parcel_id for r in harv._records] == ["A1", "A2"]
        assert all(isinstance(r, PropertyRecord) for r in harv._records)
        assert all(r.state == "WY" for r in harv._records)

    def test_harvest_empty_feed_is_clean(self, harv):
        async def fake_fetch(max_records=None):
            return []

        harv.fetch_raw = fake_fetch
        metrics = asyncio.run(harv.harvest(persist=False))

        assert metrics["fetched"] == 0
        assert metrics["parsed"] == 0
        assert metrics["skipped"] == 0
        assert harv._records == []
