-- Repair three defects introduced when video moved out of media_blobs.
--
-- 0066 added CHECK (kind <> 'video' OR s3_key IS NOT NULL), so a finished video
-- is a property_media row backed by object storage with NO media_blobs row.
-- Three things in 0063 still assumed the old bytea world:
--
--   1. video_studio_jobs.media_id still points at media_blobs(media_id). Nothing
--      ever writes that row now, so the final UPDATE that marks a job 'ready'
--      raises ForeignKeyViolationError — every video job fails at the last step,
--      after Sora has already been billed for every clip.
--
--   2. property_media has nowhere to record a content type. It used to live on
--      the media_blobs row; object-storage rows lost it, so /api/media/{id} was
--      reduced to hardcoding video/mp4. Client uploads are sniffed as
--      video/quicktime (iPhone .mov) and video/webm too, and serving those as
--      mp4 under X-Content-Type-Options: nosniff gives a black player.
--
--   3. quota_daily_slot is fixed at INSERT and absent from 0063's GRANT UPDATE
--      list, so a job queued at 23:55 and generated after midnight reserves its
--      quota against yesterday's slot — invisible to today's SUM, and the daily
--      cap is silently bypassed for the whole backlog. Quota is consumed on the
--      day it is spent, so the worker must be able to restamp the slot.
--
-- Depends on 0012 (property_media), 0022 (media_blobs), 0063 (video_studio_jobs),
-- 0066 (chk_video_is_s3_backed).

BEGIN;

-- ── 1. Repoint the finished-video FK at property_media ──────────────────────
ALTER TABLE video_studio_jobs DROP CONSTRAINT IF EXISTS video_studio_jobs_media_id_fkey;

ALTER TABLE video_studio_jobs
    ADD CONSTRAINT video_studio_jobs_media_id_fkey
    FOREIGN KEY (media_id) REFERENCES property_media(id) ON DELETE SET NULL;

-- ── 2. Record the real content type for object-storage-backed media ─────────
-- Nullable: blob-backed rows keep theirs on media_blobs, which stays the
-- authoritative source for anything with bytes in Postgres.
ALTER TABLE property_media
    ADD COLUMN IF NOT EXISTS content_type text;

COMMENT ON COLUMN property_media.content_type IS
    'Content type for object-storage-backed rows (s3_key IS NOT NULL). Blob-backed rows carry theirs on media_blobs.';

-- Backfill what we can from the blob table so the column is never a second,
-- disagreeing source of truth for rows that have both.
UPDATE property_media AS pm
   SET content_type = mb.content_type
  FROM media_blobs AS mb
 WHERE mb.media_id = pm.id
   AND pm.content_type IS NULL;

-- ── 3. Let the worker restamp the quota day it actually spends ──────────────
-- Only video_studio_jobs needs this: 0063 granted UPDATE column-by-column, so a
-- column omitted from that list is unwritable. property_media holds a plain
-- table-level UPDATE grant, which already covers the new column above.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        EXECUTE 'GRANT UPDATE (quota_daily_slot) ON video_studio_jobs TO oracle_app';
    END IF;
END
$$;

COMMIT;
