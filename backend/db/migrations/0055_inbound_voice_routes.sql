-- Tenant-safe inbound Twilio routes and Qwen intake-call state.
-- Depends on 0054_contact_truth.sql for agent_contacts and clients.contact_id.

BEGIN;

CREATE TABLE IF NOT EXISTS telephony_routes (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id                 text NOT NULL,
    endpoint_key             uuid NOT NULL DEFAULT gen_random_uuid(),
    inbound_did              text NOT NULL,
    twilio_account_sid       char(34) NOT NULL,
    intake_mode              text NOT NULL DEFAULT 'auto',
    forwarding_mode          text NOT NULL DEFAULT 'none',
    forwarding_source_e164   text,
    sip_domain               text,
    voice_caller_id_e164     text,
    voice_caller_id_verified boolean NOT NULL DEFAULT false,
    sms_sender_e164          text,
    sms_sender_type          text,
    active                   boolean NOT NULL DEFAULT true,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT telephony_routes_tenant_agent_key UNIQUE (tenant_id, agent_id),
    CONSTRAINT telephony_routes_endpoint_key UNIQUE (endpoint_key),
    CONSTRAINT telephony_routes_did_chk CHECK (inbound_did ~ '^\+[1-9][0-9]{7,14}$'),
    CONSTRAINT telephony_routes_account_chk CHECK (twilio_account_sid ~ '^AC[0-9A-Fa-f]{32}$'),
    CONSTRAINT telephony_routes_intake_chk CHECK (intake_mode IN ('buyer','seller','auto')),
    CONSTRAINT telephony_routes_forwarding_chk CHECK (
        forwarding_mode IN ('none','carrier_conditional','sip')
        AND (forwarding_source_e164 IS NULL OR forwarding_source_e164 ~ '^\+[1-9][0-9]{7,14}$')
        AND (voice_caller_id_e164 IS NULL OR voice_caller_id_e164 ~ '^\+[1-9][0-9]{7,14}$')
        AND (
            forwarding_mode <> 'sip'
            OR (sip_domain IS NOT NULL AND sip_domain ~ '^[A-Za-z0-9.-]{1,253}$')
        )
    ),
    CONSTRAINT telephony_routes_voice_caller_id_chk CHECK (
        NOT voice_caller_id_verified OR voice_caller_id_e164 IS NOT NULL
    ),
    -- Twilio verified caller IDs are valid for outbound voice only.  SMS must
    -- identify a registered/ported sender class and can never inherit the
    -- voice-only verification flag.
    CONSTRAINT telephony_routes_sms_sender_chk CHECK (
        (sms_sender_e164 IS NULL AND sms_sender_type IS NULL)
        OR (
            sms_sender_e164 ~ '^\+[1-9][0-9]{7,14}$'
            AND sms_sender_type IN (
                'twilio_registered','ported','toll_free_verified'
            )
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_routes_active_did
    ON telephony_routes (inbound_did) WHERE active;
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_routes_tenant_id
    ON telephony_routes (tenant_id, id);
CREATE INDEX IF NOT EXISTS idx_telephony_routes_tenant
    ON telephony_routes (tenant_id, active, agent_id);

-- Composite targets keep every call relationship tenant-bound, even if an
-- opaque UUID from another tenant is guessed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_tasks_tenant_id
    ON client_tasks (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_intake_sessions_tenant_id
    ON contact_intake_sessions (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_handoff_tasks_tenant_id
    ON intake_handoff_tasks (tenant_id, id);

DROP TRIGGER IF EXISTS trg_telephony_routes_updated ON telephony_routes;
CREATE TRIGGER trg_telephony_routes_updated
    BEFORE UPDATE ON telephony_routes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS inbound_voice_calls (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    route_id                   uuid NOT NULL,
    contact_id                 uuid,
    client_id                  uuid,
    provider_call_sid          char(34) NOT NULL,
    provider_status            text NOT NULL DEFAULT 'ringing',
    direction                  text NOT NULL DEFAULT 'inbound',
    intake_mode                text NOT NULL,
    caller_phone_lookup_hash   char(64) NOT NULL,
    caller_phone_ciphertext    bytea NOT NULL,
    transcript_ciphertext      bytea,
    summary_ciphertext         bytea,
    intake_answers_ciphertext  bytea,
    contact_intake_session_id  uuid,
    intake_handoff_task_id     uuid,
    transcript_status          text NOT NULL DEFAULT 'pending',
    handoff_status             text NOT NULL DEFAULT 'unqualified',
    callback_task_id           uuid,
    opt_out_requested          boolean NOT NULL DEFAULT false,
    disclosure_version         text NOT NULL,
    disclosed_at               timestamptz NOT NULL,
    started_at                 timestamptz,
    ended_at                   timestamptz,
    retention_expires_at       timestamptz NOT NULL DEFAULT (now() + interval '365 days'),
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT inbound_voice_calls_tenant_route_fk
        FOREIGN KEY (tenant_id, route_id)
        REFERENCES telephony_routes(tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT inbound_voice_calls_tenant_contact_fk
        FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts(tenant_id, id) ON DELETE SET NULL (contact_id),
    CONSTRAINT inbound_voice_calls_tenant_client_fk
        FOREIGN KEY (tenant_id, client_id)
        REFERENCES clients(tenant_id, id) ON DELETE SET NULL (client_id),
    CONSTRAINT inbound_voice_calls_tenant_callback_task_fk
        FOREIGN KEY (tenant_id, callback_task_id)
        REFERENCES client_tasks(tenant_id, id) ON DELETE SET NULL (callback_task_id),
    CONSTRAINT inbound_voice_calls_tenant_intake_session_fk
        FOREIGN KEY (tenant_id, contact_intake_session_id)
        REFERENCES contact_intake_sessions(tenant_id, id)
        ON DELETE SET NULL (contact_intake_session_id),
    CONSTRAINT inbound_voice_calls_tenant_intake_task_fk
        FOREIGN KEY (tenant_id, intake_handoff_task_id)
        REFERENCES intake_handoff_tasks(tenant_id, id)
        ON DELETE SET NULL (intake_handoff_task_id),
    CONSTRAINT inbound_voice_calls_provider_key UNIQUE (provider_call_sid),
    CONSTRAINT inbound_voice_calls_sid_chk CHECK (provider_call_sid ~ '^CA[0-9A-Fa-f]{32}$'),
    CONSTRAINT inbound_voice_calls_direction_chk CHECK (direction = 'inbound'),
    CONSTRAINT inbound_voice_calls_mode_chk CHECK (intake_mode IN ('buyer','seller','auto')),
    CONSTRAINT inbound_voice_calls_status_chk CHECK (
        provider_status IN (
            'ringing','in-progress','completed','busy','failed',
            'no-answer','canceled','declined'
        )
    ),
    CONSTRAINT inbound_voice_calls_transcript_chk CHECK (
        transcript_status IN ('pending','active','complete','failed','deleted')
    ),
    CONSTRAINT inbound_voice_calls_handoff_chk CHECK (
        handoff_status IN ('matched','unqualified','callback_ready','do_not_contact','closed')
    ),
    CONSTRAINT inbound_voice_calls_retention_chk CHECK (retention_expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_inbound_voice_calls_tenant_created
    ON inbound_voice_calls (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_voice_calls_contact
    ON inbound_voice_calls (tenant_id, contact_id, created_at DESC)
    WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbound_voice_calls_open
    ON inbound_voice_calls (tenant_id, provider_status, created_at)
    WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_inbound_voice_calls_retention
    ON inbound_voice_calls (retention_expires_at)
    WHERE transcript_status <> 'deleted';
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_voice_contact_intake
    ON inbound_voice_calls (contact_intake_session_id)
    WHERE contact_intake_session_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_inbound_voice_calls_updated ON inbound_voice_calls;
CREATE TRIGGER trg_inbound_voice_calls_updated
    BEFORE UPDATE ON inbound_voice_calls
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['telephony_routes','inbound_voice_calls']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', table_name || '_tenant_isolation', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I '
            'USING (app_is_platform_admin() OR tenant_id = app_current_tenant()) '
            'WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant())',
            table_name || '_tenant_isolation', table_name
        );
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON telephony_routes, inbound_voice_calls TO oracle_app;
REVOKE DELETE, TRUNCATE ON telephony_routes, inbound_voice_calls FROM oracle_app;

COMMIT;
