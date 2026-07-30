-- Fast deterministic matching between licensed MLS listings and public leads.
-- These indexes do not merge source facts; the API exposes an explicit overlay.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_oml_match_address_zip
    ON oracle_mls_listings (
        state_code,
        zip_code,
        regexp_replace(lower(address), '[^a-z0-9]', '', 'g')
    )
    WHERE address <> '' AND zip_code <> '';

CREATE INDEX IF NOT EXISTS idx_oml_match_parcel
    ON oracle_mls_listings (
        state_code,
        regexp_replace(lower(COALESCE(features->>'parcel_number','')),
                       '[^a-z0-9]', '', 'g')
    )
    WHERE COALESCE(features->>'parcel_number','') <> '';

CREATE INDEX IF NOT EXISTS idx_oml_source_modified
    ON oracle_mls_listings (
        mls_id,
        ((features->>'source_modified_at')::text)
    )
    WHERE features ? 'source_modified_at';

COMMIT;
