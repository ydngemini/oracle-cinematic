-- 0032_contract_draft_workspaces.sql
--
-- AI-assisted contract workspaces are deliberately separate from the contract
-- lifecycle.  A workspace can be previewed, saved, resumed, and downloaded
-- before it has a lead/transaction anchor or enters the approval/signature
-- path.  The encrypted payload contains the draft text and supplied inputs;
-- metadata stores only hashes, field names, and workflow state.

CREATE TABLE IF NOT EXISTS contract_draft_workspaces (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_type         text NOT NULL CHECK (document_type IN
                              ('assignment','seller_purchase','buyer_purchase','joint_venture','redline')),
    template_key          text NOT NULL,
    template_version      text NOT NULL,
    template_sha256       char(64) NOT NULL CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
    input_hash            char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    payload_ciphertext    bytea NOT NULL,
    status                text NOT NULL DEFAULT 'draft'
                              CHECK (status IN ('draft','ready')),
    metadata              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    completed_at          timestamptz
);

CREATE INDEX IF NOT EXISTS idx_contract_draft_workspaces_tenant_updated
    ON contract_draft_workspaces (tenant_id, updated_at DESC);

DROP TRIGGER IF EXISTS trg_contract_draft_workspaces_updated ON contract_draft_workspaces;
CREATE TRIGGER trg_contract_draft_workspaces_updated
    BEFORE UPDATE ON contract_draft_workspaces
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE contract_draft_workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE contract_draft_workspaces FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS contract_draft_workspaces_tenant_isolation ON contract_draft_workspaces;
CREATE POLICY contract_draft_workspaces_tenant_isolation ON contract_draft_workspaces
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE ON contract_draft_workspaces TO oracle_app;
