"""Direct-source-only MLS browse contracts."""

from __future__ import annotations

import inspect

import mls_portal
from mls_enrichment import MLS_OVERLAY_SELECT


def test_mls_portal_has_no_third_party_listing_fetch_path():
    source = inspect.getsource(mls_portal)
    assert "_fetch_rentcast_listings" not in source
    assert "/listings/sale" not in source
    assert "aiohttp" not in source


def test_third_party_cached_rows_are_quarantined_from_direct_mls_surfaces():
    assert "rentcast" in mls_portal.THIRD_PARTY_LISTING_SOURCE_IDS
    assert "m.mls_id <> 'rentcast'" in MLS_OVERLAY_SELECT
