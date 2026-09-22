-- 0108 — Telnyx as the messaging (SMS/MMS) rail, separate from Plivo voice.
--
-- Neoh keeps two independent carrier rails per the migration brief:
--   VoiceProvider     -> Plivo   (telephony_routes, unchanged by this file)
--   MessagingProvider -> Telnyx  (new tables below)
--
-- The public business number is NOT duplicated here. messaging_routes has no
-- number column of its own — it joins to telephony_routes on
-- (tenant_id, agent_id) and reads voice_caller_id_e164 as the one public
-- identity shared by both rails. Losing that join loses messaging routing
-- entirely, by design: messaging can only ever be set up for a number that
-- already went through voice's server-verified connect flow.

BEGIN;

-- ── messaging_routes: one row per agent, messaging-specific state only ─────
CREATE TABLE IF NOT EXISTS messaging_routes (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id                    text NOT NULL,
    provider                    text NOT NULL DEFAULT 'telnyx',
    provider_account_id         text,
    messaging_profile_id        text,

    -- Hosted-number eligibility/order lifecycle.
    eligibility_status          text NOT NULL DEFAULT 'unknown',
    eligibility_checked_at      timestamptz,
    eligibility_detail          text,
    hosted_order_id             text,
    hosted_order_status         text NOT NULL DEFAULT 'not_started',
    hosted_order_failure_reason text,
    verification_method         text,

    -- LOA/invoice document state — see messaging_hosted_documents for the
    -- actual encrypted file references. These two columns are a fast summary
    -- so the UI does not need a join to know whether it should prompt for
    -- documents.
    loa_document_state          text NOT NULL DEFAULT 'not_required',
    invoice_document_state      text NOT NULL DEFAULT 'not_required',

    -- 10DLC — brand is tenant-wide (see tenant_messaging_brands) but the
    -- campaign a given number is assigned to is recorded here since a
    -- brokerage could in principle run more than one campaign.
    campaign_id                 uuid,

    active                      boolean NOT NULL DEFAULT true,
    disconnected_at             timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT messaging_routes_tenant_agent_key UNIQUE (tenant_id, agent_id),
    CONSTRAINT messaging_routes_tenant_id_key UNIQUE (tenant_id, id),
    CONSTRAINT messaging_routes_provider_chk CHECK (provider IN ('telnyx','twilio')),
    CONSTRAINT messaging_routes_eligibility_chk CHECK (eligibility_status IN (
        'unknown','eligible','ineligible_wireless','ineligible_google_voice',
        'ineligible_provider','ineligible_number_type','ineligible_region',
        'needs_review'
    )),
    CONSTRAINT messaging_routes_hosted_status_chk CHECK (hosted_order_status IN (
        'not_started','pending_verification','pending_documents',
        'manual_action_required','processing','active','failed','disconnected'
    )),
    CONSTRAINT messaging_routes_loa_chk CHECK (loa_document_state IN (
        'not_required','required','uploaded','accepted','rejected'
    )),
    CONSTRAINT messaging_routes_invoice_chk CHECK (invoice_document_state IN (
        'not_required','required','uploaded','accepted','rejected'
    ))
);

CREATE INDEX IF NOT EXISTS idx_messaging_routes_tenant
    ON messaging_routes (tenant_id, active);

DROP TRIGGER IF EXISTS trg_messaging_routes_updated ON messaging_routes;
CREATE TRIGGER trg_messaging_routes_updated
    BEFORE UPDATE ON messaging_routes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE messaging_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE messaging_routes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS messaging_routes_tenant_isolation ON messaging_routes;
CREATE POLICY messaging_routes_tenant_isolation ON messaging_routes
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON messaging_routes TO oracle_app;

-- ── tenant_messaging_brands: 10DLC Brand, one per tenant/brokerage ─────────
CREATE TABLE IF NOT EXISTS tenant_messaging_brands (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    provider            text NOT NULL DEFAULT 'telnyx',
    provider_brand_id   text,
    status              text NOT NULL DEFAULT 'not_started',
    -- Business info Telnyx's Brand API accepts (confirmed field set against
    -- the installed SDK — see messaging_provider.py). EIN is the one field
    -- worth masking on read; it is not encrypted at rest here because the
    -- pgcrypto tenant-key path is reserved for PII with its own retention
    -- policy (contact phones/transcripts) — this is business registration
    -- data the tenant explicitly submits for a 10DLC filing, comparable to
    -- what already sits in tenant billing profiles.
    display_name        text,
    company_name        text,
    ein                 text,
    entity_type         text,
    vertical            text,
    email               text,
    phone               text,
    street              text,
    city                text,
    state               text,
    postal_code         text,
    country             text,
    website             text,
    failure_reason      text,
    submitted_at        timestamptz,
    approved_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_messaging_brands_status_chk CHECK (status IN (
        'not_started','pending','unverified','verified','failed'
    ))
);

DROP TRIGGER IF EXISTS trg_tenant_messaging_brands_updated ON tenant_messaging_brands;
CREATE TRIGGER trg_tenant_messaging_brands_updated
    BEFORE UPDATE ON tenant_messaging_brands
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE tenant_messaging_brands ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_messaging_brands FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_messaging_brands_tenant_isolation ON tenant_messaging_brands;
CREATE POLICY tenant_messaging_brands_tenant_isolation ON tenant_messaging_brands
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON tenant_messaging_brands TO oracle_app;

-- ── tenant_messaging_campaigns: 10DLC Campaign, tenant-wide ────────────────
CREATE TABLE IF NOT EXISTS tenant_messaging_campaigns (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    brand_id             uuid NOT NULL REFERENCES tenant_messaging_brands(id) ON DELETE RESTRICT,
    provider             text NOT NULL DEFAULT 'telnyx',
    provider_campaign_id text,
    status               text NOT NULL DEFAULT 'not_started',
    usecase              text,
    description          text,
    failure_reason       text,
    submitted_at         timestamptz,
    approved_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_messaging_campaigns_status_chk CHECK (status IN (
        'not_started','pending','approved','rejected','failed'
    )),
    CONSTRAINT tenant_messaging_campaigns_tenant_id_key UNIQUE (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_messaging_campaigns_tenant
    ON tenant_messaging_campaigns (tenant_id, status);

DROP TRIGGER IF EXISTS trg_tenant_messaging_campaigns_updated ON tenant_messaging_campaigns;
CREATE TRIGGER trg_tenant_messaging_campaigns_updated
    BEFORE UPDATE ON tenant_messaging_campaigns
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE tenant_messaging_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_messaging_campaigns FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_messaging_campaigns_tenant_isolation ON tenant_messaging_campaigns;
CREATE POLICY tenant_messaging_campaigns_tenant_isolation ON tenant_messaging_campaigns
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON tenant_messaging_campaigns TO oracle_app;

ALTER TABLE messaging_routes
    ADD CONSTRAINT messaging_routes_tenant_campaign_fk
        FOREIGN KEY (tenant_id, campaign_id)
        REFERENCES tenant_messaging_campaigns (tenant_id, id)
        ON DELETE SET NULL (campaign_id);

-- ── messaging_hosted_documents: LOA/invoice files, encrypted-at-rest keys ──
CREATE TABLE IF NOT EXISTS messaging_hosted_documents (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    messaging_route_id  uuid NOT NULL,
    doc_type            text NOT NULL,
    storage_key         text NOT NULL,
    sha256              char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type        text NOT NULL,
    status              text NOT NULL DEFAULT 'uploaded',
    rejection_reason    text,
    uploaded_by         text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messaging_hosted_documents_tenant_route_fk
        FOREIGN KEY (tenant_id, messaging_route_id)
        REFERENCES messaging_routes (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT messaging_hosted_documents_type_chk CHECK (doc_type IN ('loa','invoice')),
    CONSTRAINT messaging_hosted_documents_status_chk CHECK (status IN (
        'uploaded','accepted','rejected'
    ))
);

CREATE INDEX IF NOT EXISTS idx_messaging_hosted_documents_route
    ON messaging_hosted_documents (tenant_id, messaging_route_id, doc_type, created_at DESC);

ALTER TABLE messaging_hosted_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE messaging_hosted_documents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS messaging_hosted_documents_tenant_isolation ON messaging_hosted_documents;
CREATE POLICY messaging_hosted_documents_tenant_isolation ON messaging_hosted_documents
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON messaging_hosted_documents TO oracle_app;

-- ── sms_messages: generic message log — the CRM timeline event source ─────
CREATE TABLE IF NOT EXISTS sms_messages (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id              text NOT NULL,
    contact_id            uuid,
    client_id             uuid,
    direction             text NOT NULL,
    provider              text NOT NULL,
    provider_message_id   text NOT NULL,
    provider_status       text,
    status                text NOT NULL DEFAULT 'queued',
    from_e164             text NOT NULL,
    to_e164               text NOT NULL,
    body                  text,
    media                 jsonb NOT NULL DEFAULT '[]'::jsonb,
    error_reason          text,
    opt_out_event         boolean NOT NULL DEFAULT false,
    activity_id           uuid,
    created_at            timestamptz NOT NULL DEFAULT now(),
    delivered_at          timestamptz,
    failed_at             timestamptz,
    CONSTRAINT sms_messages_tenant_contact_fk
        FOREIGN KEY (tenant_id, contact_id)
        REFERENCES agent_contacts(tenant_id, id) ON DELETE SET NULL (contact_id),
    CONSTRAINT sms_messages_tenant_client_fk
        FOREIGN KEY (tenant_id, client_id)
        REFERENCES clients(tenant_id, id) ON DELETE SET NULL (client_id),
    CONSTRAINT sms_messages_direction_chk CHECK (direction IN ('inbound','outbound')),
    CONSTRAINT sms_messages_provider_chk CHECK (provider IN ('telnyx','twilio')),
    CONSTRAINT sms_messages_status_chk CHECK (status IN (
        'queued','sent','delivered','failed','undelivered'
    )),
    -- The durable idempotency boundary a retried/duplicated webhook must
    -- collapse onto (brief section 27).
    CONSTRAINT sms_messages_provider_message_key UNIQUE (provider, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_sms_messages_tenant_created
    ON sms_messages (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_messages_contact
    ON sms_messages (tenant_id, contact_id, created_at DESC) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sms_messages_client
    ON sms_messages (tenant_id, client_id, created_at DESC) WHERE client_id IS NOT NULL;

ALTER TABLE sms_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE sms_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sms_messages_tenant_isolation ON sms_messages;
CREATE POLICY sms_messages_tenant_isolation ON sms_messages
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON sms_messages TO oracle_app;

COMMIT;
