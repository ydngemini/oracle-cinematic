BEGIN;

-- Short-lived, single-use OAuth state is stored as a SHA-256 digest so a
-- database read cannot be turned into an account-linking request.  The PKCE
-- verifier is encrypted with the same tenant-derived pgcrypto key used for
-- provider credentials.
CREATE TABLE IF NOT EXISTS oauth_authorization_states (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider                   text NOT NULL DEFAULT 'google'
                                   CHECK (provider = 'google'),
    state_hash                 char(64) NOT NULL UNIQUE
                                   CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    account_label              text NOT NULL,
    code_verifier_ciphertext   bytea NOT NULL,
    redirect_uri               text NOT NULL,
    return_path                text NOT NULL DEFAULT '/',
    scopes                     text[] NOT NULL DEFAULT '{}',
    expires_at                 timestamptz NOT NULL,
    consumed_at                timestamptz,
    created_by                 text NOT NULL,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_oauth_state_lifetime
        CHECK (expires_at > created_at AND expires_at <= created_at + interval '20 minutes'),
    CONSTRAINT chk_oauth_return_path
        CHECK (return_path LIKE '/%' AND return_path NOT LIKE '//%')
);

CREATE INDEX IF NOT EXISTS idx_oauth_authorization_states_pending
    ON oauth_authorization_states(provider, state_hash, expires_at)
    WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_oauth_authorization_states_tenant
    ON oauth_authorization_states(tenant_id, created_at DESC);

ALTER TABLE oauth_authorization_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_authorization_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS oauth_authorization_states_tenant_isolation
    ON oauth_authorization_states;
CREATE POLICY oauth_authorization_states_tenant_isolation
    ON oauth_authorization_states
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- OAuth state is protocol material, not an operator-editable record.
REVOKE UPDATE, DELETE, TRUNCATE ON oauth_authorization_states FROM oracle_app;
GRANT UPDATE (consumed_at) ON oauth_authorization_states TO oracle_app;

CREATE OR REPLACE FUNCTION purge_expired_oauth_states()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE deleted_count integer := 0;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'platform administrator context required'
            USING ERRCODE = '42501';
    END IF;
    DELETE FROM oauth_authorization_states
     WHERE expires_at < now() - interval '7 days';
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;
REVOKE ALL ON FUNCTION purge_expired_oauth_states() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_expired_oauth_states() TO oracle_app;

COMMIT;
