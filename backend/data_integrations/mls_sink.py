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
        photos, features, last_updated,
        -- Promoted out of the features JSONB by 0110. Provenance that lives
        -- only inside a JSON blob cannot be indexed, filtered or enforced,
        -- and these decide whether a row may be shown as licensed inventory.
        license_classification, source_modified_at, source_status
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
        $21,$22,$23,$24,$25,$26,$27::jsonb,now(),
        $28, $29, $30
    )
    ON CONFLICT (mls_id, mls_number) DO UPDATE SET
        address=EXCLUDED.address, city=EXCLUDED.city, state_code=EXCLUDED.state_code,
        zip_code=EXCLUDED.zip_code, county=EXCLUDED.county,
        latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
        list_price=EXCLUDED.list_price, orig_list_price=EXCLUDED.orig_list_price,
        status=EXCLUDED.status, property_type=EXCLUDED.property_type,
        license_classification=EXCLUDED.license_classification,
        source_modified_at=EXCLUDED.source_modified_at,
        source_status=EXCLUDED.source_status,
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


async def upsert_mls_records(
    conn: Any,
    records: list[dict[str, Any]],
    *,
    license_classification: str = "developer_listing_dataset",
) -> int:
    """Write already-normalized, already-validated records. Returns rows written.

    `license_classification` defaults to developer data because that is the
    safe answer: a caller that has not established a licence must not be able
    to write rows that downstream code will serve as licensed inventory.
    """
    from datetime import datetime as _dt

    upserted = 0
    for rec in records:
        feats = rec.get("features") or {}
        raw_modified = feats.get("source_modified_at") if isinstance(feats, dict) else None
        modified = None
        if isinstance(raw_modified, _dt):
            modified = raw_modified
        elif isinstance(raw_modified, str) and raw_modified:
            try:
                modified = _dt.fromisoformat(raw_modified.replace("Z", "+00:00"))
            except ValueError:
                modified = None
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
            license_classification,
            modified,
            (feats.get("source_status") if isinstance(feats, dict) else None),
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
    provider: str = "",
    dataset: str = "",
    succeeded: bool = True,
    backfill_complete: Optional[bool] = None,
    error: str = "",
    error_class: Optional[str] = None,
) -> None:
    """Upsert the one status row a feed maintains.

    This is also where a feed's LICENCE and HEALTH are written, because this is
    the only place that knows a sync actually happened. Migration 0110 added
    those columns and nothing populated them, so every feed sat at the
    `developer_listing_dataset` default and `backfill_complete = false`
    forever: entitlement never engaged, and a correctly licensed feed could
    never reach READY without someone editing rows by hand.

    Classification comes from mls_licensing, which fails closed — a dataset is
    developer data unless an operator declared it licensed and named the
    agreement. It is recomputed on every sync rather than written once, so
    revoking the declaration downgrades the feed on the next run instead of
    leaving a stale "licensed" stamp behind.
    """
    from mls_licensing import classify_from_env

    licence = classify_from_env(dataset or mls_id)

    await conn.execute(
        """
        INSERT INTO mls_sync_status
            (mls_id, mls_name, feed_type, last_sync_at, listings_synced,
             sync_lag_minutes, notes, updated_at,
             provider, dataset, license_classification, license_reason,
             agreement_ref, last_attempt_at, last_success_at,
             last_error, last_error_class, consecutive_failures,
             backfill_complete)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now(),
                $8, $9, $10, $11, $12, now(),
                CASE WHEN $13 THEN now() ELSE NULL END,
                NULLIF($14, ''), $15,
                CASE WHEN $13 THEN 0 ELSE 1 END,
                COALESCE($16, false))
        ON CONFLICT (mls_id) DO UPDATE SET
            mls_name = EXCLUDED.mls_name,
            feed_type = EXCLUDED.feed_type,
            last_sync_at = CASE WHEN $13 THEN EXCLUDED.last_sync_at
                                ELSE mls_sync_status.last_sync_at END,
            listings_synced = mls_sync_status.listings_synced + EXCLUDED.listings_synced,
            sync_lag_minutes = EXCLUDED.sync_lag_minutes,
            notes = EXCLUDED.notes,
            provider = EXCLUDED.provider,
            dataset = EXCLUDED.dataset,
            license_classification = EXCLUDED.license_classification,
            license_reason = EXCLUDED.license_reason,
            agreement_ref = EXCLUDED.agreement_ref,
            last_attempt_at = now(),
            -- Only a success moves last_success_at. A failed run must not make
            -- the feed look fresh, which is the whole basis of staleness.
            last_success_at = CASE WHEN $13 THEN now()
                                   ELSE mls_sync_status.last_success_at END,
            last_error = CASE WHEN $13 THEN NULL ELSE NULLIF($14, '') END,
            last_error_class = CASE WHEN $13 THEN NULL ELSE $15 END,
            consecutive_failures = CASE WHEN $13 THEN 0
                                        ELSE mls_sync_status.consecutive_failures + 1 END,
            -- Backfill completion only ever moves forward: a later delta sync
            -- passing NULL must not un-complete a finished initial walk.
            backfill_complete = COALESCE($16, mls_sync_status.backfill_complete),
            updated_at = now()
        """,
        mls_id, mls_name, feed_type, last_sync_at, listings_synced,
        sync_lag_minutes,
        json.dumps(notes or {}, separators=(",", ":")),
        provider or feed_type, dataset or "",
        licence.classification, licence.reason, licence.agreement_ref,
        bool(succeeded), error or "", error_class,
        backfill_complete,
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
    **status_kwargs: Any,
) -> int:
    """Convenience wrapper: write the batch, then the status row it produced.

    `status_kwargs` forwards the licence/health fields (provider, dataset,
    succeeded, backfill_complete, error, error_class) so a caller does not have
    to choose between this wrapper and reporting its health.
    """
    from mls_licensing import classify_from_env
    licence = classify_from_env(status_kwargs.get("dataset") or mls_id)
    upserted = await upsert_mls_records(
        conn, records, license_classification=licence.classification)
    await record_sync_status(
        conn,
        mls_id=mls_id,
        mls_name=mls_name,
        feed_type=feed_type,
        last_sync_at=last_sync_at,
        **status_kwargs,
        listings_synced=upserted,
        sync_lag_minutes=sync_lag_minutes,
        notes=notes,
    )
    return upserted
