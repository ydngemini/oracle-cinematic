-- 0031_tenant_contract_template_registry.sql
--
-- Every tenant receives the same version-controlled contract-source registry.
-- A source being approved by the platform registry is deliberately distinct
-- from attorney approval of a tenant's executable legal form: registrations
-- are safe to provision automatically, while contract_templates remain drafts
-- until the existing attorney-review workflow approves their exact checksum.

CREATE TABLE IF NOT EXISTS contract_template_sources (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    template_key        text NOT NULL,
    version             text NOT NULL,
    document_type       text NOT NULL CHECK (document_type IN
                            ('assignment','seller_purchase','buyer_purchase','joint_venture','redline')),
    jurisdiction        text NOT NULL,
    source_sha256       char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_status       text NOT NULL DEFAULT 'approved'
                            CHECK (source_status IN ('approved','retired')),
    source_ref          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (template_key, version)
);

CREATE TABLE IF NOT EXISTS tenant_contract_template_registrations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id           uuid NOT NULL REFERENCES contract_template_sources(id) ON DELETE RESTRICT,
    status              text NOT NULL DEFAULT 'registered'
                            CHECK (status IN ('registered','retired')),
    registered_by       text NOT NULL DEFAULT 'system:contract-template-registry',
    registered_at       timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_contract_template_registry_tenant
    ON tenant_contract_template_registrations(tenant_id, status);

-- Source checksums are calculated from the canonical strings in
-- backend/ml_forge/synthetic_lawyer.py. A body change must ship as a new
-- source version, never as a silent replacement of a tenant legal form.
INSERT INTO contract_template_sources (
    template_key, version, document_type, jurisdiction, source_sha256, source_status, source_ref
) VALUES
    ('assignment-standard', '1.0.0', 'assignment', 'US-GENERIC', '8f99726faf31ebba04aa13ee48fae7858b0823e280063a5c4ddcfffc993287fb', 'approved', 'backend/ml_forge/synthetic_lawyer.py:BUILTIN_CONTRACT_TEMPLATES'),
    ('seller-purchase-standard', '1.0.0', 'seller_purchase', 'US-GENERIC', '720b67f916d5c1410de8d56ec363a7692d55b0dce89f4f7a0ddfc7f5327253d1', 'approved', 'backend/ml_forge/synthetic_lawyer.py:BUILTIN_CONTRACT_TEMPLATES'),
    ('buyer-purchase-standard', '1.0.0', 'buyer_purchase', 'US-GENERIC', 'a85496efd60d1f5e0227fd727e0c654a286277dece34a62e18a69ab8fdad1461', 'approved', 'backend/ml_forge/synthetic_lawyer.py:BUILTIN_CONTRACT_TEMPLATES'),
    ('joint-venture-standard', '1.0.0', 'joint_venture', 'US-GENERIC', '5134573965ccc2f211d3e69d723b3837c45366317d8eae771104a59e5948a3d9', 'approved', 'backend/ml_forge/synthetic_lawyer.py:BUILTIN_CONTRACT_TEMPLATES'),
    ('defensive-redline-standard', '1.0.0', 'redline', 'US-GENERIC', '5d21ececbad45dc86e5f2f853ca7fc5ff4a6c59b51fe572494ed2fe6d0acbe1c', 'approved', 'backend/ml_forge/synthetic_lawyer.py:BUILTIN_CONTRACT_TEMPLATES')
ON CONFLICT (template_key, version) DO UPDATE
SET document_type = EXCLUDED.document_type,
    jurisdiction = EXCLUDED.jurisdiction,
    source_sha256 = EXCLUDED.source_sha256,
    source_status = EXCLUDED.source_status,
    source_ref = EXCLUDED.source_ref,
    updated_at = now();

-- Backfill the registry for every current tenant before future rows are
-- handled by the tenant trigger below.
INSERT INTO tenant_contract_template_registrations (tenant_id, source_id)
SELECT tenant.id, source.id
FROM tenants AS tenant
CROSS JOIN contract_template_sources AS source
WHERE source.source_status = 'approved'
ON CONFLICT (tenant_id, source_id) DO NOTHING;

CREATE OR REPLACE FUNCTION register_tenant_contract_template_sources()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    INSERT INTO tenant_contract_template_registrations (tenant_id, source_id)
    SELECT NEW.id, source.id
    FROM contract_template_sources AS source
    WHERE source.source_status = 'approved'
    ON CONFLICT (tenant_id, source_id) DO NOTHING;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION register_tenant_contract_template_sources() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_tenants_register_contract_template_sources ON tenants;
CREATE TRIGGER trg_tenants_register_contract_template_sources
AFTER INSERT ON tenants
FOR EACH ROW EXECUTE FUNCTION register_tenant_contract_template_sources();

ALTER TABLE contract_template_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE contract_template_sources FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS contract_template_sources_authenticated_read ON contract_template_sources;
CREATE POLICY contract_template_sources_authenticated_read ON contract_template_sources
    FOR SELECT USING (app_current_tenant() IS NOT NULL OR app_is_platform_admin());

ALTER TABLE tenant_contract_template_registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_contract_template_registrations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_contract_template_registrations_tenant_isolation ON tenant_contract_template_registrations;
CREATE POLICY tenant_contract_template_registrations_tenant_isolation ON tenant_contract_template_registrations
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT ON contract_template_sources, tenant_contract_template_registrations TO oracle_app;
