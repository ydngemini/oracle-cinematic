-- Persist the account-security agreement acknowledgement server-side so it is
-- auditable and cannot be satisfied by editing browser storage.

BEGIN;

CREATE TABLE IF NOT EXISTS account_security_acceptances (
    tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id_normalized  text NOT NULL CHECK (
        char_length(agent_id_normalized) BETWEEN 1 AND 128
        AND agent_id_normalized = lower(btrim(agent_id_normalized))
    ),
    agreement_version    text NOT NULL CHECK (char_length(agreement_version) BETWEEN 1 AND 96),
    accepted_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent_id_normalized, agreement_version)
);

CREATE INDEX IF NOT EXISTS idx_account_security_acceptances_recent
    ON account_security_acceptances (tenant_id, accepted_at DESC);

ALTER TABLE account_security_acceptances ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_security_acceptances FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_security_acceptances_tenant_isolation
    ON account_security_acceptances;
CREATE POLICY account_security_acceptances_tenant_isolation
    ON account_security_acceptances
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE ON account_security_acceptances TO oracle_app;
REVOKE DELETE, TRUNCATE ON account_security_acceptances FROM oracle_app;

COMMIT;
