-- ---------------------------------------------------------------------------
-- 0166 — a studio video records who asked for it (prompt 03, T03.16, AC03.06;
-- ledger L03-13).
--
-- Suspending an agent cancels their queued work (privacy_lifecycle._PENDING),
-- but video_studio_jobs had no requester column: a suspended agent's queued
-- video still went to the paid provider, and only closing the whole
-- brokerage cancelled it (L03-11). video_studio_api.create_video_job now
-- stores the requesting login in created_by, and suspension cancels that
-- agent's queued videos with the reason.
--
-- created_by is the agent's sign-in identifier, the same value the other
-- queued-work tables keep (reconstruction_jobs, voice_walkthrough_jobs).
-- Rows created before this migration stay NULL: their requester was never
-- recorded and is not guessed; they are still cancelled when the brokerage
-- closes.
--
-- Previous release on this schema: unaffected (it never writes the column).
--
-- Rollback: ALTER TABLE video_studio_jobs DROP COLUMN created_by;
-- ---------------------------------------------------------------------------

ALTER TABLE video_studio_jobs ADD COLUMN IF NOT EXISTS created_by text;

COMMENT ON COLUMN video_studio_jobs.created_by IS
    '0166: the sign-in identifier of the agent who requested the video; NULL for rows created before 0166. Suspension cancels that agent''s queued videos.';
