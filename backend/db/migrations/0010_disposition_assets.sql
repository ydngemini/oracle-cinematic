-- ===========================================================================
-- 0010_disposition_assets.sql
-- Oracle — storage for AI-generated disposition marketing assets (client-
-- management roadmap, "Temporal Listing Safeguards" — the worker half).
--
-- The disposition enforcer (backend/disposition_enforcer.py) sweeps
-- lead_contract_status (0007) hourly; when a contract enters the danger zone
-- it generates email/IG marketing copy via Bedrock, stores it here, and
-- transitions dossier_status 'under_contract' → 'marketing'. The status
-- transition itself is the idempotency guard — no flag column needed to stop
-- regeneration, because the sweep only ever selects 'under_contract' rows.
--
-- Columns inherit the leads table's existing RLS policies and grants;
-- nothing new to enable here.
-- ===========================================================================

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS marketing_payload      jsonb,
    ADD COLUMN IF NOT EXISTS marketing_generated_at timestamptz;

COMMENT ON COLUMN leads.marketing_payload IS
    'AI-generated disposition assets: {"email_subject", "email_body", "ig_caption"} — written by disposition_enforcer.py';
