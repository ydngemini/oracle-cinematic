"""
data_integrations/mls_sink.py — the one write path every MLS/listings provider
shares, regardless of what wire protocol got the data here.

RESO/OData (listings_feed.py) and Bridge Interactive's own JSON REST protocol
(bridge_listings_feed.py) disagree about almost everything: request shape,
pagination, response envelope, even which timestamp field marks "changed
since". They agree about nothing except the destination — both must land in
``oracle_mls_listings`` through the identical validation and the identical
upsert, and both must report themselves through the identical
``mls_sync_status`` row shape. That agreement is this module.

A provider module owns: its request/pagination/envelope, its own
`normalize()` into the canonical record dict (see `_reject_reason` below for
the exact keys expected), and its own cursor computation. It calls
`upsert_mls_records()` with the finished records and `record_sync_status()`
with the outcome. Nothing here knows what an OData `$filter` or a Bridge
`access_token` query param is.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_UPSERT = """
    INSERT INTO oracle_mls_listings (
        mls_id, mls_number, address, city, state_code, zip_code, county,
        latitude, longitude, list_price, orig_list_price, status, property_type,
        beds, baths_full, baths_half, sqft, lot_sqft, year_built, hoa_monthly,
        days_on_market, list_date, close_date, close_price, description,
        photos, features, last_updated
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
        $21,$22,$23,$24,$25,$26,$27::jsonb,now()
    )
    ON CONFLICT (mls_id, mls_number) DO UPDATE SET
        address=EXCLUDED.address, city=EXCLUDED.city, state_code=EXCLUDED.state_code,
        zip_code=EXCLUDED.zip_code, county=EXCLUDED.county,
        latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
        list_price=EXCLUDED.list_price, orig_list_price=EXCLUDED.orig_list_price,
        status=EXCLUDED.status, property_type=EXCLUDED.property_type,
        beds=EXCLUDED.beds, baths_full=EXCLUDED.baths_full, baths_half=EXCLUDED.baths_half,
        sqft=EXCLUDED.sqft, lot_sqft=EXCLUDED.lot_sqft, year_built=EXCLUDED.year_built,
        hoa_monthly=EXCLUDED.hoa_monthly, days_on_market=EXCLUDED.days_on_market,
        list_date=EXCLUDED.list_date, close_date=EXCLUDED.close_date,
        close_price=EXCLUDED.close_price, description=EXCLUDED.description,
        photos=EXCLUDED.photos, features=EXCLUDED.features,
        last_updated=now()
"""


def reject_reason(rec: dict[str, Any]) -> Optional[str]:
    """Provider-agnostic sanity check. Returns None when the record is fit to write."""
    if not rec.get("mls_number"):
        return "missing_listing_key"
    state = str(rec.get("state_code") or "")
    if len(state) != 2 or not state.isalpha():
        return "invalid_state"
    latitude, longitude = rec.get("latitude"), rec.get("longitude")
    if latitude is not None and not -90 <= latitude <= 90:
        return "invalid_latitude"
    if longitude is not None and not -180 <= longitude <= 180:
        return "invalid_longitude"
    return None


async def upsert_mls_records(conn: Any, records: list[dict[str, Any]]) -> int:
    """Write already-normalized, already-validated records. Returns rows written."""
    upserted = 0
    for rec in records:
        await conn.execute(
            _UPSERT,
            rec["mls_id"], rec["mls_number"], rec["address"], rec["city"],
            rec["state_code"], rec["zip_code"], rec["county"], rec["latitude"],
            rec["longitude"], rec["list_price"], rec["orig_list_price"], rec["status"],
            rec["property_type"], rec["beds"], rec["baths_full"], rec["baths_half"],
            rec["sqft"], rec["lot_sqft"], rec["year_built"], rec["hoa_monthly"],
            rec["days_on_market"], rec["list_date"], rec["close_date"],
            rec["close_price"], rec["description"], rec["photos"],
            json.dumps(rec["features"], separators=(",", ":")),
        )
        upserted += 1
    return upserted


async def record_sync_status(
    conn: Any,
    *,
    mls_id: str,
    mls_name: str,
    feed_type: str,
    last_sync_at,
    listings_synced: int,
    sync_lag_minutes: int = 0,
    notes: Optional[dict] = None,
) -> None:
    """Upsert the one status row a feed maintains. `feed_type` is the provider's
    own label ('RESO_Web_API', 'Bridge_API_v2', ...) so the dashboard can tell
    providers apart without a second table."""
    await conn.execute(
        """
        INSERT INTO mls_sync_status
            (mls_id, mls_name, feed_type, last_sync_at, listings_synced,
             sync_lag_minutes, notes, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now())
        ON CONFLICT (mls_id) DO UPDATE SET
            mls_name = EXCLUDED.mls_name,
            feed_type = EXCLUDED.feed_type,
            last_sync_at = EXCLUDED.last_sync_at,
            listings_synced = mls_sync_status.listings_synced + EXCLUDED.listings_synced,
            sync_lag_minutes = EXCLUDED.sync_lag_minutes,
            notes = EXCLUDED.notes,
            updated_at = now()
        """,
        mls_id, mls_name, feed_type, last_sync_at, listings_synced,
        sync_lag_minutes,
        json.dumps(notes or {}, separators=(",", ":")),
    )


async def upsert_mls_records_and_status(
    conn: Any,
    *,
    records: list[dict[str, Any]],
    mls_id: str,
    mls_name: str,
    feed_type: str,
    last_sync_at,
    sync_lag_minutes: int = 0,
    notes: Optional[dict] = None,
) -> int:
    """Convenience wrapper: write the batch, then the status row it produced."""
    upserted = await upsert_mls_records(conn, records)
    await record_sync_status(
        conn,
        mls_id=mls_id,
        mls_name=mls_name,
        feed_type=feed_type,
        last_sync_at=last_sync_at,
        listings_synced=upserted,
        sync_lag_minutes=sync_lag_minutes,
        notes=notes,
    )
    return upserted
