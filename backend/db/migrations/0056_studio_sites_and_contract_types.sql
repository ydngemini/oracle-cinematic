-- Expand the contract registry without inventing legal text, and add the
-- tenant-scoped foundation for Neoh Studio hyperlocal sites.

BEGIN;

-- These values describe a workflow category only. A category does not make a
-- form executable: contract_templates still requires an exact checksum plus
-- attorney approval before contracts_api can render a binding document.
ALTER TABLE contract_template_sources
    DROP CONSTRAINT IF EXISTS contract_template_sources_document_type_check;
ALTER TABLE contract_template_sources
    ADD CONSTRAINT contract_template_sources_document_type_check CHECK (document_type IN (
        'assignment','seller_purchase','buyer_purchase','joint_venture','redline',
        'account_security_esa','buyer_representation','buyer_offer',
        'inspection_repair_request','financing_contingency_addendum',
        'listing_agreement','seller_disclosure','counteroffer_addendum',
        'termination_release'
    ));

ALTER TABLE contract_templates
    DROP CONSTRAINT IF EXISTS contract_templates_document_type_check;
ALTER TABLE contract_templates
    ADD CONSTRAINT contract_templates_document_type_check CHECK (document_type IN (
        'assignment','seller_purchase','buyer_purchase','joint_venture','redline',
        'account_security_esa','buyer_representation','buyer_offer',
        'inspection_repair_request','financing_contingency_addendum',
        'listing_agreement','seller_disclosure','counteroffer_addendum',
        'termination_release'
    ));

ALTER TABLE contract_documents
    DROP CONSTRAINT IF EXISTS contract_documents_document_type_check;
ALTER TABLE contract_documents
    ADD CONSTRAINT contract_documents_document_type_check CHECK (document_type IN (
        'assignment','seller_purchase','buyer_purchase','joint_venture','redline',
        'account_security_esa','buyer_representation','buyer_offer',
        'inspection_repair_request','financing_contingency_addendum',
        'listing_agreement','seller_disclosure','counteroffer_addendum',
        'termination_release'
    ));

ALTER TABLE contract_draft_workspaces
    DROP CONSTRAINT IF EXISTS contract_draft_workspaces_document_type_check;
ALTER TABLE contract_draft_workspaces
    ADD CONSTRAINT contract_draft_workspaces_document_type_check CHECK (document_type IN (
        'assignment','seller_purchase','buyer_purchase','joint_venture','redline',
        'account_security_esa','buyer_representation','buyer_offer',
        'inspection_repair_request','financing_contingency_addendum',
        'listing_agreement','seller_disclosure','counteroffer_addendum',
        'termination_release'
    ));

CREATE TABLE IF NOT EXISTS hyperlocal_sites (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_agent_id        text NOT NULL,
    name                  text NOT NULL CHECK (char_length(name) BETWEEN 2 AND 120),
    slug                  text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'),
    template_key          text NOT NULL CHECK (template_key IN
                              ('editorial','neighborhood','listing_focus')),
    status                text NOT NULL DEFAULT 'draft' CHECK (status IN
                              ('draft','preview','published','archived')),
    preview_revision_id   uuid,
    published_revision_id uuid,
    primary_domain        text,
    published_at          timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS hyperlocal_site_revisions (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id                uuid NOT NULL,
    revision               integer NOT NULL CHECK (revision > 0),
    brand_theme            jsonb NOT NULL,
    content                jsonb NOT NULL,
    authorized_idx_sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_manifest        jsonb NOT NULL DEFAULT '[]'::jsonb,
    content_sha256         char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_by             text NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hyperlocal_site_revisions_site_fk
        FOREIGN KEY (tenant_id, site_id)
        REFERENCES hyperlocal_sites(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT hyperlocal_site_revisions_json_chk CHECK (
        jsonb_typeof(brand_theme) = 'object'
        AND jsonb_typeof(content) = 'object'
        AND jsonb_typeof(authorized_idx_sources) = 'array'
        AND jsonb_typeof(source_manifest) = 'array'
    ),
    UNIQUE (tenant_id, site_id, id),
    UNIQUE (tenant_id, site_id, revision)
);

ALTER TABLE hyperlocal_sites
    DROP CONSTRAINT IF EXISTS hyperlocal_sites_preview_revision_fk;
ALTER TABLE hyperlocal_sites
    ADD CONSTRAINT hyperlocal_sites_preview_revision_fk
    FOREIGN KEY (tenant_id, id, preview_revision_id)
    REFERENCES hyperlocal_site_revisions(tenant_id, site_id, id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE hyperlocal_sites
    DROP CONSTRAINT IF EXISTS hyperlocal_sites_published_revision_fk;
ALTER TABLE hyperlocal_sites
    ADD CONSTRAINT hyperlocal_sites_published_revision_fk
    FOREIGN KEY (tenant_id, id, published_revision_id)
    REFERENCES hyperlocal_site_revisions(tenant_id, site_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS hyperlocal_site_domains (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id         uuid NOT NULL,
    hostname        text NOT NULL CHECK (hostname = lower(hostname)),
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending','verified','active','failed','retired')),
    verification_hash char(64) CHECK (
                        verification_hash IS NULL OR verification_hash ~ '^[0-9a-f]{64}$'),
    verified_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hyperlocal_site_domains_site_fk
        FOREIGN KEY (tenant_id, site_id)
        REFERENCES hyperlocal_sites(tenant_id, id) ON DELETE CASCADE,
    UNIQUE (hostname),
    UNIQUE (tenant_id, site_id, hostname)
);

CREATE TABLE IF NOT EXISTS hyperlocal_site_attribution_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id         uuid NOT NULL,
    event_type      text NOT NULL CHECK (event_type IN
                        ('visit','lead_capture','intake_complete','appointment',
                         'contract','closing')),
    subject_kind    text NOT NULL DEFAULT 'session' CHECK (subject_kind IN
                        ('session','contact','client')),
    subject_id      text,
    session_hash    char(64) CHECK (session_hash IS NULL OR session_hash ~ '^[0-9a-f]{64}$'),
    source          text,
    medium          text,
    campaign        text,
    content         text,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hyperlocal_site_attribution_site_fk
        FOREIGN KEY (tenant_id, site_id)
        REFERENCES hyperlocal_sites(tenant_id, id) ON DELETE CASCADE
);

-- Campaign approvals are tenant anchors, not merely globally unique IDs.  The
-- composite key prevents an application bug from linking a campaign to an
-- approval that belongs to another tenant even if that UUID becomes known.
CREATE UNIQUE INDEX IF NOT EXISTS uq_action_approvals_tenant_id
    ON action_approvals (tenant_id, id);

CREATE TABLE IF NOT EXISTS studio_campaigns (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id              uuid NOT NULL,
    provider             text NOT NULL,
    customer_account_ref text NOT NULL,
    name                 text NOT NULL CHECK (char_length(name) BETWEEN 2 AND 160),
    hard_budget          numeric(14,2) NOT NULL CHECK (hard_budget > 0),
    spent_amount         numeric(14,2) NOT NULL DEFAULT 0 CHECK (
                           spent_amount >= 0 AND spent_amount <= hard_budget),
    status               text NOT NULL DEFAULT 'draft' CHECK (status IN
                           ('draft','awaiting_approval','approved','active','paused','completed')),
    approval_id          uuid,
    source_manifest      jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                           jsonb_typeof(source_manifest) = 'array'),
    created_by           text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT studio_campaigns_site_fk
        FOREIGN KEY (tenant_id, site_id)
        REFERENCES hyperlocal_sites(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT studio_campaigns_approval_fk
        FOREIGN KEY (tenant_id, approval_id)
        REFERENCES action_approvals(tenant_id, id)
        ON DELETE SET NULL (approval_id)
);

-- Rebuild the approval relationship for databases that previously applied this
-- migration with the scalar action_approvals(id) reference.
ALTER TABLE studio_campaigns
    DROP CONSTRAINT IF EXISTS studio_campaigns_approval_id_fkey;
ALTER TABLE studio_campaigns
    DROP CONSTRAINT IF EXISTS studio_campaigns_approval_fk;
ALTER TABLE studio_campaigns
    ADD CONSTRAINT studio_campaigns_approval_fk
    FOREIGN KEY (tenant_id, approval_id)
    REFERENCES action_approvals(tenant_id, id)
    ON DELETE SET NULL (approval_id);

CREATE INDEX IF NOT EXISTS idx_hyperlocal_sites_tenant_status
    ON hyperlocal_sites (tenant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_hyperlocal_site_revisions_latest
    ON hyperlocal_site_revisions (tenant_id, site_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_hyperlocal_site_attribution_funnel
    ON hyperlocal_site_attribution_events (tenant_id, site_id, event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_studio_campaigns_tenant_status
    ON studio_campaigns (tenant_id, status, updated_at DESC);

DROP TRIGGER IF EXISTS trg_hyperlocal_sites_updated ON hyperlocal_sites;
CREATE TRIGGER trg_hyperlocal_sites_updated
    BEFORE UPDATE ON hyperlocal_sites
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_hyperlocal_site_domains_updated ON hyperlocal_site_domains;
CREATE TRIGGER trg_hyperlocal_site_domains_updated
    BEFORE UPDATE ON hyperlocal_site_domains
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_studio_campaigns_updated ON studio_campaigns;
CREATE TRIGGER trg_studio_campaigns_updated
    BEFORE UPDATE ON studio_campaigns
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE hyperlocal_sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_sites FORCE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_domains FORCE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_attribution_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_attribution_events FORCE ROW LEVEL SECURITY;
ALTER TABLE studio_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE studio_campaigns FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS hyperlocal_sites_tenant_isolation ON hyperlocal_sites;
CREATE POLICY hyperlocal_sites_tenant_isolation ON hyperlocal_sites
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
DROP POLICY IF EXISTS hyperlocal_site_revisions_tenant_isolation ON hyperlocal_site_revisions;
CREATE POLICY hyperlocal_site_revisions_tenant_isolation ON hyperlocal_site_revisions
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
DROP POLICY IF EXISTS hyperlocal_site_domains_tenant_isolation ON hyperlocal_site_domains;
CREATE POLICY hyperlocal_site_domains_tenant_isolation ON hyperlocal_site_domains
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
DROP POLICY IF EXISTS hyperlocal_site_attribution_tenant_isolation ON hyperlocal_site_attribution_events;
CREATE POLICY hyperlocal_site_attribution_tenant_isolation ON hyperlocal_site_attribution_events
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
DROP POLICY IF EXISTS studio_campaigns_tenant_isolation ON studio_campaigns;
CREATE POLICY studio_campaigns_tenant_isolation ON studio_campaigns
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE ON
    hyperlocal_sites, hyperlocal_site_revisions, hyperlocal_site_domains, studio_campaigns
    TO oracle_app;
GRANT SELECT, INSERT ON hyperlocal_site_attribution_events TO oracle_app;
REVOKE DELETE, TRUNCATE ON
    hyperlocal_sites, hyperlocal_site_revisions, hyperlocal_site_domains,
    hyperlocal_site_attribution_events, studio_campaigns
    FROM oracle_app;

COMMIT;
