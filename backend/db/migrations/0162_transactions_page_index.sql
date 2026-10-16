-- ---------------------------------------------------------------------------
-- 0162 — the deal list pages by key, newest first (prompt 16, T16.05,
-- AC16.03; ledger L16-04).
--
-- portfolio_api.list_transactions used to LIMIT/OFFSET over a join of every
-- matching deal with clients and offers, ordered (updated_at DESC, id). It now
-- pages deal ids first by the keyset (updated_at, id) and joins only that
-- page. This index serves the keyset under the tenant wall: the tenant column
-- leads (0141's wall is an index condition on it), then the page order.
-- idx_transactions_workflow (tenant_id, status, updated_at DESC) still serves
-- the status-filtered list.
--
-- Small per brokerage (deals); a plain CREATE INDEX holds its lock for
-- milliseconds at this size.
--
-- Previous release on this schema: unaffected (an index).
--
-- Rollback: DROP INDEX idx_transactions_page.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_transactions_page
    ON transactions (tenant_id, updated_at DESC, id DESC);
