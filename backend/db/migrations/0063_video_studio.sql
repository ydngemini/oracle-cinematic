-- Video Marketing Studio jobs table.
--
-- Stores the lifecycle of each video generation job (text or image-based),
-- including source photos, script, status, and the resulting media blob.
-- PyAV concatenates multiple clips from a single job when multi-image reels
-- are requested.

BEGIN;

-- Widen the property_media kind CHECK to admit 'video' (studio output MP4s are
-- stored as property_media rows so delivery reuses the authenticated
-- /api/media/{id} join and media_blobs' FK chain stays intact).
DO $$ BEGIN
    IF to_regclass('property_media') IS NOT NULL THEN
        ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_media_kind;
        ALTER TABLE property_media ADD CONSTRAINT chk_media_kind
            CHECK (kind IN ('photo', 'document', 'splat', 'floorplan', 'tour', 'pano', 'video'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS video_studio_jobs (
    id                   uuid NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Job metadata
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),

    -- Source inputs: text prompt or up to N photos (JSON array of media IDs)
    -- Size is enforced at submission time and must match Sora's allowed sizes.
    kind                 text NOT NULL CHECK (kind IN ('text', 'image')),
    source               jsonb NOT NULL, -- {property:{...}, images:[media_id,...], voiceover:bool}
    size                 text NOT NULL CHECK (size IN ('720x1280', '1280x720')),
    script               text NOT NULL DEFAULT '',

    -- Lifecycle
    status               text NOT NULL CHECK (
        status IN ('queued', 'scripting', 'generating', 'stitching', 'ready', 'failed', 'cancelled')
    ) DEFAULT 'queued',
    
    -- Provider coordination
    provider_job_ids     text[] NOT NULL DEFAULT '{}', -- tracks N Sora clips (ids like "videos/…")
    media_id            uuid REFERENCES media_blobs(media_id) ON DELETE SET NULL, -- final stitched MP4
    
    -- Outcome
    error                text,

    -- Quota/rate limiting
    quota_daily_slot     date NOT NULL DEFAULT CURRENT_DATE,
    quota_consumed_units numeric(12,2) NOT NULL DEFAULT 0.00  -- seconds consumed (per-clip * count)
);

-- Optimized fetch of recent jobs for the tenant's dashboard
CREATE INDEX IF NOT EXISTS idx_video_studio_jobs_recent
    ON video_studio_jobs (tenant_id, created_at DESC);

-- Speed up quota enforcement queries (daily cap, per-tenant slot)
CREATE INDEX IF NOT EXISTS idx_video_studio_jobs_quota_slot
    ON video_studio_jobs (tenant_id, quota_daily_slot)
    WHERE status NOT IN ('failed', 'cancelled');

CREATE INDEX IF NOT EXISTS idx_video_studio_jobs_active
    ON video_studio_jobs (status, created_at)
    WHERE status IN ('queued', 'scripting', 'generating', 'stitching');

-- updated_at touch trigger (set_updated_at() created in an earlier migration).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'set_updated_at')
       AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_video_studio_jobs_updated') THEN
        CREATE TRIGGER trg_video_studio_jobs_updated
            BEFORE UPDATE ON video_studio_jobs
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- Row-level security isolates tenant data
ALTER TABLE video_studio_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_studio_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS video_studio_jobs_tenant_isolation ON video_studio_jobs;
CREATE POLICY video_studio_jobs_tenant_isolation
    ON video_studio_jobs
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Operators initiate jobs; the worker process updates status and writes media.
GRANT SELECT, INSERT ON video_studio_jobs TO oracle_app;
GRANT UPDATE (status, script, provider_job_ids, media_id, error, updated_at, quota_consumed_units)
    ON video_studio_jobs TO oracle_app;

COMMIT;