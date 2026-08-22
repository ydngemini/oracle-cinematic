-- Give compliance_checklist_items an idempotency key so it can have a writer.
--
-- 0013 created the table and GET /api/compliance/checklist/{transaction_id}
-- reads it, but nothing has ever written a row — the endpoint has always
-- reported total_items: 0. Its sibling's docstring even names the route that
-- would materialise one (`POST /api/compliance/checklist`), and that route does
-- not exist anywhere in the codebase.
--
-- Building the writer needs a conflict target: materialising a checklist must
-- be safe to re-run when a transaction's details change (a year_built is
-- corrected, a flood-zone flag flips) without resetting the delivery/signature
-- state an agent has already recorded against items that were required before
-- and still are. ON CONFLICT DO NOTHING against this key gives exactly that.
--
-- Depends on 0013 (compliance_checklist_items).

BEGIN;

-- Deduplicate first: without a key nothing prevented repeats, and the index
-- cannot be created while any exist. Keeps the oldest row per pair, which is
-- the one carrying any recorded delivery or signature history.
DELETE FROM compliance_checklist_items a
      USING compliance_checklist_items b
      WHERE a.transaction_id = b.transaction_id
        AND a.disclosure_id  = b.disclosure_id
        AND a.created_at     > b.created_at;

CREATE UNIQUE INDEX IF NOT EXISTS uq_cci_txn_disclosure
    ON compliance_checklist_items (transaction_id, disclosure_id);

COMMENT ON INDEX uq_cci_txn_disclosure IS
    'Conflict target for POST /api/compliance/checklist. Re-materialising a '
    'checklist must not duplicate items nor reset tracked status on existing ones.';

COMMIT;
