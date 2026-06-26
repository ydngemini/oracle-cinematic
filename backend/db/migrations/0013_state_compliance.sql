-- ===========================================================================
-- 0013_state_compliance.sql — State compliance, licensing, MLS, and market data
--
-- Creates the full table surface consumed by state_compliance.py:
--
--   1.  state_regulatory_profiles   — per-state regulatory settings
--   2.  state_disclosure_forms      — disclosure form catalogue
--   3.  state_contract_templates    — contract template catalogue
--   4.  state_advertising_rules     — advertising compliance rules
--   5.  state_licensing_requirements— pre-license/CE/renewal rules per state
--   6.  state_reciprocity_matrix    — state-to-state licence reciprocity
--   7.  agent_licenses              — tenant-scoped agent licence records
--   8.  agent_ce_log                — CE credit log (per tenant, per agent)
--   9.  mls_boards                  — MLS board registry
--   10. mls_sync_status             — feed sync health snapshot
--   11. oracle_mls_listings         — normalised listing store (all MLS feeds)
--   12. state_market_stats          — state-level market aggregate (mat. view)
--   13. county_market_stats         — county-level market + tax data
--   14. fema_flood_zones            — FEMA NFHL flood zone polygons (PostGIS)
--   15. fema_communities            — FEMA community / FIRM panel metadata
--   16. school_districts            — NCES school district centroids
--   17. parcel_zoning               — assessor parcel zoning classification
--   18. transactions                — lightweight transaction header
--   19. compliance_checklist_items  — per-transaction disclosure checklist
--
-- RLS policy: public/reference tables (state_*, mls_boards, fema_*, school_*,
-- parcel_*) are readable by all authenticated tenants — no row-level filter.
-- Tenant-private tables (agent_licenses, agent_ce_log, compliance_checklist_items)
-- copy the standard 0001 isolation posture.
--
-- Idempotent: IF NOT EXISTS / DO $$ guards throughout; safe to re-apply.
-- Requires: PostGIS (for flood zone geometry); earthdistance extension (school
-- radius queries).  Both are enabled on Aurora PostgreSQL ≥ 15.
-- ===========================================================================

-- PostGIS + earthdistance power the geospatial features (flood-zone geometry,
-- school/listing radius indexes). Both are enabled on Aurora PostgreSQL >= 15,
-- but may be absent on a slim local image (e.g. postgres:*-alpine). We attempt to
-- enable them WITHOUT failing the whole migration if they are unavailable: every
-- geospatial object below is individually guarded on its extension being present,
-- so the core compliance tables (mls_boards, state_*, agent_licenses, ...) always
-- create. Geospatial features stay absent locally and light up automatically
-- wherever the extensions exist.
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS postgis;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'PostGIS unavailable (%) — flood-zone geometry + GIST indexes skipped', SQLERRM;
END $$;

DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS earthdistance CASCADE;  -- also enables cube
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'earthdistance unavailable (%) — ll_to_earth radius indexes skipped', SQLERRM;
END $$;

-- ---------------------------------------------------------------------------
-- 1. state_regulatory_profiles
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_regulatory_profiles (
    state_code                  char(2)      PRIMARY KEY,
    state_name                  text         NOT NULL,
    attorney_review_required    boolean      NOT NULL DEFAULT false,
    mandatory_disclosure        boolean      NOT NULL DEFAULT true,
    has_tds                     boolean      NOT NULL DEFAULT false,
    license_authority           text         NOT NULL DEFAULT 'State Real Estate Commission',
    license_authority_url       text,
    regulatory_url              text,
    ce_hours_per_cycle          smallint,
    license_renewal_years       smallint     NOT NULL DEFAULT 2,
    buyer_agency_required       boolean      NOT NULL DEFAULT false,
    dual_agency_permitted       boolean      NOT NULL DEFAULT true,
    designated_agency_permitted boolean      NOT NULL DEFAULT true,
    sub_agency_permitted        boolean      NOT NULL DEFAULT true,
    earnest_money_escrow_days   smallint,
    transfer_tax_rate           numeric(6,4),
    notes                       text,
    updated_at                  timestamptz  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 2. state_disclosure_forms
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_disclosure_forms (
    id             uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    state_code     char(2)      NOT NULL REFERENCES state_regulatory_profiles(state_code),
    form_name      text         NOT NULL,
    form_type      text         NOT NULL,   -- seller_disclosure | tds | lead_paint | ...
    required_when  text         NOT NULL DEFAULT '',
    effective_date date,
    download_url   text,
    notes          text,
    updated_at     timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sdf_state_type
    ON state_disclosure_forms(state_code, form_type);

-- ---------------------------------------------------------------------------
-- 3. state_contract_templates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_contract_templates (
    id             uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    state_code     char(2)      NOT NULL REFERENCES state_regulatory_profiles(state_code),
    template_name  text         NOT NULL,
    association    text         NOT NULL DEFAULT '',
    property_types text[]       NOT NULL DEFAULT '{}',
    version        text         NOT NULL DEFAULT '',
    effective_date date,
    download_url   text,
    updated_at     timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sct_state ON state_contract_templates(state_code);

-- ---------------------------------------------------------------------------
-- 4. state_advertising_rules
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_advertising_rules (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    state_code       char(2)     NOT NULL REFERENCES state_regulatory_profiles(state_code),
    category         text        NOT NULL,  -- team_names | brokerage_name | internet_ads | ...
    requirement      text        NOT NULL,
    enforcement_body text        NOT NULL DEFAULT '',
    citations        text[]      NOT NULL DEFAULT '{}',
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sar_state_cat ON state_advertising_rules(state_code, category);

-- ---------------------------------------------------------------------------
-- 5. state_licensing_requirements
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_licensing_requirements (
    id                          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    state_code                  char(2)     NOT NULL REFERENCES state_regulatory_profiles(state_code),
    license_type                text        NOT NULL,  -- salesperson | broker | broker_associate
    pre_license_hours           smallint    NOT NULL DEFAULT 0,
    exam_required               boolean     NOT NULL DEFAULT true,
    background_check            boolean     NOT NULL DEFAULT true,
    errors_omissions_required   boolean     NOT NULL DEFAULT false,
    ce_hours_per_cycle          smallint    NOT NULL DEFAULT 0,
    renewal_cycle_years         smallint    NOT NULL DEFAULT 2,
    sponsoring_broker_required  boolean     NOT NULL DEFAULT true,
    license_authority           text        NOT NULL DEFAULT 'State Real Estate Commission',
    application_fee_usd         numeric(8,2),
    exam_provider               text,       -- PSI | Pearson VUE | AMP
    notes                       text,
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (state_code, license_type)
);

-- ---------------------------------------------------------------------------
-- 6. state_reciprocity_matrix
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_reciprocity_matrix (
    id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    from_state               char(2)     NOT NULL,
    to_state                 char(2)     NOT NULL,
    reciprocity_class        text        NOT NULL DEFAULT 'none',  -- full | partial | none
    additional_requirements  text[]      NOT NULL DEFAULT '{}',
    notes                    text,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_state, to_state)
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_reciprocity_class'
    ) THEN
        ALTER TABLE state_reciprocity_matrix
            ADD CONSTRAINT chk_reciprocity_class
            CHECK (reciprocity_class IN ('full', 'partial', 'none'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_srm_from ON state_reciprocity_matrix(from_state);
CREATE INDEX IF NOT EXISTS idx_srm_to   ON state_reciprocity_matrix(to_state);

-- ---------------------------------------------------------------------------
-- 7. agent_licenses  (tenant-private)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_licenses (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid        NOT NULL,
    agent_id            text        NOT NULL,
    state_code          char(2)     NOT NULL,
    license_type        text        NOT NULL DEFAULT 'salesperson',
    license_number      text        NOT NULL,
    status              text        NOT NULL DEFAULT 'active',
    issued_date         date,
    expiry_date         date,
    ce_hours_completed  smallint    NOT NULL DEFAULT 0,
    ce_hours_required   smallint    NOT NULL DEFAULT 0,
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_license_status') THEN
        ALTER TABLE agent_licenses
            ADD CONSTRAINT chk_license_status
            CHECK (status IN ('active', 'expired', 'suspended', 'inactive'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_al_tenant_agent ON agent_licenses(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_al_expiry        ON agent_licenses(expiry_date) WHERE status = 'active';

-- RLS (mirrors 0001 pattern)
ALTER TABLE agent_licenses ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'agent_licenses' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON agent_licenses
            USING (
                current_setting('app.current_tenant', true) = '00000000-0000-0000-0000-000000000000'
                OR tenant_id::text = current_setting('app.current_tenant', true)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 8. agent_ce_log  (tenant-private)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_ce_log (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid        NOT NULL,
    agent_id           text        NOT NULL,
    state_code         char(2)     NOT NULL,
    provider           text        NOT NULL,
    course_name        text        NOT NULL,
    hours              numeric(5,1) NOT NULL,
    completion_date    date        NOT NULL,
    certificate_number text,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ce_agent_state ON agent_ce_log(agent_id, state_code, completion_date);

ALTER TABLE agent_ce_log ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'agent_ce_log' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON agent_ce_log
            USING (
                current_setting('app.current_tenant', true) = '00000000-0000-0000-0000-000000000000'
                OR tenant_id::text = current_setting('app.current_tenant', true)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 9. mls_boards
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mls_boards (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    mls_name      text        NOT NULL,
    states        char(2)[]   NOT NULL DEFAULT '{}',
    counties      text[]      NOT NULL DEFAULT '{}',
    member_count  integer,
    listing_count integer,
    feed_type     text        NOT NULL DEFAULT 'RESO_Web_API',
    data_sharing  text        NOT NULL DEFAULT 'IDX_only',
    website       text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mb_states ON mls_boards USING GIN (states);

-- ---------------------------------------------------------------------------
-- 10. mls_sync_status
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mls_sync_status (
    mls_id            text        PRIMARY KEY,
    mls_name          text        NOT NULL DEFAULT '',
    feed_type         text        NOT NULL DEFAULT 'RESO_Web_API',
    last_sync_at      timestamptz,
    listings_synced   integer     NOT NULL DEFAULT 0,
    errors_last_24h   integer     NOT NULL DEFAULT 0,
    sync_lag_minutes  integer,
    notes             text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 11. oracle_mls_listings  (normalised listing store)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS oracle_mls_listings (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    mls_id            text        NOT NULL,
    mls_number        text        NOT NULL,
    address           text        NOT NULL,
    city              text        NOT NULL DEFAULT '',
    state_code        char(2)     NOT NULL,
    zip_code          text        NOT NULL DEFAULT '',
    county            text        NOT NULL DEFAULT '',
    latitude          numeric(10,7),
    longitude         numeric(10,7),
    list_price        numeric(14,2) NOT NULL,
    orig_list_price   numeric(14,2),
    status            text        NOT NULL DEFAULT 'active',
    property_type     text        NOT NULL DEFAULT 'residential_1_4',
    beds              smallint,
    baths_full        smallint,
    baths_half        smallint,
    sqft              integer,
    lot_sqft          integer,
    year_built        smallint,
    hoa_monthly       numeric(8,2),
    days_on_market    integer,
    list_date         date,
    close_date        date,
    close_price       numeric(14,2),
    description       text,
    photos            text[]      NOT NULL DEFAULT '{}',
    features          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    last_updated      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (mls_id, mls_number)
);

CREATE INDEX IF NOT EXISTS idx_oml_state_status ON oracle_mls_listings(state_code, status);
CREATE INDEX IF NOT EXISTS idx_oml_price         ON oracle_mls_listings(list_price);
CREATE INDEX IF NOT EXISTS idx_oml_mls_id        ON oracle_mls_listings(mls_id);
CREATE INDEX IF NOT EXISTS idx_oml_last_updated  ON oracle_mls_listings(last_updated DESC);

-- Spatial index (requires the earthdistance extension; skipped where absent)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'earthdistance')
       AND NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_oml_latlon') THEN
        CREATE INDEX idx_oml_latlon ON oracle_mls_listings
            USING GIST (ll_to_earth(latitude::float8, longitude::float8))
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 12. state_market_stats  (refreshed nightly by harvester)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_market_stats (
    state_code              char(2)      PRIMARY KEY,
    state_name              text         NOT NULL DEFAULT '',
    median_list_price       numeric(14,2),
    median_sale_price       numeric(14,2),
    median_days_on_market   numeric(6,1),
    months_of_supply        numeric(5,2),
    yoy_price_change_pct    numeric(6,3),
    active_listings         integer,
    closed_sales_last_30d   integer,
    list_to_sale_ratio      numeric(6,4),
    avg_price_per_sqft      numeric(10,2),
    as_of_date              date,
    updated_at              timestamptz  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 13. county_market_stats
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS county_market_stats (
    fips_code               char(5)      PRIMARY KEY,
    county_name             text         NOT NULL DEFAULT '',
    state_code              char(2)      NOT NULL,
    median_sale_price       numeric(14,2),
    median_list_price       numeric(14,2),
    median_days_on_market   numeric(6,1),
    property_tax_rate_pct   numeric(6,4),
    effective_tax_rate_pct  numeric(6,4),
    median_annual_tax       numeric(10,2),
    population              integer,
    households              integer,
    homeownership_rate_pct  numeric(5,2),
    as_of_date              date,
    updated_at              timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cms_state ON county_market_stats(state_code);

-- ---------------------------------------------------------------------------
-- 14. fema_flood_zones  (PostGIS geometry — SRID 4326 = WGS84)
-- ---------------------------------------------------------------------------
-- geom is a PostGIS geometry type, so it is NOT declared inline (that would make
-- the whole table fail to create without PostGIS). It is added — together with its
-- GIST index — only where PostGIS is present (see the guarded block below).
CREATE TABLE IF NOT EXISTS fema_flood_zones (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    flood_zone       text        NOT NULL,   -- AE, X, VE, 0.2PCT, …
    firm_panel       text,
    firm_date        date,
    community_number text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'fema_flood_zones' AND column_name = 'geom') THEN
            ALTER TABLE fema_flood_zones ADD COLUMN geom geometry(MultiPolygon, 4326);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_ffz_geom') THEN
            CREATE INDEX idx_ffz_geom ON fema_flood_zones USING GIST (geom);
        END IF;
    ELSE
        RAISE NOTICE 'PostGIS absent — fema_flood_zones.geom column + GIST index omitted locally';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ffz_zone      ON fema_flood_zones(flood_zone);
CREATE INDEX IF NOT EXISTS idx_ffz_community ON fema_flood_zones(community_number);

-- ---------------------------------------------------------------------------
-- 15. fema_communities
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fema_communities (
    community_number text  PRIMARY KEY,
    community_name   text  NOT NULL DEFAULT '',
    state_code       char(2),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 16. school_districts  (centroid-based for radius queries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS school_districts (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    district_name         text        NOT NULL,
    district_type         text        NOT NULL DEFAULT 'unified',
    state_code            char(2)     NOT NULL,
    county                text        NOT NULL DEFAULT '',
    nces_id               text,
    rating                numeric(3,1),
    enrollment            integer,
    student_teacher_ratio numeric(5,2),
    centroid_lat          numeric(10,7) NOT NULL,
    centroid_lng          numeric(10,7) NOT NULL,
    website               text,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'earthdistance')
       AND NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_sd_centroid') THEN
        CREATE INDEX idx_sd_centroid ON school_districts
            USING GIST (ll_to_earth(centroid_lat::float8, centroid_lng::float8));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_sd_state ON school_districts(state_code);

-- ---------------------------------------------------------------------------
-- 17. parcel_zoning
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parcel_zoning (
    parcel_id                    text        PRIMARY KEY,
    zone_code                    text        NOT NULL,
    zone_description             text        NOT NULL DEFAULT '',
    zone_category                text        NOT NULL DEFAULT 'residential',
    overlays                     text[]      NOT NULL DEFAULT '{}',
    permitted_uses               text[]      NOT NULL DEFAULT '{}',
    conditional_uses             text[]      NOT NULL DEFAULT '{}',
    max_height_ft                numeric(7,2),
    max_density_units_per_acre   numeric(7,2),
    min_lot_sqft                 integer,
    setback_front_ft             numeric(6,2),
    setback_rear_ft              numeric(6,2),
    setback_side_ft              numeric(6,2),
    jurisdiction                 text,
    updated_at                   timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_zone_category') THEN
        ALTER TABLE parcel_zoning
            ADD CONSTRAINT chk_zone_category
            CHECK (zone_category IN (
                'residential', 'commercial', 'industrial',
                'agricultural', 'mixed', 'special_purpose'
            ));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 18. transactions  (lightweight header for compliance checklist anchor)
-- If this table already exists from another migration, ALTER to add columns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid        NOT NULL,
    state_code   char(2)     NOT NULL,
    property_type text       NOT NULL DEFAULT 'residential_1_4',
    listing_id   uuid,
    lead_id      uuid,
    status       text        NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_txn_tenant ON transactions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_txn_state  ON transactions(state_code);

ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'transactions' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON transactions
            USING (
                current_setting('app.current_tenant', true) = '00000000-0000-0000-0000-000000000000'
                OR tenant_id::text = current_setting('app.current_tenant', true)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 19. compliance_checklist_items  (tenant-private)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance_checklist_items (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL,
    transaction_id  uuid        NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    disclosure_id   text        NOT NULL,
    form_name       text        NOT NULL,
    status          text        NOT NULL DEFAULT 'pending',
    due_date        date,
    delivered_at    timestamptz,
    signed_at       timestamptz,
    signed_by       text,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_checklist_status'
    ) THEN
        ALTER TABLE compliance_checklist_items
            ADD CONSTRAINT chk_checklist_status
            CHECK (status IN ('pending', 'delivered', 'signed', 'waived'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cci_txn ON compliance_checklist_items(transaction_id);
CREATE INDEX IF NOT EXISTS idx_cci_tenant_status
    ON compliance_checklist_items(tenant_id, status);

ALTER TABLE compliance_checklist_items ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'compliance_checklist_items' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON compliance_checklist_items
            USING (
                current_setting('app.current_tenant', true) = '00000000-0000-0000-0000-000000000000'
                OR tenant_id::text = current_setting('app.current_tenant', true)
            );
    END IF;
END $$;

-- ===========================================================================
-- Seed: reference-data rows for state_regulatory_profiles (all 50 + DC)
-- Safe to re-run (ON CONFLICT DO NOTHING).
-- ===========================================================================
INSERT INTO state_regulatory_profiles
    (state_code, state_name, attorney_review_required, mandatory_disclosure,
     has_tds, ce_hours_per_cycle, license_renewal_years,
     dual_agency_permitted, buyer_agency_required)
VALUES
    ('AL','Alabama',       false, true,  false, 15, 2, true,  false),
    ('AK','Alaska',        false, true,  false, 20, 2, false, false),
    ('AZ','Arizona',       false, true,  false, 24, 2, true,  false),
    ('AR','Arkansas',      false, false, false, 7,  2, true,  false),
    ('CA','California',    false, true,  true,  45, 4, true,  false),
    ('CO','Colorado',      false, true,  true,  24, 3, false, false),
    ('CT','Connecticut',   true,  true,  false, 12, 2, true,  false),
    ('DE','Delaware',      true,  true,  false, 21, 2, true,  false),
    ('FL','Florida',       false, true,  true,  14, 2, false, false),
    ('GA','Georgia',       false, true,  false, 36, 4, true,  false),
    ('HI','Hawaii',        false, true,  false, 20, 2, true,  false),
    ('ID','Idaho',         false, true,  false, 12, 2, true,  false),
    ('IL','Illinois',      false, true,  true,  12, 2, true,  false),
    ('IN','Indiana',       false, true,  false, 12, 3, true,  false),
    ('IA','Iowa',          false, true,  false, 36, 3, true,  false),
    ('KS','Kansas',        false, true,  false, 12, 2, true,  false),
    ('KY','Kentucky',      false, true,  false, 6,  2, true,  false),
    ('LA','Louisiana',     false, true,  false, 12, 2, true,  false),
    ('ME','Maine',         false, true,  false, 21, 2, true,  false),
    ('MD','Maryland',      false, true,  false, 15, 2, false, false),
    ('MA','Massachusetts', true,  true,  false, 12, 2, true,  false),
    ('MI','Michigan',      false, true,  false, 18, 3, true,  false),
    ('MN','Minnesota',     false, true,  true,  30, 2, true,  false),
    ('MS','Mississippi',   false, true,  false, 16, 2, true,  false),
    ('MO','Missouri',      false, true,  false, 12, 2, true,  false),
    ('MT','Montana',       false, true,  false, 12, 2, true,  false),
    ('NE','Nebraska',      false, true,  false, 18, 2, true,  false),
    ('NV','Nevada',        false, true,  false, 24, 2, true,  false),
    ('NH','New Hampshire', false, false, false, 15, 3, true,  false),
    ('NJ','New Jersey',    false, true,  false, 12, 2, true,  false),
    ('NM','New Mexico',    false, false, false, 36, 3, true,  false),
    ('NY','New York',      true,  true,  false, 22, 2, true,  false),
    ('NC','North Carolina',false, true,  false, 8,  1, true,  false),
    ('ND','North Dakota',  false, false, false, 15, 2, true,  false),
    ('OH','Ohio',          false, true,  true,  30, 3, true,  false),
    ('OK','Oklahoma',      false, true,  false, 21, 3, true,  false),
    ('OR','Oregon',        false, true,  true,  30, 2, true,  false),
    ('PA','Pennsylvania',  false, true,  false, 14, 2, true,  false),
    ('RI','Rhode Island',  false, true,  false, 24, 2, true,  false),
    ('SC','South Carolina',true,  true,  false, 10, 2, true,  false),
    ('SD','South Dakota',  false, true,  false, 12, 2, true,  false),
    ('TN','Tennessee',     false, true,  false, 16, 2, true,  false),
    ('TX','Texas',         false, true,  true,  18, 2, false, false),
    ('UT','Utah',          false, true,  false, 18, 2, true,  false),
    ('VT','Vermont',       false, true,  false, 16, 2, true,  false),
    ('VA','Virginia',      false, true,  false, 16, 2, true,  false),
    ('WA','Washington',    false, true,  true,  30, 2, true,  false),
    ('WV','West Virginia', true,  true,  false, 7,  2, true,  false),
    ('WI','Wisconsin',     false, true,  false, 18, 2, false, false),
    ('WY','Wyoming',       false, false, false, 22, 2, true,  false),
    ('DC','District of Columbia', false, true, false, 15, 2, true, false)
ON CONFLICT (state_code) DO NOTHING;
