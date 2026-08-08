-- Our AI Sales: contact state, SMS commands, immutable Smart Plans, and
-- short-lived browser-dialer intents. All private rows are tenant-scoped and
-- protected by forced RLS. No table stores a dialer's destination in plaintext.

BEGIN;

ALTER TABLE agent_contacts ADD COLUMN IF NOT EXISTS state_code char(2);
ALTER TABLE agent_contacts DROP CONSTRAINT IF EXISTS agent_contacts_state_code_chk;
ALTER TABLE agent_contacts ADD CONSTRAINT agent_contacts_state_code_chk
    CHECK (state_code IS NULL OR state_code ~ '^[A-Z]{2}$');

ALTER TABLE command_executions
    DROP CONSTRAINT IF EXISTS command_executions_command_type_check;
ALTER TABLE command_executions
    DROP CONSTRAINT IF EXISTS command_executions_command_type_chk;
ALTER TABLE command_executions
    ADD CONSTRAINT command_executions_command_type_chk
    CHECK (command_type IN ('EMAIL','SMS','CALL','CALENDAR'));

ALTER TABLE provider_credentials
    ADD COLUMN IF NOT EXISTS validation_status text NOT NULL DEFAULT 'unverified',
    ADD COLUMN IF NOT EXISTS validation_error text,
    ADD COLUMN IF NOT EXISTS validated_capabilities jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE provider_credentials
    DROP CONSTRAINT IF EXISTS provider_credentials_validation_status_chk;
ALTER TABLE provider_credentials
    ADD CONSTRAINT provider_credentials_validation_status_chk
    CHECK (validation_status IN ('unverified','valid','invalid','expired'));

ALTER TABLE live_call_sessions ADD COLUMN IF NOT EXISTS contact_id uuid;
ALTER TABLE live_call_sessions DROP CONSTRAINT IF EXISTS chk_call_anchor;
ALTER TABLE live_call_sessions ADD CONSTRAINT chk_call_anchor
    CHECK (contact_id IS NOT NULL OR client_id IS NOT NULL OR lead_id IS NOT NULL);
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'live_call_sessions_tenant_contact_fk'
    ) THEN
        ALTER TABLE live_call_sessions
            ADD CONSTRAINT live_call_sessions_tenant_contact_fk
            FOREIGN KEY (tenant_id, contact_id)
            REFERENCES agent_contacts (tenant_id, id)
            ON DELETE SET NULL (contact_id);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_command_executions_tenant_id
    ON command_executions (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_automation_jobs_tenant_id
    ON automation_jobs (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_tasks_tenant_id
    ON client_tasks (tenant_id, id);

CREATE TABLE IF NOT EXISTS smart_plans (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_agent_id      text NOT NULL,
    name                text NOT NULL,
    description         text NOT NULL DEFAULT '',
    draft_definition    jsonb NOT NULL DEFAULT '{"steps":[]}'::jsonb,
    scope               text NOT NULL DEFAULT 'personal',
    status              text NOT NULL DEFAULT 'draft',
    current_revision_id uuid,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT smart_plans_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT smart_plans_name_chk CHECK (length(name) BETWEEN 1 AND 160),
    CONSTRAINT smart_plans_definition_chk CHECK (jsonb_typeof(draft_definition) = 'object'),
    CONSTRAINT smart_plans_scope_chk CHECK (scope IN ('personal','team')),
    CONSTRAINT smart_plans_status_chk CHECK (status IN ('draft','published','archived'))
);

CREATE TABLE IF NOT EXISTS smart_plan_revisions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id         uuid NOT NULL,
    revision_number integer NOT NULL,
    definition      jsonb NOT NULL,
    definition_hash char(64) NOT NULL,
    published_at    timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT smart_plan_revisions_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT smart_plan_revision_number_key UNIQUE (tenant_id, plan_id, revision_number),
    CONSTRAINT smart_plan_revisions_plan_fk FOREIGN KEY (tenant_id, plan_id)
        REFERENCES smart_plans (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_revisions_number_chk CHECK (revision_number > 0),
    CONSTRAINT smart_plan_revisions_definition_chk CHECK (jsonb_typeof(definition) = 'object'),
    CONSTRAINT smart_plan_revisions_hash_chk CHECK (definition_hash ~ '^[0-9a-f]{64}$')
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'smart_plans_current_revision_fk'
    ) THEN
        ALTER TABLE smart_plans
            ADD CONSTRAINT smart_plans_current_revision_fk
            FOREIGN KEY (tenant_id, current_revision_id)
            REFERENCES smart_plan_revisions (tenant_id, id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS smart_plan_enrollments (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id             uuid NOT NULL,
    revision_id         uuid NOT NULL,
    contact_id          uuid NOT NULL,
    status              text NOT NULL DEFAULT 'active',
    current_step_index  integer NOT NULL DEFAULT 0,
    next_run_at         timestamptz,
    preview_hash        char(64) NOT NULL,
    resume_count        integer NOT NULL DEFAULT 0,
    created_by          text NOT NULL,
    paused_at           timestamptz,
    completed_at        timestamptz,
    cancelled_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT smart_plan_enrollments_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT smart_plan_enrollments_plan_fk FOREIGN KEY (tenant_id, plan_id)
        REFERENCES smart_plans (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_enrollments_revision_fk FOREIGN KEY (tenant_id, revision_id)
        REFERENCES smart_plan_revisions (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_enrollments_contact_fk FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_enrollments_status_chk CHECK (
        status IN ('active','paused','completed','cancelled','blocked')
    ),
    CONSTRAINT smart_plan_enrollments_step_chk CHECK (current_step_index >= 0),
    CONSTRAINT smart_plan_enrollments_resume_chk CHECK (resume_count >= 0),
    CONSTRAINT smart_plan_enrollments_preview_chk CHECK (preview_hash ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_smart_plan_active_contact
    ON smart_plan_enrollments (tenant_id, plan_id, contact_id)
    WHERE status IN ('active','paused');

CREATE TABLE IF NOT EXISTS smart_plan_step_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    enrollment_id   uuid NOT NULL,
    step_key        text NOT NULL,
    step_index      integer NOT NULL,
    step_type       text NOT NULL,
    scheduled_for   timestamptz NOT NULL,
    state           text NOT NULL DEFAULT 'scheduled',
    command_id      uuid,
    task_id         uuid,
    job_id          uuid,
    blocker         text,
    last_error      text,
    attempt_count   integer NOT NULL DEFAULT 0,
    started_at      timestamptz,
    finished_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT smart_plan_step_runs_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT smart_plan_step_runs_enrollment_key UNIQUE (tenant_id, enrollment_id, step_key),
    CONSTRAINT smart_plan_step_runs_enrollment_fk FOREIGN KEY (tenant_id, enrollment_id)
        REFERENCES smart_plan_enrollments (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_step_runs_command_fk FOREIGN KEY (tenant_id, command_id)
        REFERENCES command_executions (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_step_runs_task_fk FOREIGN KEY (tenant_id, task_id)
        REFERENCES client_tasks (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_step_runs_job_fk FOREIGN KEY (tenant_id, job_id)
        REFERENCES automation_jobs (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT smart_plan_step_runs_index_chk CHECK (step_index >= 0),
    CONSTRAINT smart_plan_step_runs_type_chk CHECK (
        step_type IN ('wait','task','email','sms','approved_call')
    ),
    CONSTRAINT smart_plan_step_runs_state_chk CHECK (
        state IN ('scheduled','paused','running','awaiting_approval','succeeded',
                  'blocked','failed','skipped','cancelled')
    ),
    CONSTRAINT smart_plan_step_runs_attempt_chk CHECK (attempt_count >= 0)
);

CREATE TABLE IF NOT EXISTS agent_call_intents (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id            text NOT NULL,
    contact_id          uuid NOT NULL,
    state               text NOT NULL DEFAULT 'prepared',
    expires_at          timestamptz NOT NULL,
    provider_call_sid   text,
    failure_reason      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    authorized_at       timestamptz,
    completed_at        timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_call_intents_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT agent_call_intents_contact_fk FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT agent_call_intents_state_chk CHECK (
        state IN ('prepared','authorized','ringing','in_progress','completed',
                  'failed','cancelled','expired')
    ),
    CONSTRAINT agent_call_intents_expiry_chk CHECK (expires_at > created_at),
    CONSTRAINT agent_call_intents_sid_chk CHECK (
        provider_call_sid IS NULL OR provider_call_sid ~ '^CA[0-9A-Fa-f]{32}$'
    )
);
CREATE INDEX IF NOT EXISTS idx_agent_call_intents_agent
    ON agent_call_intents (tenant_id, agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_call_intents_expiry
    ON agent_call_intents (expires_at)
    WHERE state = 'prepared';

CREATE INDEX IF NOT EXISTS idx_smart_plans_owner
    ON smart_plans (tenant_id, owner_agent_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_smart_plan_enrollments_queue
    ON smart_plan_enrollments (tenant_id, status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_smart_plan_step_runs_queue
    ON smart_plan_step_runs (tenant_id, state, scheduled_for);

DROP TRIGGER IF EXISTS trg_smart_plans_updated ON smart_plans;
CREATE TRIGGER trg_smart_plans_updated BEFORE UPDATE ON smart_plans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_smart_plan_enrollments_updated ON smart_plan_enrollments;
CREATE TRIGGER trg_smart_plan_enrollments_updated BEFORE UPDATE ON smart_plan_enrollments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_smart_plan_step_runs_updated ON smart_plan_step_runs;
CREATE TRIGGER trg_smart_plan_step_runs_updated BEFORE UPDATE ON smart_plan_step_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_agent_call_intents_updated ON agent_call_intents;
CREATE TRIGGER trg_agent_call_intents_updated BEFORE UPDATE ON agent_call_intents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION prevent_smart_plan_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'smart plan revisions are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_smart_plan_revisions_immutable ON smart_plan_revisions;
CREATE TRIGGER trg_smart_plan_revisions_immutable
    BEFORE UPDATE OR DELETE ON smart_plan_revisions
    FOR EACH ROW EXECUTE FUNCTION prevent_smart_plan_revision_mutation();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'smart_plans','smart_plan_revisions','smart_plan_enrollments',
        'smart_plan_step_runs','agent_call_intents'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I',
            table_name || '_tenant_isolation', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I '
            'USING (app_is_platform_admin() OR tenant_id = app_current_tenant()) '
            'WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant())',
            table_name || '_tenant_isolation', table_name
        );
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON
    smart_plans, smart_plan_enrollments, smart_plan_step_runs, agent_call_intents
    TO oracle_app;
GRANT SELECT, INSERT ON smart_plan_revisions TO oracle_app;
REVOKE DELETE, TRUNCATE ON
    smart_plans, smart_plan_revisions, smart_plan_enrollments,
    smart_plan_step_runs, agent_call_intents
    FROM oracle_app;

COMMIT;
