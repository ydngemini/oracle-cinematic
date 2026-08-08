-- Signed multi-source lead intake and deterministic brokerage routing.

BEGIN;

CREATE TABLE IF NOT EXISTS lead_source_connectors (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    public_id                 uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    source_key                text NOT NULL CHECK (source_key ~ '^[a-z0-9][a-z0-9_-]{1,63}$'),
    name                      text NOT NULL CHECK (char_length(name) BETWEEN 2 AND 120),
    webhook_secret_ciphertext bytea NOT NULL,
    active                    boolean NOT NULL DEFAULT true,
    created_by                text NOT NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id)
);

CREATE TABLE IF NOT EXISTS lead_routing_rules (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            text NOT NULL CHECK (char_length(name) BETWEEN 2 AND 120),
    priority        integer NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 10000),
    enabled         boolean NOT NULL DEFAULT true,
    source_key      text,
    zip_codes       text[] NOT NULL DEFAULT ARRAY[]::text[],
    state_codes     text[] NOT NULL DEFAULT ARRAY[]::text[],
    intent          text NOT NULL DEFAULT 'any' CHECK (intent IN ('any','buyer','seller')),
    assignment_mode text NOT NULL DEFAULT 'round_robin' CHECK (assignment_mode IN ('round_robin','fixed_agent')),
    agent_ids       text[] NOT NULL DEFAULT ARRAY[]::text[],
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id),
    CONSTRAINT lead_routing_rule_fixed_agent_chk CHECK (
        assignment_mode <> 'fixed_agent' OR cardinality(agent_ids) > 0
    )
);

CREATE TABLE IF NOT EXISTS agent_routing_state (
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id         text NOT NULL,
    accepting_leads  boolean NOT NULL DEFAULT true,
    capacity         integer NOT NULL DEFAULT 100 CHECK (capacity BETWEEN 0 AND 100000),
    last_assigned_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,agent_id)
);

CREATE TABLE IF NOT EXISTS lead_intake_events (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    connector_id             uuid NOT NULL,
    external_event_id        text NOT NULL CHECK (char_length(external_event_id) BETWEEN 1 AND 240),
    payload_ciphertext       bytea NOT NULL,
    payload_digest           char(64) NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    source_key               text NOT NULL,
    intent                   text NOT NULL CHECK (intent IN ('buyer','seller')),
    zip_code                 text,
    state_code               char(2),
    status                   text NOT NULL DEFAULT 'received' CHECK (status IN ('received','routed','unassigned','failed')),
    contact_id               uuid,
    assigned_agent_id        text,
    route_reason             text,
    received_at              timestamptz NOT NULL DEFAULT now(),
    routed_at                timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id),
    UNIQUE (tenant_id,connector_id,external_event_id),
    CONSTRAINT lead_intake_connector_fk FOREIGN KEY (tenant_id,connector_id)
        REFERENCES lead_source_connectors(tenant_id,id) ON DELETE RESTRICT,
    CONSTRAINT lead_intake_contact_fk FOREIGN KEY (tenant_id,contact_id)
        REFERENCES agent_contacts(tenant_id,id) ON DELETE RESTRICT,
    CONSTRAINT lead_intake_zip_chk CHECK (zip_code IS NULL OR zip_code ~ '^\d{5}$'),
    CONSTRAINT lead_intake_state_chk CHECK (state_code IS NULL OR state_code ~ '^[A-Z]{2}$')
);

CREATE INDEX IF NOT EXISTS idx_lead_routing_rules_match
    ON lead_routing_rules (tenant_id,enabled,priority);
CREATE INDEX IF NOT EXISTS idx_lead_intake_events_recent
    ON lead_intake_events (tenant_id,received_at DESC);
CREATE INDEX IF NOT EXISTS idx_lead_intake_events_contact
    ON lead_intake_events (tenant_id,contact_id) WHERE contact_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_lead_source_connectors_updated ON lead_source_connectors;
CREATE TRIGGER trg_lead_source_connectors_updated BEFORE UPDATE ON lead_source_connectors
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_lead_routing_rules_updated ON lead_routing_rules;
CREATE TRIGGER trg_lead_routing_rules_updated BEFORE UPDATE ON lead_routing_rules
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_agent_routing_state_updated ON agent_routing_state;
CREATE TRIGGER trg_agent_routing_state_updated BEFORE UPDATE ON agent_routing_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_lead_intake_events_updated ON lead_intake_events;
CREATE TRIGGER trg_lead_intake_events_updated BEFORE UPDATE ON lead_intake_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'lead_source_connectors','lead_routing_rules','agent_routing_state','lead_intake_events'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I',table_name || '_tenant_isolation',table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (app_is_platform_admin() OR tenant_id=app_current_tenant()) WITH CHECK (app_is_platform_admin() OR tenant_id=app_current_tenant())',
            table_name || '_tenant_isolation',table_name
        );
    END LOOP;
END $$;

GRANT SELECT,INSERT,UPDATE ON
    lead_source_connectors,lead_routing_rules,agent_routing_state,lead_intake_events
    TO oracle_app;
REVOKE DELETE,TRUNCATE ON
    lead_source_connectors,lead_routing_rules,agent_routing_state,lead_intake_events
    FROM oracle_app;

COMMIT;
