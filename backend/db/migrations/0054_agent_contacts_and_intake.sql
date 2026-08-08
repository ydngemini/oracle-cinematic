-- Canonical CRM contact identity, exact three-question intake, and safe
-- anniversary nurture scheduling. Additive migration: legacy `clients` remains
-- the opportunity/relationship record while callers move to `agent_contacts`.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_tenant_id ON clients (tenant_id, id);

CREATE TABLE IF NOT EXISTS agent_contacts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    assigned_agent_id   text,
    pii_ciphertext      bytea,
    email_lookup_hash   char(64),
    phone_lookup_hash   char(64),
    birthday_month      smallint,
    birthday_day        smallint,
    timezone            text NOT NULL DEFAULT 'UTC',
    preferred_channel   text NOT NULL DEFAULT 'none',
    consent             jsonb NOT NULL DEFAULT
        '{"email":{"granted":false},"sms":{"granted":false},"voice":{"granted":false}}'::jsonb,
    suppression         jsonb NOT NULL DEFAULT
        '{"global":false,"email":false,"sms":false,"voice":false,"dnc":false}'::jsonb,
    nurture_enabled     boolean NOT NULL DEFAULT true,
    source              text,
    legacy_client_id    uuid,
    data_state          text NOT NULL DEFAULT 'sealed',
    deleted_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_contacts_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT agent_contacts_tenant_legacy_client_fk
        FOREIGN KEY (tenant_id, legacy_client_id)
        REFERENCES clients (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT agent_contacts_preferred_channel_chk CHECK (
        preferred_channel IN ('none','email','sms','voice')
    ),
    CONSTRAINT agent_contacts_data_state_chk CHECK (
        data_state IN ('pending_encryption','sealed')
    ),
    CONSTRAINT agent_contacts_consent_json_chk CHECK (
        jsonb_typeof(consent) = 'object' AND jsonb_typeof(suppression) = 'object'
    ),
    CONSTRAINT agent_contacts_email_hash_chk CHECK (
        email_lookup_hash IS NULL OR email_lookup_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT agent_contacts_phone_hash_chk CHECK (
        phone_lookup_hash IS NULL OR phone_lookup_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT agent_contacts_birthday_chk CHECK (
        (birthday_month IS NULL AND birthday_day IS NULL)
        OR (
            birthday_month BETWEEN 1 AND 12
            AND birthday_day BETWEEN 1 AND CASE birthday_month
                WHEN 2 THEN 29
                WHEN 4 THEN 30
                WHEN 6 THEN 30
                WHEN 9 THEN 30
                WHEN 11 THEN 30
                ELSE 31
            END
        )
    )
);

-- Upgrade the earlier draft's UUID-only legacy link if this migration is
-- re-applied. The composite key blocks blind cross-tenant relationship anchors.
ALTER TABLE agent_contacts
    DROP CONSTRAINT IF EXISTS agent_contacts_legacy_client_id_fkey;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'agent_contacts_tenant_legacy_client_fk'
    ) THEN
        ALTER TABLE agent_contacts
            ADD CONSTRAINT agent_contacts_tenant_legacy_client_fk
            FOREIGN KEY (tenant_id, legacy_client_id)
            REFERENCES clients (tenant_id, id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_contacts_legacy_client
    ON agent_contacts (legacy_client_id)
    WHERE legacy_client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_contacts_email_lookup
    ON agent_contacts (tenant_id, email_lookup_hash)
    WHERE deleted_at IS NULL AND email_lookup_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_contacts_phone_lookup
    ON agent_contacts (tenant_id, phone_lookup_hash)
    WHERE deleted_at IS NULL AND phone_lookup_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_contacts_agent
    ON agent_contacts (tenant_id, assigned_agent_id, updated_at DESC)
    WHERE deleted_at IS NULL;

DROP TRIGGER IF EXISTS trg_agent_contacts_updated ON agent_contacts;
CREATE TRIGGER trg_agent_contacts_updated
    BEFORE UPDATE ON agent_contacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- The composite key makes it impossible to link a client to another tenant's
-- contact even if a UUID is guessed. contact_id remains nullable for the
-- rolling dual-read/dual-write cutover.
ALTER TABLE clients ADD COLUMN IF NOT EXISTS contact_id uuid;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'clients_tenant_contact_fk'
    ) THEN
        ALTER TABLE clients ADD CONSTRAINT clients_tenant_contact_fk
            FOREIGN KEY (tenant_id, contact_id)
            REFERENCES agent_contacts (tenant_id, id)
            ON DELETE RESTRICT;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_clients_contact_id
    ON clients (tenant_id, contact_id)
    WHERE contact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS contact_property_relationships (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contact_id                uuid NOT NULL,
    client_id                 uuid,
    property_ref_kind         text NOT NULL,
    property_ref_id           uuid,
    property_label_ciphertext bytea,
    relationship_type         text NOT NULL,
    purchase_date             date,
    closing_date              date,
    anniversary_enabled       boolean NOT NULL DEFAULT true,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contact_property_relationships_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT contact_property_contact_fk FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT contact_property_client_fk FOREIGN KEY (tenant_id, client_id)
        REFERENCES clients (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT contact_property_ref_kind_chk CHECK (
        property_ref_kind IN ('lead','listing','public_record','manual')
    ),
    CONSTRAINT contact_property_relationship_type_chk CHECK (
        relationship_type IN ('owner','seller','buyer','occupant','other')
    ),
    CONSTRAINT contact_property_ref_chk CHECK (
        (property_ref_kind = 'manual'
            AND property_ref_id IS NULL
            AND property_label_ciphertext IS NOT NULL)
        OR
        (property_ref_kind <> 'manual' AND property_ref_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_property_source_relationship
    ON contact_property_relationships (
        tenant_id, contact_id, property_ref_kind, property_ref_id, relationship_type
    )
    WHERE property_ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_contact_property_anniversary
    ON contact_property_relationships (tenant_id, contact_id, closing_date, purchase_date)
    WHERE anniversary_enabled;
DROP TRIGGER IF EXISTS trg_contact_property_relationships_updated
    ON contact_property_relationships;
CREATE TRIGGER trg_contact_property_relationships_updated
    BEFORE UPDATE ON contact_property_relationships
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS contact_intake_sessions (
    id                           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contact_id                   uuid NOT NULL,
    client_id                    uuid,
    persona                      text NOT NULL,
    question_set_version         text NOT NULL DEFAULT 'neoh-intake-v1',
    question_count               smallint NOT NULL DEFAULT 3,
    raw_answers_ciphertext       bytea NOT NULL,
    normalized_fields_ciphertext bytea NOT NULL,
    transcript_ciphertext        bytea NOT NULL,
    tool_access                  text[] NOT NULL DEFAULT ARRAY[]::text[],
    status                       text NOT NULL DEFAULT 'handoff_pending',
    created_by                   text NOT NULL,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contact_intake_contact_fk FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT contact_intake_client_fk FOREIGN KEY (tenant_id, client_id)
        REFERENCES clients (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT contact_intake_persona_chk CHECK (persona IN ('buyer','seller')),
    CONSTRAINT contact_intake_question_count_chk CHECK (question_count = 3),
    CONSTRAINT contact_intake_tool_access_chk CHECK (cardinality(tool_access) = 0),
    CONSTRAINT contact_intake_status_chk CHECK (
        status IN ('handoff_pending','reviewed','closed')
    )
);
CREATE INDEX IF NOT EXISTS idx_contact_intake_contact
    ON contact_intake_sessions (tenant_id, contact_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_intake_tenant_id
    ON contact_intake_sessions (tenant_id, id);

CREATE TABLE IF NOT EXISTS intake_handoff_tasks (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    intake_session_id uuid NOT NULL,
    contact_id        uuid NOT NULL,
    client_id         uuid,
    title             text NOT NULL,
    status            text NOT NULL DEFAULT 'open',
    assigned_agent_id text,
    due_at             timestamptz,
    completed_at       timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT intake_handoff_tenant_session_key UNIQUE (tenant_id, intake_session_id),
    CONSTRAINT intake_handoff_session_fk FOREIGN KEY (tenant_id, intake_session_id)
        REFERENCES contact_intake_sessions (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT intake_handoff_contact_fk FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT intake_handoff_client_fk FOREIGN KEY (tenant_id, client_id)
        REFERENCES clients (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT intake_handoff_status_chk CHECK (
        status IN ('open','done','cancelled')
    )
);
-- Add the composite tenant guard when this migration is re-applied to a DB
-- that briefly received the earlier single-column session reference.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'intake_handoff_session_fk'
    ) THEN
        ALTER TABLE intake_handoff_tasks ADD CONSTRAINT intake_handoff_session_fk
            FOREIGN KEY (tenant_id, intake_session_id)
            REFERENCES contact_intake_sessions (tenant_id, id)
            ON DELETE CASCADE;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_intake_handoff_queue
    ON intake_handoff_tasks (tenant_id, status, due_at, created_at)
    WHERE status = 'open';
DROP TRIGGER IF EXISTS trg_intake_handoff_tasks_updated ON intake_handoff_tasks;
CREATE TRIGGER trg_intake_handoff_tasks_updated
    BEFORE UPDATE ON intake_handoff_tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- This table is both the exact-once reservation ledger and the durable worker
-- queue. A sender must atomically transition scheduled -> leased -> sending and
-- may only mark sent after the provider acknowledges delivery.
CREATE TABLE IF NOT EXISTS contact_nurture_jobs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contact_id          uuid NOT NULL,
    relationship_id     uuid,
    event_type          text NOT NULL,
    channel             text NOT NULL,
    calendar_year       smallint NOT NULL,
    idempotency_key     text NOT NULL,
    scheduled_for       timestamptz NOT NULL,
    state               text NOT NULL DEFAULT 'scheduled',
    policy_snapshot     jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_message_id text,
    last_error_code     text,
    sent_at             timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contact_nurture_contact_fk FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT contact_nurture_relationship_fk FOREIGN KEY (tenant_id, relationship_id)
        REFERENCES contact_property_relationships (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT contact_nurture_event_type_chk CHECK (
        event_type IN ('birthday','home_anniversary')
    ),
    CONSTRAINT contact_nurture_channel_chk CHECK (channel IN ('email','sms')),
    CONSTRAINT contact_nurture_year_chk CHECK (calendar_year BETWEEN 2000 AND 2200),
    CONSTRAINT contact_nurture_state_chk CHECK (
        state IN ('scheduled','leased','sending','sent','skipped','failed','cancelled')
    ),
    CONSTRAINT contact_nurture_relationship_required_chk CHECK (
        (event_type = 'birthday' AND relationship_id IS NULL)
        OR (event_type = 'home_anniversary' AND relationship_id IS NOT NULL)
    ),
    CONSTRAINT contact_nurture_exact_once_key UNIQUE (
        tenant_id, contact_id, event_type, channel, calendar_year
    ),
    CONSTRAINT contact_nurture_idempotency_key UNIQUE (tenant_id, idempotency_key)
);
-- Recreate this FK so an idempotent re-apply also upgrades the delete action
-- from the earlier draft's CASCADE to audit-preserving RESTRICT.
ALTER TABLE contact_nurture_jobs
    DROP CONSTRAINT IF EXISTS contact_nurture_relationship_fk;
ALTER TABLE contact_nurture_jobs
    ADD CONSTRAINT contact_nurture_relationship_fk
    FOREIGN KEY (tenant_id, relationship_id)
    REFERENCES contact_property_relationships (tenant_id, id)
    ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS idx_contact_nurture_due
    ON contact_nurture_jobs (state, scheduled_for, created_at)
    WHERE state IN ('scheduled','failed');
DROP TRIGGER IF EXISTS trg_contact_nurture_jobs_updated ON contact_nurture_jobs;
CREATE TRIGGER trg_contact_nurture_jobs_updated
    BEFORE UPDATE ON contact_nurture_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'agent_contacts',
        'contact_property_relationships',
        'contact_intake_sessions',
        'intake_handoff_tasks',
        'contact_nurture_jobs'
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

GRANT SELECT, INSERT, UPDATE ON agent_contacts TO oracle_app;
GRANT SELECT, INSERT, UPDATE ON contact_property_relationships TO oracle_app;
GRANT SELECT, INSERT ON contact_intake_sessions TO oracle_app;
GRANT SELECT, INSERT, UPDATE ON intake_handoff_tasks TO oracle_app;
GRANT SELECT, INSERT, UPDATE ON contact_nurture_jobs TO oracle_app;
REVOKE DELETE, TRUNCATE ON agent_contacts FROM oracle_app;
REVOKE DELETE, TRUNCATE ON contact_property_relationships FROM oracle_app;
REVOKE UPDATE, DELETE, TRUNCATE ON contact_intake_sessions FROM oracle_app;
REVOKE DELETE, TRUNCATE ON intake_handoff_tasks FROM oracle_app;
REVOKE DELETE, TRUNCATE ON contact_nurture_jobs FROM oracle_app;

-- FORCE RLS already protects legacy clients; migration-only platform context
-- allows a cross-tenant, metadata-only backfill. No plaintext PII is copied.
SELECT set_config('app.current_role', 'platform_admin', true);

INSERT INTO agent_contacts (
    tenant_id, assigned_agent_id, legacy_client_id, source, data_state
)
SELECT tenant_id, assignee_id, id, COALESCE(source, 'legacy_client'), 'pending_encryption'
  FROM clients
ON CONFLICT (legacy_client_id) WHERE legacy_client_id IS NOT NULL DO NOTHING;

UPDATE clients AS client
   SET contact_id = contact.id
  FROM agent_contacts AS contact
 WHERE contact.legacy_client_id = client.id
   AND contact.tenant_id = client.tenant_id
   AND client.contact_id IS NULL;

COMMENT ON COLUMN agent_contacts.pii_ciphertext IS
    'Tenant-key encrypted JSON containing normalized full_name, email, and phone. '
    'No encryption key is persisted in this table.';
COMMENT ON COLUMN agent_contacts.phone_lookup_hash IS
    'Tenant-keyed HMAC-SHA256 of normalized E.164 phone for exact matching.';
COMMENT ON COLUMN agent_contacts.email_lookup_hash IS
    'Tenant-keyed HMAC-SHA256 of normalized email for exact matching.';
COMMENT ON COLUMN clients.contact_id IS
    'Canonical agent_contacts identity. Nullable during the dual-read/write migration.';
COMMENT ON COLUMN contact_intake_sessions.tool_access IS
    'Must remain empty: the intake persona has no MLS or public-record tools.';

COMMIT;
