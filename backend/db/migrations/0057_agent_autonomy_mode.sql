-- Make Neoh's autonomy posture explicit while preserving non-bypassable gates.

BEGIN;

ALTER TABLE agent_ai_settings
    ADD COLUMN IF NOT EXISTS autonomy_mode text NOT NULL DEFAULT 'policy_autopilot';

ALTER TABLE agent_ai_settings
    DROP CONSTRAINT IF EXISTS agent_ai_settings_autonomy_mode_check;
ALTER TABLE agent_ai_settings
    ADD CONSTRAINT agent_ai_settings_autonomy_mode_check CHECK (
        autonomy_mode IN ('policy_autopilot','full_autonomy')
    );

-- These invariants existed before autonomy_mode and remain deliberately
-- non-configurable. "Full autonomy" never means bypassing external-action,
-- live-call, or legal-document approval.
ALTER TABLE agent_ai_settings
    DROP CONSTRAINT IF EXISTS agent_ai_settings_outreach_requires_approval_check;
ALTER TABLE agent_ai_settings
    ADD CONSTRAINT agent_ai_settings_outreach_requires_approval_check
        CHECK (outreach_requires_approval);
ALTER TABLE agent_ai_settings
    DROP CONSTRAINT IF EXISTS agent_ai_settings_calls_require_approval_check;
ALTER TABLE agent_ai_settings
    ADD CONSTRAINT agent_ai_settings_calls_require_approval_check
        CHECK (calls_require_approval);
ALTER TABLE agent_ai_settings
    DROP CONSTRAINT IF EXISTS agent_ai_settings_legal_requires_approval_check;
ALTER TABLE agent_ai_settings
    ADD CONSTRAINT agent_ai_settings_legal_requires_approval_check
        CHECK (legal_requires_approval);

COMMENT ON COLUMN agent_ai_settings.autonomy_mode IS
    'policy_autopilot is the default; full_autonomy expands internal work only and cannot bypass consent, DNC, Fair Housing, spend, call, or legal approvals.';

COMMIT;
