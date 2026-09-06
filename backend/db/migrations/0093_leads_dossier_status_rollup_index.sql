-- 0093 — get_team_pipeline timed out for every broker who asked
--
-- The AI tool `get_team_pipeline` (ai_chat_store.py:924) aggregates the whole
-- leads table for a tenant:
--
--     SELECT dossier_status, count(*), count(*) FILTER (contract_expires_at ...)
--       FROM leads WHERE tenant_id=$1 GROUP BY dossier_status
--
-- Measured on an idle database: 13,294ms over 8.5M rows, reading 2,495,113
-- pages. The pool's command_timeout is 30s (db/connection.py), so under any
-- real load it raised a bare TimeoutError and the model received a failure
-- receipt instead of an answer. Caught by running an actual AI chat turn, not
-- by reading the code.
--
-- lead_pipeline_counts already exists for "never count leads directly", but it
-- keys on `state`, not `dossier_status`, so it is not a drop-in here.
--
-- INCLUDE (contract_expires_at) is what makes this an INDEX ONLY scan rather
-- than an index scan plus 2.5M heap fetches: the FILTER needs that column and
-- carrying it in the leaf pages keeps the heap out of the plan entirely.
--
--     before   Parallel Seq Scan          13,294ms
--     after    Parallel Index Only Scan    2,017ms
--
-- ⚠ This index is USELESS until the table has statistics and a visibility map.
-- Both were absent here — `leads` had never been vacuumed or analyzed, so
-- n_live_tup read 0 and only 42% of pages were all-visible, and the planner
-- chose the sequential scan even with the index present. See the ANALYZE step
-- in infra/scripts/finish-deploy.sh; on a fresh production load it is not
-- optional.

CREATE INDEX IF NOT EXISTS idx_leads_tenant_dossier_status
    ON leads (tenant_id, dossier_status)
    INCLUDE (contract_expires_at);
