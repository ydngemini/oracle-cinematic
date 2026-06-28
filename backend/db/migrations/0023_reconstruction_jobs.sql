-- 0023_reconstruction_jobs.sql
-- Walkable-tour Phase 3: durable job table for Gaussian-splat reconstruction +
-- the long-missing 'pano' media kind (Phase 2 forward-compat; media_api/tour_api
-- already reference it).
--
-- Idempotent + forward-only: IF NOT EXISTS / guarded DO-blocks everywhere, so
-- re-applying is a no-op. NOTE: there is no migration runner — only a FRESH db
-- volume auto-runs these via docker-entrypoint-initdb.d. Apply to an existing DB
-- manually:  docker exec -i oracle-db-1 psql -U postgres -d oracle -f - < this

-- 1. Widen the property_media kind CHECK to admit 'pano' (360° equirect rooms).
DO $$ BEGIN
    IF to_regclass('property_media') IS NOT NULL THEN
        ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_media_kind;
        ALTER TABLE property_media ADD CONSTRAINT chk_media_kind
            CHECK (kind IN ('photo', 'document', 'splat', 'floorplan', 'tour', 'pano'));
    END IF;
END $$;

-- 2. reconstruction_jobs — one row per enqueued capture→splat job.
CREATE TABLE IF NOT EXISTS reconstruction_jobs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id)   ON DELETE CASCADE,
    lead_id      uuid REFERENCES leads(id)              ON DELETE CASCADE,
    listing_id   uuid REFERENCES listings(id)           ON DELETE CASCADE,
    status       text NOT NULL DEFAULT 'queued',
    provider     text,
    progress     int  NOT NULL DEFAULT 0,
    media_id     uuid REFERENCES property_media(id)      ON DELETE SET NULL,
    error        text,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_recon_status CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    CONSTRAINT chk_recon_property CHECK (lead_id IS NOT NULL OR listing_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_recon_jobs_tenant  ON reconstruction_jobs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_recon_jobs_lead    ON reconstruction_jobs(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_jobs_listing ON reconstruction_jobs(listing_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recon_jobs_active  ON reconstruction_jobs(status, created_at)
    WHERE status IN ('queued', 'running');

-- updated_at touch trigger (set_updated_at() created in an earlier migration).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'set_updated_at')
       AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_recon_jobs_updated') THEN
        CREATE TRIGGER trg_recon_jobs_updated
            BEFORE UPDATE ON reconstruction_jobs
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- 3. RLS — tenant isolation, FORCEd (canonical policy shape used across the schema).
ALTER TABLE reconstruction_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE reconstruction_jobs FORCE  ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'reconstruction_jobs_tenant_isolation') THEN
        CREATE POLICY reconstruction_jobs_tenant_isolation ON reconstruction_jobs
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;

-- 4. Grant to the app login role if it exists (matches 0022's guarded grant).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        GRANT SELECT, INSERT, UPDATE ON reconstruction_jobs TO oracle_app;
    END IF;
END $$;
