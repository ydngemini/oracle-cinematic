-- Structured 2D/3D floor plans for a lead or listing.
--
-- One CURRENT plan per property, plus an append-only revision trail so an
-- agent can see what a rehab estimate was computed against at the time. The
-- geometry itself is a single jsonb document (FloorplanDocument, see
-- oracle-app/src/lib/floorplan/protocol.ts) rather than normalised wall/room
-- tables: it is always read and written whole by the editor, is never queried
-- by individual wall, and its shape is owned by the client schema version.
--
-- Denormalised metrics ARE columns, because underwriting, search and reporting
-- filter on square footage and must not pay a jsonb traversal to do it.
--
-- Depends on 0013 (property_media / leads / listings) for the FK targets.

BEGIN;

CREATE TABLE IF NOT EXISTS property_floorplans (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL,

    lead_id             uuid REFERENCES leads(id) ON DELETE CASCADE,
    listing_id          uuid REFERENCES listings(id) ON DELETE CASCADE,

    -- Client-side schema version of the `document` payload. Read paths refuse
    -- documents newer than they understand rather than mis-rendering them.
    schema_version      integer NOT NULL DEFAULT 1,
    document            jsonb   NOT NULL,

    -- Denormalised from `document` on write. Kept in sync by the API, which is
    -- the only writer; the CHECK below stops obviously wrong values landing.
    total_sqft          numeric(10, 2) NOT NULL DEFAULT 0,
    wall_linear_ft      numeric(10, 2) NOT NULL DEFAULT 0,
    room_count          integer NOT NULL DEFAULT 0,
    level_count         integer NOT NULL DEFAULT 0,

    -- Provenance. ai_generated drives the same AI-media disclosure the splat
    -- viewer carries; anything the vision pipeline produced MUST set it true.
    source              text NOT NULL DEFAULT 'manual',
    ai_generated        boolean NOT NULL DEFAULT false,
    model_version       text,
    confidence          numeric(4, 3),

    created_by          uuid,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- Exactly one subject, never both, never neither.
    CONSTRAINT property_floorplans_subject_chk CHECK (
        (lead_id IS NOT NULL AND listing_id IS NULL)
     OR (lead_id IS NULL AND listing_id IS NOT NULL)
    ),
    CONSTRAINT property_floorplans_source_chk CHECK (
        source IN ('manual', 'ai_vision', 'parcel_vector', 'imported')
    ),
    CONSTRAINT property_floorplans_metrics_chk CHECK (
        total_sqft >= 0 AND wall_linear_ft >= 0
        AND room_count >= 0 AND level_count >= 0
        AND (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
    ),
    -- A machine-generated plan without a model version is unattributable.
    CONSTRAINT property_floorplans_ai_attribution_chk CHECK (
        ai_generated = false OR model_version IS NOT NULL
    )
);

-- One current plan per subject. Partial uniques because each FK is nullable.
CREATE UNIQUE INDEX IF NOT EXISTS property_floorplans_lead_uniq
    ON property_floorplans (tenant_id, lead_id) WHERE lead_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS property_floorplans_listing_uniq
    ON property_floorplans (tenant_id, listing_id) WHERE listing_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS property_floorplans_tenant_idx
    ON property_floorplans (tenant_id, updated_at DESC);


-- Append-only revision trail. Never updated, only inserted; lets an agent
-- answer "what layout was this $84k estimate based on?" months later.
CREATE TABLE IF NOT EXISTS property_floorplan_revisions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL,
    floorplan_id        uuid NOT NULL REFERENCES property_floorplans(id) ON DELETE CASCADE,
    revision            integer NOT NULL,
    document            jsonb NOT NULL,
    total_sqft          numeric(10, 2) NOT NULL DEFAULT 0,
    wall_linear_ft      numeric(10, 2) NOT NULL DEFAULT 0,
    -- Snapshot of the rehab line items this revision produced, so the estimate
    -- is reproducible even after the cost table changes.
    rehab_items         jsonb,
    created_by          uuid,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT property_floorplan_revisions_uniq UNIQUE (floorplan_id, revision)
);

CREATE INDEX IF NOT EXISTS property_floorplan_revisions_lookup_idx
    ON property_floorplan_revisions (tenant_id, floorplan_id, revision DESC);


-- Row-level security isolates tenant data.
ALTER TABLE property_floorplans ENABLE ROW LEVEL SECURITY;
ALTER TABLE property_floorplans FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS property_floorplans_tenant_isolation ON property_floorplans;
CREATE POLICY property_floorplans_tenant_isolation
    ON property_floorplans
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

ALTER TABLE property_floorplan_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE property_floorplan_revisions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS property_floorplan_revisions_tenant_isolation ON property_floorplan_revisions;
CREATE POLICY property_floorplan_revisions_tenant_isolation
    ON property_floorplan_revisions
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Agents create and revise plans. Revisions are append-only by grant: no
-- UPDATE and no DELETE, so the audit trail cannot be rewritten from the app.
GRANT SELECT, INSERT ON property_floorplans TO oracle_app;
GRANT UPDATE (document, schema_version, total_sqft, wall_linear_ft, room_count,
              level_count, source, ai_generated, model_version, confidence, updated_at)
    ON property_floorplans TO oracle_app;
GRANT DELETE ON property_floorplans TO oracle_app;

GRANT SELECT, INSERT ON property_floorplan_revisions TO oracle_app;

COMMIT;
