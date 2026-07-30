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
           AND m.state_code = leads.state
           AND (
                (
                    COALESCE(m.features->>'parcel_number','') <> ''
                    AND regexp_replace(lower(m.features->>'parcel_number'),
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
                            COALESCE(m.address,'') <> ''
                            AND COALESCE(leads.payload->>'address','') <> ''
                            AND regexp_replace(lower(m.address), '[^a-z0-9]', '', 'g')
                                = regexp_replace(lower(leads.payload->>'address'),
                                                 '[^a-z0-9]', '', 'g')
                            AND m.zip_code = COALESCE(leads.payload->>'zip_code','')
                        )
                    )
                )
                OR (
                    COALESCE(m.address,'') <> ''
                    AND COALESCE(leads.payload->>'address','') <> ''
                    AND COALESCE(m.zip_code,'') <> ''
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


def clean_mls_overlay(value: Any) -> dict[str, Any] | None:
    """Normalize asyncpg/json-string results without manufacturing empty data."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) and value.get("listing_id") else None
