"""Deterministic MLS overlays for public-record leads.

An MLS listing is not merged into or allowed to overwrite assessor facts.  This
correlated projection returns a separate overlay only when the records share:

* state + normalized parcel number + normalized county/address evidence; or
* state + exact normalized street address + exact ZIP code.

Coordinates alone are never sufficient. They are useful for maps, but are not a
safe identity key for condos, subdivisions, or neighboring parcels.
"""

from __future__ import annotations

import json
from typing import Any

from mls_health import visible_feed_predicate


# The WHERE clause is written to match idx_oml_match_parcel and
# idx_oml_match_address_zip EXACTLY — the COALESCE inside the parcel expression
# and the bare `address <> ''` / `zip_code <> ''` partial-index predicates.
# `COALESCE(m.address,'') <> ''` means the same thing, but the planner cannot
# prove the partial predicate from it, and a parcel expression without the
# COALESCE is a different expression: with either mismatch every lead on a
# pipeline page seq-scanned all listings in its state — 2.7 s per lead, 140 s
# per 51-lead page on the ACTRIS (TX) feed, on EVERY WebSocket connect
# (Mission 8). `state_code` is char(2) and leads.state is text: comparing them
# unadorned casts the COLUMN, which drops the index's leading column — so the
# lead side is cast to bpchar instead. tests/test_mls_overlay_indexable.py pins
# the correspondence. 140 s → 5 ms per page, measured.
MLS_OVERLAY_SELECT = r"""
    (
        SELECT jsonb_build_object(
            'listing_id', m.id::text,
            'mls_id', m.mls_id,
            'mls_number', m.mls_number,
            'status', m.status,
            'list_price', m.list_price,
            'original_list_price', m.orig_list_price,
            'days_on_market', m.days_on_market,
            'list_date', m.list_date,
            'source_modified_at', m.features->>'source_modified_at',
            'last_ingested_at', m.last_updated,
            'match_method',
                CASE
                    WHEN COALESCE(m.features->>'parcel_number','') <> ''
                         AND regexp_replace(lower(m.features->>'parcel_number'),
                                            '[^a-z0-9]', '', 'g')
                             = regexp_replace(lower(leads.parcel_id),
                                              '[^a-z0-9]', '', 'g')
                    THEN 'parcel_and_location'
                    ELSE 'normalized_address_and_zip'
                END,
            'match_confidence',
                CASE
                    WHEN COALESCE(m.features->>'parcel_number','') <> ''
                         AND regexp_replace(lower(m.features->>'parcel_number'),
                                            '[^a-z0-9]', '', 'g')
                             = regexp_replace(lower(leads.parcel_id),
                                              '[^a-z0-9]', '', 'g')
                    THEN 1.0
                    ELSE 0.98
                END,
            'source_kind', COALESCE(m.features->>'source_kind', 'listing_provider'),
            'provenance', COALESCE(m.features->'provenance', '{}'::jsonb),
            'verification_required', m.last_updated < now() - interval '24 hours'
        )
          FROM oracle_mls_listings AS m
         WHERE m.mls_id <> 'rentcast'
           AND __VISIBLE_FEED__
           AND m.state_code = leads.state::bpchar
           AND (
                (
                    COALESCE(m.features->>'parcel_number','') <> ''
                    AND regexp_replace(lower(COALESCE(m.features->>'parcel_number','')),
                                       '[^a-z0-9]', '', 'g')
                        = regexp_replace(lower(leads.parcel_id),
                                         '[^a-z0-9]', '', 'g')
                    AND (
                        (
                            COALESCE(m.county,'') <> ''
                            AND COALESCE(leads.payload->>'county','') <> ''
                            AND regexp_replace(lower(m.county), '[^a-z0-9]', '', 'g')
                                = regexp_replace(lower(leads.payload->>'county'),
                                                 '[^a-z0-9]', '', 'g')
                        )
                        OR (
                            m.address <> ''
                            AND COALESCE(leads.payload->>'address','') <> ''
                            AND regexp_replace(lower(m.address), '[^a-z0-9]', '', 'g')
                                = regexp_replace(lower(leads.payload->>'address'),
                                                 '[^a-z0-9]', '', 'g')
                            AND m.zip_code = COALESCE(leads.payload->>'zip_code','')
                        )
                    )
                )
                OR (
                    m.address <> ''
                    AND COALESCE(leads.payload->>'address','') <> ''
                    AND m.zip_code <> ''
                    AND regexp_replace(lower(m.address), '[^a-z0-9]', '', 'g')
                        = regexp_replace(lower(leads.payload->>'address'),
                                         '[^a-z0-9]', '', 'g')
                    AND m.zip_code = COALESCE(leads.payload->>'zip_code','')
                )
           )
         ORDER BY
            (
                COALESCE(m.features->>'parcel_number','') <> ''
                AND regexp_replace(lower(m.features->>'parcel_number'),
                                   '[^a-z0-9]', '', 'g')
                    = regexp_replace(lower(leads.parcel_id),
                                     '[^a-z0-9]', '', 'g')
            ) DESC,
            m.last_updated DESC,
            m.id ASC
         LIMIT 1
    ) AS mls_overlay
"""

# Bound at import, not left for callers to remember. This is a module constant
# interpolated into other modules' queries (lead_dossier, server), so there is
# no call site where a forgotten narrowing would be visible — an overlay that
# quietly carried another brokerage's licensed listing onto a lead would look
# exactly like a correct one.
MLS_OVERLAY_SELECT = MLS_OVERLAY_SELECT.replace(
    "__VISIBLE_FEED__", visible_feed_predicate("m")
)
assert "__VISIBLE_FEED__" not in MLS_OVERLAY_SELECT


def clean_mls_overlay(value: Any) -> dict[str, Any] | None:
    """Normalize asyncpg/json-string results without manufacturing empty data."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) and value.get("listing_id") else None
