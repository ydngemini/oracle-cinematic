-- 0135: client reconciliation records whether each property source was checked.
--
-- Audit A02 / ledger L11-02 (docs/claude-prompts/LEDGER.md): the public-record
-- property-candidate lookup caught every error and stored `[]`, so a lookup
-- that timed out or failed was indistinguishable from a verified zero. The
-- per-source status now lives beside the candidates, in the CONTRACT C6
-- vocabulary: checked | timed_out | unavailable | not_applicable.
--
--   source_status = {
--     "public_property_records": {
--       "status": "checked" | "timed_out" | "unavailable" | "not_applicable",
--       "required": true,
--       "reason": stable code or short text, never exception or CRM text,
--       "checked_at": ISO time of THIS pass's successful check, or null,
--       "last_checked_at": ISO time of the most recent successful check, or null,
--       "matched": count of candidates the check returned (null unless checked)
--     }
--   }
--
-- Existing rows default to '{}' (no status recorded); readers treat a missing
-- source as "not recorded", never as checked. No new function, so no grant is
-- needed: 0053 already grants SELECT, INSERT, UPDATE on client_ai_state to
-- oracle_app, at table level (not column level), which covers this column.
--
-- Rollback: ALTER TABLE client_ai_state DROP CONSTRAINT IF EXISTS
-- client_ai_state_source_status_chk; ALTER TABLE client_ai_state DROP COLUMN
-- IF EXISTS source_status;  (the application code must be rolled back first,
-- because it writes the column.)

ALTER TABLE client_ai_state
    ADD COLUMN IF NOT EXISTS source_status jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'client_ai_state_source_status_chk') THEN
        ALTER TABLE client_ai_state
            ADD CONSTRAINT client_ai_state_source_status_chk
            CHECK (jsonb_typeof(source_status) = 'object');
    END IF;
END $$;
