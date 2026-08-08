-- Automatic, evidence-backed CRM stewardship for every tenant client.
-- Model output is advisory; deterministic policy owns durable score/stage writes.

BEGIN;

CREATE TABLE IF NOT EXISTS client_ai_state (
    client_id              uuid PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
    tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    enabled                boolean NOT NULL DEFAULT true,
    status                 text NOT NULL DEFAULT 'queued',
    score_mode             text NOT NULL DEFAULT 'auto',
    stage_mode             text NOT NULL DEFAULT 'auto',
    score_version          text,
    score_breakdown        jsonb NOT NULL DEFAULT '[]'::jsonb,
    normalized_preferences jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary                text,
    next_actions           jsonb NOT NULL DEFAULT '[]'::jsonb,
    data_gaps              jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence               jsonb NOT NULL DEFAULT '[]'::jsonb,
    property_candidates    jsonb NOT NULL DEFAULT '[]'::jsonb,
    model_id               text,
    input_fingerprint      char(64),
    last_evaluated_at      timestamptz,
    last_error_code        text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT client_ai_state_status_chk CHECK (
        status IN ('queued','running','complete','degraded','failed','disabled')
    ),
    CONSTRAINT client_ai_state_score_mode_chk CHECK (score_mode IN ('auto','manual')),
    CONSTRAINT client_ai_state_stage_mode_chk CHECK (stage_mode IN ('auto','manual')),
    CONSTRAINT client_ai_state_tenant_client_key UNIQUE (tenant_id, client_id),
    CONSTRAINT client_ai_state_json_chk CHECK (
        jsonb_typeof(score_breakdown) = 'array'
        AND jsonb_typeof(normalized_preferences) = 'object'
        AND jsonb_typeof(next_actions) = 'array'
        AND jsonb_typeof(data_gaps) = 'array'
        AND jsonb_typeof(evidence) = 'array'
        AND jsonb_typeof(property_candidates) = 'array'
    )
);

CREATE INDEX IF NOT EXISTS idx_client_ai_state_queue
    ON client_ai_state (tenant_id, status, updated_at)
    WHERE enabled;

DROP TRIGGER IF EXISTS trg_client_ai_state_updated ON client_ai_state;
CREATE TRIGGER trg_client_ai_state_updated
    BEFORE UPDATE ON client_ai_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE client_ai_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_ai_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS client_ai_state_tenant_isolation ON client_ai_state;
CREATE POLICY client_ai_state_tenant_isolation ON client_ai_state
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE ON client_ai_state TO oracle_app;
REVOKE DELETE, TRUNCATE ON client_ai_state FROM oracle_app;

-- The migration transaction performs a cross-tenant backfill after FORCE RLS.
-- Use the same transaction-local platform role convention as migration 0050;
-- the setting is discarded when the migration transaction commits.
SELECT set_config('app.current_role', 'platform_admin', true);

INSERT INTO client_ai_state (client_id, tenant_id, status)
SELECT id, tenant_id, 'queued'
  FROM clients
 WHERE archived_at IS NULL
ON CONFLICT (client_id) DO NOTHING;

-- Queue an idempotent initial pass. The application handler is registered before
-- durable workers start, so these rows are safe to create during the migration.
INSERT INTO automation_jobs (
    tenant_id, job_type, queue_name, state, risk_class, payload, priority,
    max_attempts, idempotency_key, created_by
)
SELECT c.tenant_id,
       'crm:client_reconcile',
       'default',
       'queued',
       'internal_edit',
       jsonb_build_object(
           'tenant_id', c.tenant_id::text,
           'client_id', c.id::text,
           'requested_by', 'migration-0053',
           'reason', 'initial_backfill'
       ),
       45,
       5,
       'client-ai:backfill:v1:' || c.id::text,
       'migration-0053'
  FROM clients c
 WHERE c.archived_at IS NULL
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

COMMIT;
