-- 0074 — ai_decision_traces
--
-- Captures the human judgement applied to machine proposals, so that a
-- preference/reward corpus exists later. Today every one of these signals is
-- produced and discarded: `action_approvals` records that a decision happened
-- but is mutated in place, so the *pairing* of "what was proposed" against
-- "what the human actually wanted" is not recoverable once the row is updated.
--
-- Three properties this table exists to guarantee:
--
--   1. proposal and final are stored SEPARATELY, each with its own digest.
--      Training supervised on an accepted-unchanged proposal means training a
--      model on its own output as ground truth. `signal` plus the two digests
--      is what makes that filterable rather than a judgement call at export.
--
--   2. Reward arrives LATE. An offer is accepted weeks after it is drafted; a
--      deal closes months after. The outcome_* columns are therefore nullable
--      and written long after insert. Everything else is immutable (see the
--      trigger below) so a trace cannot be rewritten to agree with its outcome.
--
--   3. Tenant isolation. These rows contain client-identifying draft payloads,
--      exactly as `action_approvals.draft_payload` already does — this adds no
--      new exposure class, and carries the same RLS. Redaction happens at
--      dataset-build time (reusing redact_pii), not here: redacting at capture
--      would destroy the fidelity the reward signal is measured against.

BEGIN;

CREATE TABLE IF NOT EXISTS ai_decision_traces (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Who decided, and what produced the thing they decided on.
    agent_id            text NOT NULL,
    surface             text NOT NULL CHECK (surface IN
                            ('approval','chat_action','stage_override')),
    action_type         text NOT NULL,
    risk_class          text,
    -- Registry version of the model that produced the proposal, or 'base:<name>'
    -- when none was active. NULL means "not recorded" and must never be read as
    -- "base answered" — that is the same distinction model_resolver enforces.
    model_version       text,

    -- Where this came from, so a trace is auditable back to the live record.
    source_table        text NOT NULL,
    source_id           uuid NOT NULL,

    -- The pair. `final` is NULL unless the human changed something.
    proposal            jsonb NOT NULL,
    proposal_sha256     char(64) NOT NULL CHECK (proposal_sha256 ~ '^[0-9a-f]{64}$'),
    final               jsonb,
    final_sha256        char(64) CHECK (final_sha256 ~ '^[0-9a-f]{64}$'),

    -- The label. Derived at capture, never free text.
    signal              text NOT NULL CHECK (signal IN
                            ('accepted_unchanged','edited','rejected','expired')),
    decided_at          timestamptz NOT NULL,
    -- Wall-clock the human took to decide. A weak confidence proxy, not a
    -- quality measure: a fast approval may mean obvious, or may mean unread.
    decision_latency_ms integer CHECK (decision_latency_ms IS NULL OR decision_latency_ms >= 0),

    -- Late-binding reward. Written long after the row is inserted.
    outcome_kind        text,
    outcome_value       numeric,
    outcome_at          timestamptz,
    outcome_source      text,

    -- Withdrawal, matching style_training_examples' semantics.
    consent_version     text,
    revoked_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),

    -- One trace per decided record. Re-deciding is impossible upstream
    -- (decide_approval refuses a non-pending approval), so a duplicate here
    -- means a capture bug, not a legitimate second decision.
    UNIQUE (tenant_id, source_table, source_id),

    -- The self-poisoning discriminator, enforced rather than trusted.
    CONSTRAINT chk_trace_signal_shape CHECK (
        (signal = 'edited'
            AND final IS NOT NULL
            AND final_sha256 IS NOT NULL
            AND final_sha256 <> proposal_sha256)
        OR (signal IN ('accepted_unchanged','rejected','expired')
            AND final IS NULL
            AND final_sha256 IS NULL)
    ),

    -- An outcome is a fact with a time and a provenance, or it is absent.
    CONSTRAINT chk_trace_outcome_shape CHECK (
        (outcome_kind IS NULL AND outcome_at IS NULL AND outcome_source IS NULL
         AND outcome_value IS NULL)
        OR (outcome_kind IS NOT NULL AND outcome_at IS NOT NULL
            AND outcome_source IS NOT NULL)
    )
);

-- Dataset assembly reads by tenant + signal, and the reward join reads the
-- rows still awaiting an outcome.
CREATE INDEX IF NOT EXISTS idx_traces_tenant_signal
    ON ai_decision_traces (tenant_id, signal, decided_at DESC)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_traces_awaiting_outcome
    ON ai_decision_traces (tenant_id, decided_at)
    WHERE outcome_kind IS NULL AND revoked_at IS NULL;

-- Append-only in everything but reward and revocation.
--
-- A training corpus whose labels can be edited after the fact is not evidence.
-- Enforced in the database because the guarantee has to hold against any
-- writer, including a future one that has not read this file.
CREATE OR REPLACE FUNCTION ai_decision_traces_immutable()
RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.surface IS DISTINCT FROM OLD.surface
       OR NEW.action_type IS DISTINCT FROM OLD.action_type
       OR NEW.source_table IS DISTINCT FROM OLD.source_table
       OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.proposal IS DISTINCT FROM OLD.proposal
       OR NEW.proposal_sha256 IS DISTINCT FROM OLD.proposal_sha256
       OR NEW.final IS DISTINCT FROM OLD.final
       OR NEW.final_sha256 IS DISTINCT FROM OLD.final_sha256
       OR NEW.signal IS DISTINCT FROM OLD.signal
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
    THEN
        RAISE EXCEPTION
            'ai_decision_traces is append-only; only outcome_* and revoked_at may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_decision_traces_immutable ON ai_decision_traces;
CREATE TRIGGER trg_ai_decision_traces_immutable
    BEFORE UPDATE ON ai_decision_traces
    FOR EACH ROW EXECUTE FUNCTION ai_decision_traces_immutable();

ALTER TABLE ai_decision_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_decision_traces FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'ai_decision_traces_tenant_isolation'
    ) THEN
        CREATE POLICY ai_decision_traces_tenant_isolation ON ai_decision_traces
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;

-- Grants, and the REVOKE that makes "append-only" true rather than aspirational.
--
-- 0001/0003 grant the app role blanket DML across the schema, so without an
-- explicit REVOKE this table would allow DELETE — and the trigger above only
-- guards UPDATE. A row that can be deleted outright is not append-only, and a
-- training corpus that can lose rows silently cannot be audited against a model
-- that was trained on them. `audit_ledger` sets the precedent here: INSERT and
-- SELECT only.
--
-- Withdrawal still works: it is an UPDATE setting revoked_at, and dataset
-- assembly filters on it. Tenant deletion also still works — referential
-- actions from the tenants FK run with the table owner's privileges, not the
-- app role's.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON ai_decision_traces TO oracle_app';
        EXECUTE 'REVOKE DELETE ON ai_decision_traces FROM oracle_app';
    END IF;
END $$;

COMMENT ON TABLE ai_decision_traces IS
    'Human judgement on machine proposals, kept as a preference/reward corpus. '
    'Append-only apart from late-binding outcome_* and revoked_at.';

COMMENT ON COLUMN ai_decision_traces.signal IS
    'accepted_unchanged is a POSITIVE reward label but is NOT valid supervised '
    'training data — the proposal is the model''s own output, so fine-tuning on '
    'it teaches the model to agree with itself. edited yields a preference pair '
    '(final chosen over proposal); rejected is a negative.';

COMMENT ON COLUMN ai_decision_traces.final IS
    'What the human actually wanted, present only when they changed the draft. '
    'NULL for accepted_unchanged means "identical to proposal", never "unknown".';

COMMENT ON COLUMN ai_decision_traces.outcome_value IS
    'Reward, attached long after the decision. Deliberately NOT constrained to a '
    'range: outcome_kind decides the units, and forcing everything into [0,1] at '
    'write time would discard the raw figure a later re-scaling needs.';

COMMENT ON COLUMN ai_decision_traces.model_version IS
    'Registry version that produced the proposal, or base:<name>. NULL means not '
    'recorded — never read it as "the base model answered".';

COMMIT;
