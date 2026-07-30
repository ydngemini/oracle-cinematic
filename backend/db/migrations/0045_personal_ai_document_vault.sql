-- Personal AI document synthesis lifecycle. Generated PDFs remain private,
-- tenant-scoped, checksum-pinned, and encrypted by the S3 vault.

CREATE TABLE IF NOT EXISTS contract_synthesis_artifacts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id           uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    doc_id              text NOT NULL,
    state_code          char(2) NOT NULL,
    template_id         uuid NOT NULL REFERENCES contract_templates(id) ON DELETE RESTRICT,
    template_sha256     char(64) NOT NULL CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
    pdf_sha256          char(64) CHECK (pdf_sha256 IS NULL OR pdf_sha256 ~ '^[0-9a-f]{64}$'),
    s3_key              text,
    encryption          text CHECK (encryption IS NULL OR encryption = 'AES256'),
    status              text NOT NULL CHECK (status IN (
                            'generating','encrypted_in_vault','local_preview_only','failed'
                        )),
    failure_code        text,
    expires_at          timestamptz,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_contract_synthesis_client
    ON contract_synthesis_artifacts (tenant_id, client_id, created_at DESC);

ALTER TABLE contract_synthesis_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE contract_synthesis_artifacts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS contract_synthesis_tenant_isolation
    ON contract_synthesis_artifacts;
CREATE POLICY contract_synthesis_tenant_isolation
    ON contract_synthesis_artifacts
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE ON contract_synthesis_artifacts TO oracle_app;

-- The platform operator supplied this public agent contact number for draft
-- signatures. It is not a provider caller ID and does not configure Twilio.
UPDATE user_profiles
   SET phone = COALESCE(NULLIF(phone, ''), '+13024078981')
 WHERE user_id = 'ydnop@ydnhft.com'
   AND tenant_id = '00000000-0000-0000-0000-000000000000';
