"""`_listing_json` must emit the oracle_mls_listings column names verbatim.

The browse UI (`oracle-app/src/components/MlsSearch.jsx`) reads `list_price`,
`state_code`, `zip_code` and `orig_list_price` straight off each result row —
those are the actual table columns. An earlier version of this serializer
renamed them to `price` / `state` / `zip` / `orig_price`, which silently blanked
the price and the location line on every listing card while leaving beds/baths
populated, so it looked half-working rather than broken.
"""

from __future__ import annotations

import mls_portal


def _row(**over):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "mls_number": "DE-4821",
        "address": "12 Rodney Sq",
        "city": "Wilmington",
        "state_code": "DE",
        "zip_code": "19801",
        "county": "New Castle",
        "list_price": 425000,
        "orig_list_price": 449000,
        "beds": 3,
        "sqft": 1840,
    }
    base.update(over)
    return base


def test_listing_json_keeps_the_table_column_names():
    out = mls_portal._listing_json(_row())

    assert out["list_price"] == 425000
    assert out["orig_list_price"] == 449000
    assert out["state_code"] == "DE"
    assert out["zip_code"] == "19801"

    # The renamed keys must be gone — a stray alias would let a stale caller
    # keep working and mask a future regression.
    for gone in ("price", "orig_price", "state", "zip"):
        assert gone not in out


def test_listing_json_tolerates_missing_price_and_location():
    out = mls_portal._listing_json(_row(list_price=None, state_code=None, zip_code=None))

    assert out["list_price"] is None
    assert out["state_code"] == ""
    assert out["zip_code"] == ""
