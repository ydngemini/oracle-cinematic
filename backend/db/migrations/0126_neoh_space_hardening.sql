-- 0126: Neoh Space hardening (Mission 4, Part I).
--
-- reconstruction_jobs grows the columns a production 3D pipeline needs:
--
--   stage              the explicit customer-facing state machine
--                      (space_status.py). `status` stays the coarse claim key.
--   attempts           how many times a worker has started this job; bounds
--                      automatic resume after a restart (cost guard).
--   idempotency_key    a client-supplied key; the same key returns the same job
--                      instead of renting a second GPU.
--   failure_category   quality_gate / provider / conversion / orphaned /
--                      stalled / storage — picks the customer message.
--   pipeline_version   which pipeline produced the asset, so a later converter
--                      or viewer never silently reinterprets an old one.
--   provider_job_id    the GPU provider's id (RunPod pod id) the moment it
--                      exists, so a restart can terminate exactly that pod.
--   cost_estimate_usd / gpu_seconds / output_bytes   pricing inputs.
--   raw_output_key     the provider's raw output, preserved when delivery
--                      conversion fails, so a retry reconverts without
--                      retraining.
--   retry_of / resume_from   lineage of a retry, and where it starts.
--   started_at / finished_at / published_at
--
-- property_media grows:
--   superseded_at      set on the previously published capture in the SAME
--                      transaction that publishes its replacement (atomic
--                      publish). The resolver shows only unsuperseded splats.
--   scene_override     curated entry viewpoint / operator scale calibration,
--                      merged over the computed scene.json at read time.
--
-- Additive only. One active build per property is enforced by a partial
-- unique index, created only when the live data already satisfies it.

ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS stage text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS failure_category text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS pipeline_version text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS provider_job_id text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS cost_estimate_usd numeric(10, 4);
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS gpu_seconds numeric(12, 1);
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS output_bytes bigint;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS raw_output_key text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS retry_of uuid
    REFERENCES reconstruction_jobs(id) ON DELETE SET NULL;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS resume_from text;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz;
ALTER TABLE reconstruction_jobs ADD COLUMN IF NOT EXISTS published_at timestamptz;

ALTER TABLE reconstruction_jobs DROP CONSTRAINT IF EXISTS reconstruction_jobs_status_chk;
ALTER TABLE reconstruction_jobs ADD CONSTRAINT reconstruction_jobs_status_chk CHECK (status IN (
    'queued', 'running', 'succeeded', 'failed', 'failed_quality_gate', 'needs_attention'
));

ALTER TABLE reconstruction_jobs DROP CONSTRAINT IF EXISTS reconstruction_jobs_stage_chk;
ALTER TABLE reconstruction_jobs ADD CONSTRAINT reconstruction_jobs_stage_chk CHECK (
    stage IS NULL OR stage IN (
        'queued', 'preparing', 'reconstructing', 'converting', 'analyzing',
        'ready', 'needs_attention', 'failed'
    )
);

ALTER TABLE reconstruction_jobs DROP CONSTRAINT IF EXISTS reconstruction_jobs_resume_chk;
ALTER TABLE reconstruction_jobs ADD CONSTRAINT reconstruction_jobs_resume_chk CHECK (
    resume_from IS NULL OR resume_from IN ('conversion')
);

-- Same key, same job: a double-tap or a retried POST must not rent a second GPU.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recon_jobs_idempotency
    ON reconstruction_jobs (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Rerun accounting (cost guard) reads recent jobs per property.
CREATE INDEX IF NOT EXISTS idx_recon_jobs_lead_recent
    ON reconstruction_jobs (tenant_id, lead_id, created_at DESC)
    WHERE lead_id IS NOT NULL;

-- One active build per property. Guarded: if a live database already holds
-- two active jobs for one property the index would fail the migration, so it
-- is skipped with a notice and the route-level check still applies.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM reconstruction_jobs
         WHERE status IN ('queued', 'running')
         GROUP BY tenant_id, COALESCE(lead_id, listing_id)
        HAVING count(*) > 1
    ) THEN
        CREATE UNIQUE INDEX IF NOT EXISTS uq_recon_jobs_one_active_per_property
            ON reconstruction_jobs (tenant_id, (COALESCE(lead_id, listing_id)))
            WHERE status IN ('queued', 'running');
    ELSE
        RAISE NOTICE 'uq_recon_jobs_one_active_per_property skipped: duplicate active jobs exist';
    END IF;
END $$;

ALTER TABLE property_media ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
ALTER TABLE property_media ADD COLUMN IF NOT EXISTS scene_override jsonb;

COMMENT ON COLUMN property_media.superseded_at IS
    'Set when a newer capture of the same property was published in its place '
    '(atomic publish). The tour shows only rows where this is NULL; the old '
    'asset is kept for rollback until retention removes it.';
COMMENT ON COLUMN property_media.scene_override IS
    'Curated Neoh Space overrides (entryCamera, scale calibration) merged over '
    'the computed scene.json at read time. Never rewrites the computed file.';
COMMENT ON COLUMN reconstruction_jobs.stage IS
    'Canonical Neoh Space state (space_status.STAGES). status is the coarse '
    'claim key and is kept consistent with it.';
