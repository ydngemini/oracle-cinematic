-- 0124: job-claim aging (automation_jobs.claim_next_job).
--
-- The claim used to order strictly by priority. Under sustained load a steady
-- stream of higher-priority jobs starved every lower one for as long as the
-- load lasted (5+ minutes in the first-10-brokerage drill), privacy exports
-- and erasures included. The claim now takes the oldest job that has been
-- ready for more than ORACLE_JOB_STARVATION_SECONDS first. This index lets
-- that lookup walk queued jobs in age order and stop at the first one.
CREATE INDEX IF NOT EXISTS idx_automation_jobs_aged
    ON automation_jobs (queue_name, scheduled_at, created_at)
    WHERE state IN ('queued', 'failed');
