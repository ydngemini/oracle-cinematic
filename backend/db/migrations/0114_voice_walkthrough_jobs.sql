-- ---------------------------------------------------------------------------
-- 0114 — voice_walkthrough_jobs: the walkthrough-transcription queue, durable.
--
-- POST /api/voice/log-walkthrough staged the audio on the receiving process's
-- local disk and put a job on an in-process asyncio.Queue. In production that
-- POST lands on an API replica (ORACLE_PROCESS_ROLE=web), which starts no
-- transcription worker — and the worker service is a different container that
-- cannot read the API replica's disk. Every walkthrough was accepted (202) and
-- then never processed (Mission 8 capacity audit).
--
-- The row now carries the audio itself (capped at ORACLE_AUDIO_MAX_BYTES,
-- default 25 MiB) until the worker claims it with FOR UPDATE SKIP LOCKED;
-- the bytes are dropped as soon as the job reaches a terminal state, so the
-- table holds audio only for the backlog, not forever.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS voice_walkthrough_jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id           uuid NOT NULL,
    created_by        text NOT NULL,
    original_filename text NOT NULL,
    suffix            text NOT NULL,
    audio             bytea,
    audio_bytes       integer NOT NULL CHECK (audio_bytes >= 0),
    status            text NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts          integer NOT NULL DEFAULT 0,
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- Terminal jobs keep their metadata for the audit trail, never the audio.
    CONSTRAINT chk_voice_job_audio_released
        CHECK (status IN ('queued', 'running') OR audio IS NULL)
);

-- The claim: oldest queued row. Partial, so it stays the size of the backlog.
CREATE INDEX IF NOT EXISTS idx_voice_walkthrough_jobs_claim
    ON voice_walkthrough_jobs (created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_voice_walkthrough_jobs_tenant
    ON voice_walkthrough_jobs (tenant_id, created_at DESC);

ALTER TABLE voice_walkthrough_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_walkthrough_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS voice_walkthrough_jobs_tenant_isolation ON voice_walkthrough_jobs;
CREATE POLICY voice_walkthrough_jobs_tenant_isolation ON voice_walkthrough_jobs
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON voice_walkthrough_jobs TO oracle_app;
