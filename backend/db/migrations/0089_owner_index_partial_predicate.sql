-- 0089 — narrow the owner-name index to the rows that can actually be looked up
--
-- 0086 added idx_public_property_owner_normalized to kill a >120s seq scan in
-- _property_candidates, which it does (0.93ms). But it indexes all 8,623,748
-- rows, and _property_candidates never queries most of them:
--
--     client_ai_automation.py:549-550
--     normalized_name = re.sub(r"[^a-z0-9]", "", str(full_name or "").lower())
--     if linked_count or len(normalized_name) < 5:
--         return []                       <- never reaches the query
--
-- So every row whose owner_name normalises to fewer than 5 characters — NULLs,
-- blanks, punctuation-only junk — occupies an index entry no query can ever
-- reach. Measured: 956,555 of 8,623,748 rows, 11.1% of a 365 MB index.
--
-- The predicate below mirrors that floor exactly, so the planner still matches
-- the query. Its sibling in the same table, idx_public_property_address
-- (0050:231), carries WHERE address IS NOT NULL for the same reason.
--
-- ⚠ OPERATOR NOTE — this migration takes a write lock.
-- run_migrations.py wraps each file in one transaction, and CREATE INDEX
-- CONCURRENTLY cannot run inside a transaction, so this build holds a ShareLock
-- on public_property_records and blocks every INSERT/UPDATE/DELETE for its
-- duration. On dev that is ~90s. On a production table of this size, run the
-- two statements by hand outside the runner instead:
--
--     CREATE INDEX CONCURRENTLY idx_public_property_owner_lookup ON ...;
--     DROP INDEX CONCURRENTLY idx_public_property_owner_normalized;
--
-- and then record this file with `run_migrations.py --reconcile`. The same
-- caveat applies to 0086, whose two builds have already been applied here.
-- Written down rather than assumed: the app pool's command_timeout is 30s
-- (db/connection.py:362), so a lock held longer than that surfaces as the same
-- unlabelled TimeoutError() in automation_jobs that 0086 exists to eliminate.

CREATE INDEX IF NOT EXISTS idx_public_property_owner_lookup
    ON public_property_records (
        (regexp_replace(lower(COALESCE(owner_name, '')), '[^a-z0-9]', '', 'g')),
        record_refreshed_at DESC
    )
 WHERE length(regexp_replace(lower(COALESCE(owner_name, '')), '[^a-z0-9]', '', 'g')) >= 5;

-- Superseded by the partial index above. Dropped in the same transaction so the
-- lookup is never without an index to use.
DROP INDEX IF EXISTS idx_public_property_owner_normalized;
