-- 0101 — a reconstruction job records what happened at each stage.
--
-- The pipeline has never produced a non-synthetic splat, and when a run failed
-- the row said `status='failed'` with one `error` string. That is enough to
-- know something broke and not enough to know WHICH STAGE broke — capture,
-- frame extraction, camera registration, Gaussian training, delivery
-- conversion or storage. Each has a different fix and they were indistinguishable.
--
-- `diagnostics` is one jsonb document, written per stage, so a finished or
-- failed job can answer "how far did it get and what did each stage measure".
-- One column rather than twenty: the metrics a stage can report depend on which
-- provider ran, and inventing columns for metrics no tool emits would be
-- inventing the metrics.
--
-- Nothing in here is private capture content — counts, sizes, durations, exit
-- statuses and tool names only. Image bytes and file contents never land here.

BEGIN;

ALTER TABLE reconstruction_jobs
    ADD COLUMN IF NOT EXISTS diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb;

-- A job that finished training but produced no browser-delivery asset is NOT
-- ready, and the difference has to be answerable in SQL rather than by reading
-- a log. `quality_gate` records which gate refused, if one did.
ALTER TABLE reconstruction_jobs
    ADD COLUMN IF NOT EXISTS quality_gate text;

-- Distinguishes "the capture was not good enough" from "the infrastructure
-- broke". Both currently look like status='failed', and they need opposite
-- responses: recapture versus fix the deployment.
ALTER TABLE reconstruction_jobs
    DROP CONSTRAINT IF EXISTS reconstruction_jobs_status_chk;
ALTER TABLE reconstruction_jobs
    ADD CONSTRAINT reconstruction_jobs_status_chk CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed',
        -- The capture itself cannot support a reconstruction. Not an outage.
        'failed_quality_gate'
    ));

COMMIT;
