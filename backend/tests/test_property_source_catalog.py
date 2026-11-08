"""The property/market source inventory cannot drift from the code (T11.02, L11-04)."""

from __future__ import annotations

import property_source_catalog as psc

CAT = psc.catalog()
IDS = {s.id for s in CAT}


def test_every_parcel_harvester_is_catalogued():
    from harvesters.firehose import REGISTRY

    assert {f"state_parcels_{k.lower()}" for k in REGISTRY} <= IDS
    assert len(REGISTRY) >= 50


def test_every_market_dataset_and_valuation_provider_is_catalogued():
    from market_research_agent import _default_specs
    from provider_health import PROVIDERS

    assert {f"market_research_{s.key}" for s in _default_specs()} <= IDS
    assert {f"{p}_avm" for p in PROVIDERS if not p.startswith("platform_")} <= IDS


def test_entries_are_well_formed_and_unique():
    assert len(IDS) == len(CAT)
    for s in CAT:
        assert s.kind in psc.KINDS, s.id
        assert s.licence_status in psc.LICENCE_STATUSES, s.id
        assert s.fetch and s.freshness and s.writes, s.id
        if s.licence_status == "stated_in_repo":
            assert s.licence and s.licence_ref, s.id   # a stated licence cites where


def test_no_licence_is_invented():
    """A harvester on the base default is reported unverified, and an
    attribution string never counts as a licence."""
    from harvesters.base import BaseHarvester

    for s in CAT:
        if s.licence == BaseHarvester.LICENSE_NAME or s.licence.startswith("municipal-open-data"):
            assert s.licence_status == "default_unverified", s.id
        if s.id.startswith("market_research_"):
            assert s.licence_status == "owner_to_supply", s.id


def test_no_mls_source_is_catalogued():
    text = " ".join(f"{s.id} {s.fetch}" for s in CAT).lower()
    for word in ("mls", "idx", "reso", "bridge_listings"):
        assert word not in text


def test_the_committed_doc_is_current():
    assert psc.DOC_PATH.read_text() == psc.to_markdown(CAT), "run: python property_source_catalog.py --write-doc"
