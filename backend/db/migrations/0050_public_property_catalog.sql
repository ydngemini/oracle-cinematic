-- ==========================================================================
-- 0050_public_property_catalog.sql
--
-- Public assessor/parcel facts are shared source data, not CRM leads.  Keep a
-- deliberately allow-listed catalog outside the tenant-private `leads` table
-- so every authenticated CRM user can find harvested records without exposing
-- another tenant's contacts, notes, underwriting, motivation, or deal state.
-- ==========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION public_record_date_or_null(value text)
RETURNS date
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
    RETURN value::date;
EXCEPTION
    WHEN datetime_field_overflow OR invalid_datetime_format THEN
        RETURN NULL;
END;
$$;

CREATE TABLE IF NOT EXISTS public_property_records (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key            text NOT NULL,
    source_record_id      text NOT NULL,
    parcel_id             text NOT NULL,
    state                 text NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    county                text,
    city                  text,
    zip_code              text,
    address               text,
    owner_name            text,
    owner_type            text,
    public_record_value   numeric,
    reported_record_date  date,
    zoning_district       text,
    land_use              text,
    lot_area_sqft         numeric,
    building_area_sqft    numeric,
    latitude              double precision,
    longitude             double precision,
    source_name           text NOT NULL,
    coverage_scope        text NOT NULL,
    detail_level          text NOT NULL DEFAULT 'limited'
                                  CHECK (detail_level IN ('limited','standard','comprehensive')),
    observed_fields       text[] NOT NULL DEFAULT '{}',
    verification_required boolean NOT NULL DEFAULT true,
    record_refreshed_at   timestamptz NOT NULL,
    dataset_version       text,
    source_metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_document       text GENERATED ALWAYS AS (
        lower(
            COALESCE(address, '') || ' ' ||
            COALESCE(city, '') || ' ' ||
            COALESCE(county, '') || ' ' ||
            COALESCE(state, '') || ' ' ||
            COALESCE(zip_code, '') || ' ' ||
            COALESCE(owner_name, '') || ' ' ||
            COALESCE(parcel_id, '')
        )
    ) STORED,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_public_property_source_record
        UNIQUE (source_key, state, source_record_id)
);

DROP TRIGGER IF EXISTS trg_public_property_records_updated ON public_property_records;
CREATE TRIGGER trg_public_property_records_updated
BEFORE UPDATE ON public_property_records
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Seed the catalog from historical harvester leads.  The source envelope is
-- projected through an explicit allow-list; no tenant id, contact data,
-- encrypted payload, underwriting, motivation, or CRM status crosses over.
-- DISTINCT ON collapses copies of the same source record that were previously
-- written into more than one tenant.
SELECT set_config('app.current_role', 'platform_admin', true);

INSERT INTO public_property_records (
    source_key,
    source_record_id,
    parcel_id,
    state,
    county,
    city,
    zip_code,
    address,
    owner_name,
    owner_type,
    public_record_value,
    reported_record_date,
    zoning_district,
    land_use,
    lot_area_sqft,
    building_area_sqft,
    latitude,
    longitude,
    source_name,
    coverage_scope,
    detail_level,
    observed_fields,
    verification_required,
    record_refreshed_at,
    dataset_version,
    source_metadata
)
SELECT
    source_key,
    parcel_id,
    parcel_id,
    state,
    NULLIF(payload->>'county', ''),
    NULLIF(payload->>'city', ''),
    NULLIF(payload->>'zip_code', ''),
    NULLIF(payload->>'address', ''),
    NULLIF(payload->>'owner_name', ''),
    NULLIF(payload->>'owner_type', ''),
    CASE WHEN payload->>'estimated_value' ~ '^[0-9]+(?:\.[0-9]+)?$'
         THEN (payload->>'estimated_value')::numeric END,
    public_record_date_or_null(payload->>'last_sale_date'),
    NULLIF(payload->>'zoning_district', ''),
    NULLIF(payload->>'land_use', ''),
    CASE WHEN payload->>'lot_area_sqft' ~ '^[0-9]+(?:\.[0-9]+)?$'
         THEN (payload->>'lot_area_sqft')::numeric END,
    CASE WHEN payload->>'building_area_sqft' ~ '^[0-9]+(?:\.[0-9]+)?$'
         THEN (payload->>'building_area_sqft')::numeric END,
    CASE WHEN payload->>'latitude' ~ '^-?[0-9]+(?:\.[0-9]+)?$'
         THEN (payload->>'latitude')::double precision END,
    CASE WHEN payload->>'longitude' ~ '^-?[0-9]+(?:\.[0-9]+)?$'
         THEN (payload->>'longitude')::double precision END,
    COALESCE(NULLIF(payload->'provenance'->>'source_name', ''), source_key),
    COALESCE(NULLIF(payload->'provenance'->>'coverage_scope', ''), 'source scope not declared'),
    CASE
        WHEN payload->'data_quality'->>'detail_level' IN ('limited','standard','comprehensive')
        THEN payload->'data_quality'->>'detail_level'
        ELSE 'limited'
    END,
    ARRAY(
        SELECT jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(payload->'data_quality'->'observed_fields') = 'array'
                THEN payload->'data_quality'->'observed_fields'
                ELSE '[]'::jsonb
            END
        )
    ),
    true,
    updated_at,
    NULLIF(payload->>'dataset_version', ''),
    CASE
        WHEN jsonb_typeof(payload->'source_metadata') = 'object'
        THEN payload->'source_metadata'
        ELSE '{}'::jsonb
    END
FROM (
    SELECT DISTINCT ON (
        COALESCE(
            NULLIF(payload->'provenance'->>'source_key', ''),
            NULLIF(underwriting->>'source', ''),
            'firehose:' || state
        ),
        state,
        parcel_id
    )
        parcel_id,
        state,
        payload,
        updated_at,
        COALESCE(
            NULLIF(payload->'provenance'->>'source_key', ''),
            NULLIF(underwriting->>'source', ''),
            'firehose:' || state
        ) AS source_key
    FROM leads
    WHERE
        payload->'provenance'->>'data_classification' = 'public_property_record'
        OR underwriting->>'source' LIKE 'firehose:%'
        OR underwriting->>'source' = 'md_sdat'
    ORDER BY
        COALESCE(
            NULLIF(payload->'provenance'->>'source_key', ''),
            NULLIF(underwriting->>'source', ''),
            'firehose:' || state
        ),
        state,
        parcel_id,
        updated_at DESC,
        id DESC
) harvested
ON CONFLICT (source_key, state, source_record_id) DO UPDATE SET
    parcel_id = EXCLUDED.parcel_id,
    county = EXCLUDED.county,
    city = EXCLUDED.city,
    zip_code = EXCLUDED.zip_code,
    address = EXCLUDED.address,
    owner_name = EXCLUDED.owner_name,
    owner_type = EXCLUDED.owner_type,
    public_record_value = EXCLUDED.public_record_value,
    reported_record_date = EXCLUDED.reported_record_date,
    zoning_district = EXCLUDED.zoning_district,
    land_use = EXCLUDED.land_use,
    lot_area_sqft = EXCLUDED.lot_area_sqft,
    building_area_sqft = EXCLUDED.building_area_sqft,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    source_name = EXCLUDED.source_name,
    coverage_scope = EXCLUDED.coverage_scope,
    detail_level = EXCLUDED.detail_level,
    observed_fields = EXCLUDED.observed_fields,
    verification_required = true,
    record_refreshed_at = EXCLUDED.record_refreshed_at,
    dataset_version = EXCLUDED.dataset_version,
    source_metadata = EXCLUDED.source_metadata;

-- Build indexes after the historical bulk load. PostgreSQL can sort/build each
-- structure once instead of maintaining five indexes row-by-row during seed.
CREATE INDEX IF NOT EXISTS idx_public_property_state_recent
    ON public_property_records (state, record_refreshed_at DESC, id ASC);
CREATE INDEX IF NOT EXISTS idx_public_property_recent
    ON public_property_records (record_refreshed_at DESC, id ASC);
CREATE INDEX IF NOT EXISTS idx_public_property_parcel
    ON public_property_records (
        state,
        regexp_replace(lower(parcel_id), '[^a-z0-9]', '', 'g')
    );
CREATE INDEX IF NOT EXISTS idx_public_property_address
    ON public_property_records (
        state,
        regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9]', '', 'g')
    )
    WHERE address IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_public_property_search_trgm
    ON public_property_records USING gin (search_document gin_trgm_ops);

ALTER TABLE public_property_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public_property_records FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS public_property_records_read ON public_property_records;
CREATE POLICY public_property_records_read ON public_property_records
    FOR SELECT
    USING (app_current_role() IN ('agent','broker_owner','platform_admin'));

DROP POLICY IF EXISTS public_property_records_write ON public_property_records;
CREATE POLICY public_property_records_write ON public_property_records
    FOR ALL
    USING (app_is_platform_admin())
    WITH CHECK (app_is_platform_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON public_property_records TO oracle_app;
