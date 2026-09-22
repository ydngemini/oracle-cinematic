-- 0103 — make "which rows still need owner_name_normalized?" free
--
-- Same shape as 0086's idx_leads_pending_payload_normalization: the batched
-- backfill in backfill_owner_name_normalized.py asks
--     SELECT id FROM public_property_records
--      WHERE owner_name_normalized IS NULL
--      ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED
-- every batch, and with 9.8M rows almost entirely NULL right after 0102, that
-- is a full sequential scan every single call — measured: the plain dry-run
-- `count(*) ... WHERE owner_name_normalized IS NULL` alone blew the pool's
-- command_timeout=30 as a bare TimeoutError() before the backfill script
-- could run its first batch.
--
-- `IS NULL` on a plain column (not a derived expression) is provable from
-- the index predicate below by the planner's own predicate_implied_by, unlike
-- the earlier mistake 0089/0090 already fought (a size check on a derived
-- expression, which that predicate machinery cannot fold a bound constant
-- through) — so this one actually works, and self-shrinks to nothing as the
-- backfill (and the 0102 trigger, for new/updated rows) fills the column in.
-- Once empty, this index costs a few KB forever and answers "is there work
-- left?" instantly.

CREATE INDEX IF NOT EXISTS idx_public_property_owner_backfill_pending
    ON public_property_records (id)
 WHERE owner_name_normalized IS NULL;
