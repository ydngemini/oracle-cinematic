-- ==========================================================================
-- 0051_public_property_characteristics.sql
--
-- Promote source-published assessor/recorder characteristics into typed,
-- searchable columns. Values remain nullable: NULL means the jurisdiction did
-- not publish the field, and must never be replaced with a prediction.
-- ==========================================================================

ALTER TABLE public_property_records
    ADD COLUMN IF NOT EXISTS last_sale_price numeric,
    ADD COLUMN IF NOT EXISTS bedrooms numeric,
    ADD COLUMN IF NOT EXISTS bathrooms numeric,
    ADD COLUMN IF NOT EXISTS rooms numeric,
    ADD COLUMN IF NOT EXISTS year_built integer,
    ADD COLUMN IF NOT EXISTS property_class text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_public_property_last_sale_price'
    ) THEN
        ALTER TABLE public_property_records
            ADD CONSTRAINT ck_public_property_last_sale_price
            CHECK (last_sale_price IS NULL OR last_sale_price >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_public_property_bedrooms'
    ) THEN
        ALTER TABLE public_property_records
            ADD CONSTRAINT ck_public_property_bedrooms
            CHECK (bedrooms IS NULL OR bedrooms BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_public_property_bathrooms'
    ) THEN
        ALTER TABLE public_property_records
            ADD CONSTRAINT ck_public_property_bathrooms
            CHECK (bathrooms IS NULL OR bathrooms BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_public_property_rooms'
    ) THEN
        ALTER TABLE public_property_records
            ADD CONSTRAINT ck_public_property_rooms
            CHECK (rooms IS NULL OR rooms BETWEEN 0 AND 500);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_public_property_year_built'
    ) THEN
        ALTER TABLE public_property_records
            ADD CONSTRAINT ck_public_property_year_built
            CHECK (year_built IS NULL OR year_built BETWEEN 1600 AND 2200);
    END IF;
END
$$;

-- Correct provenance for already-indexed municipal violation observations.
-- These rows are not assessor parcels; the assessor reconciliation writes a
-- separate canonical Cook County PIN and duplicate suppression favors it.
UPDATE public_property_records
SET source_name = 'Chicago Building Violations (Socrata 22u3-xenr)',
    coverage_scope = 'city:Chicago'
WHERE source_key = 'chicago_building_violations'
  AND (
      source_name <> 'Chicago Building Violations (Socrata 22u3-xenr)'
      OR coverage_scope <> 'city:Chicago'
  );

UPDATE public_property_records
SET source_name = 'NYC HPD Open Housing-Code Violations (Socrata wvxf-dwi5)',
    coverage_scope = 'city:New York City'
WHERE source_key = 'nyc_hpd_violations'
  AND (
      source_name <> 'NYC HPD Open Housing-Code Violations (Socrata wvxf-dwi5)'
      OR coverage_scope <> 'city:New York City'
  );

CREATE INDEX IF NOT EXISTS idx_public_property_state_bedrooms
    ON public_property_records (state, bedrooms)
    WHERE bedrooms IS NOT NULL;
