-- Make partial national harvests explicit and persist safe per-source health.
BEGIN;

ALTER TABLE automation_jobs
    DROP CONSTRAINT IF EXISTS automation_jobs_state_check;
ALTER TABLE automation_jobs
    ADD CONSTRAINT automation_jobs_state_check CHECK (state IN (
        'draft','awaiting_approval','queued','leased','running','succeeded',
        'partial','failed','cancelled','dead_letter'
    ));

ALTER TABLE harvest_runs
    DROP CONSTRAINT IF EXISTS harvest_runs_state_check;
ALTER TABLE harvest_runs
    ADD CONSTRAINT harvest_runs_state_check CHECK (state IN (
        'running','succeeded','partial','failed','cancelled'
    ));

ALTER TABLE harvest_sources
    ADD COLUMN IF NOT EXISTS last_health_checked_at timestamptz,
    ADD COLUMN IF NOT EXISTS health_status text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS health_detail text;

ALTER TABLE harvest_sources
    DROP CONSTRAINT IF EXISTS harvest_sources_health_status_check;
ALTER TABLE harvest_sources
    ADD CONSTRAINT harvest_sources_health_status_check CHECK (
        health_status IN ('fresh','stale','degraded','failed','unknown')
    );

CREATE INDEX IF NOT EXISTS idx_harvest_sources_health
    ON harvest_sources(tenant_id, health_status, last_health_checked_at DESC);

COMMIT;
