-- 0094 — list_deals timed out too, for the same reason as 0093
--
-- The AI tool `list_deals` (ai_chat_store.py:826) asks for a tenant's 50 most
-- contract-urgent leads:
--
--     ... FROM leads l WHERE l.tenant_id=$1
--     ORDER BY l.contract_expires_at ASC NULLS LAST, l.updated_at DESC LIMIT 50
--
-- With no index matching that ordering, PostgreSQL must sort 8.5M rows to
-- return 50, which exceeded the pool's 30s command_timeout and handed the model
-- a failure receipt. Found by running a real AI chat turn after 0093 fixed
-- get_team_pipeline — the same turn simply failed one tool later.
--
-- The index column order mirrors the ORDER BY exactly, NULLS LAST included:
-- an index whose null ordering differs cannot serve the sort, and the planner
-- silently falls back to the full sort it was meant to remove.
--
--     before   Sort over 8.5M rows        > 30,000ms (timeout)
--     after    Index Scan, 50 rows              2.7ms
--
-- The optional state/dossier_status filters stay as residual predicates. They
-- are bound parameters in an `($2 IS NULL OR col=$2)` pattern, which cannot
-- drive an index anyway, and they do not need to: the LIMIT is satisfied from
-- the ordered index after reading a few rows.
--
-- Like 0093, useless without table statistics — run_migrations.py now seeds
-- those for any table that has none.

CREATE INDEX IF NOT EXISTS idx_leads_tenant_contract_window
    ON leads (tenant_id, contract_expires_at ASC NULLS LAST, updated_at DESC);
