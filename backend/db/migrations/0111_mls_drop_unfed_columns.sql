-- 0111 — remove the MLS columns nothing ever writes, and make `health` honest.
--
-- 0110 added nineteen columns. An adversarial review found nine of them dead:
-- written by nobody, or read by nobody, or both. A column that exists and is
-- always its default is worse than a missing one, because operators query it
-- and believe the answer.
--
-- The worst was `health`. Every runbook query said `SELECT mls_id, health FROM
-- mls_sync_status`, every row said `NOT_CONFIGURED` for its entire life, and
-- `idx_mls_sync_health` indexed a constant. Health has always been computed in
-- Python by compute_health(), which never looked at the column — so the
-- database's answer and the product's answer disagreed, and the database's was
-- the one in the runbook.
--
-- Dropping rather than populating is deliberate. Health derived from the
-- columns that ARE written (last_success_at, consecutive_failures,
-- backfill_complete, last_error_class) cannot go stale; a stored copy can, and
-- would need every writer to remember to update it.

BEGIN;

-- ── health: computed, never stored ───────────────────────────────────────
DROP INDEX IF EXISTS idx_mls_sync_health;
ALTER TABLE mls_sync_status DROP COLUMN IF EXISTS health;

-- ── Written by nobody, read by nobody ────────────────────────────────────
-- cursor_modified_at / cursor_key: the delta cursor is last_sync_at, and the
-- backfill cursor is backfill_cursor_key. These were a third idea that never
-- acquired a writer.
ALTER TABLE mls_sync_status DROP COLUMN IF EXISTS cursor_modified_at;
ALTER TABLE mls_sync_status DROP COLUMN IF EXISTS cursor_key;

-- last_error_at: last_error_class and the updated_at timestamp carry this.
ALTER TABLE mls_sync_status DROP COLUMN IF EXISTS last_error_at;

-- last_run: the runbook told operators to read `last_run->'rejected'`, which
-- always returned {}. Rejection detail lands in `notes`, which IS written.
ALTER TABLE mls_sync_status DROP COLUMN IF EXISTS last_run;

-- ── source_status: always NULL ───────────────────────────────────────────
-- mls_sink reads features['source_status'] and neither normalizer emits that
-- key, so the column and 0110's backfill for it matched nothing. The raw
-- provider status is preserved inside `features` where the adapters actually
-- put it; `status` holds the canonical value the product branches on.
ALTER TABLE oracle_mls_listings DROP COLUMN IF EXISTS source_status;

-- ── stale_after_minutes: kept, and now actually settable ─────────────────
-- Read by is_stale() and never written, so "configured per feed" was
-- aspirational. It stays because per-feed cadence is genuinely right — a
-- nightly board should not be called broken every morning — but it needs to be
-- reachable. An operator sets it directly; there is no customer-facing reason
-- to expose it.
COMMENT ON COLUMN mls_sync_status.stale_after_minutes IS
    'Minutes before a feed is considered STALE. Per-feed because boards '
    'publish at different cadences. Operator-set: '
    'UPDATE mls_sync_status SET stale_after_minutes = 180 WHERE mls_id = ...';

-- ── license_classification on listings: kept, now justified ──────────────
-- Currently written and not read: every reader classifies via the feed's
-- status row. It stays because it is the per-ROW record of what a listing was
-- ingested as, which survives a feed later being reclassified — the status row
-- only ever holds the current answer. Without it, reclassifying a feed would
-- silently rewrite the provenance of history.
COMMENT ON COLUMN oracle_mls_listings.license_classification IS
    'What this row was ingested as. Deliberately a per-row record: the feed''s '
    'current classification lives on mls_sync_status and can change.';

COMMIT;
