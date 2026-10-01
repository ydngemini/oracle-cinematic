-- ---------------------------------------------------------------------------
-- 0116 — an index the MLS search can actually use for its ORDER BY.
--
-- GET /api/mls/search orders a state's listings by
--     last_updated DESC, list_price DESC, mls_id, mls_number
-- and nothing matched that: idx_oml_last_updated is (last_updated DESC) only,
-- and the query also said NULLS LAST on NOT NULL columns, which the planner
-- cannot reconcile with a DESC (NULLS FIRST) index. Every search therefore
-- parallel-seq-scanned and sorted the whole state's feed — 150 ms idle, and
-- page-1 p95 2.2 s with 25 agents searching (Mission 8, ~152k TX listings).
--
-- state_code leads because the UI always searches within a state; the tail
-- makes the order total, so OFFSET pagination walks the index and stops.
-- The runner builds CREATE INDEX CONCURRENTLY outside the migration
-- transaction (run_migrations.py), so this takes no write lock on the feed.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_oml_search_order
    ON oracle_mls_listings (state_code, last_updated DESC, list_price DESC, mls_id, mls_number);
