-- 0041_password_reset_tokens.sql
--
-- Single-use password-reset capability records. The signed reset JWT carries a
-- high-entropy random jti; only its SHA-256 digest is stored here, so a database
-- read cannot recover a usable reset link. The composite foreign key binds each
-- digest to the exact user/tenant identity that received it.

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_id_tenant_identity
    ON users (id, tenant_id);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    jti_hash    char(64) PRIMARY KEY
                    CHECK (jti_hash ~ '^[0-9a-f]{64}$'),
    user_id     uuid NOT NULL,
    tenant_id   uuid NOT NULL,
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT password_reset_tokens_user_tenant_fkey
        FOREIGN KEY (user_id, tenant_id)
        REFERENCES users (id, tenant_id)
        ON DELETE CASCADE,
    CONSTRAINT password_reset_tokens_expiry_after_creation
        CHECK (expires_at > created_at),
    CONSTRAINT password_reset_tokens_consumed_after_creation
        CHECK (consumed_at IS NULL OR consumed_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_active_user
    ON password_reset_tokens (user_id, expires_at DESC)
    WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_active_expiry
    ON password_reset_tokens (expires_at)
    WHERE consumed_at IS NULL;

ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE password_reset_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS password_reset_tokens_tenant_isolation ON password_reset_tokens;
CREATE POLICY password_reset_tokens_tenant_isolation ON password_reset_tokens
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- 0001 grants broad defaults to new tables. Narrow this security-sensitive
-- table to issuance, lookup, and the single consumed_at state transition.
REVOKE ALL ON password_reset_tokens FROM PUBLIC;
REVOKE ALL ON password_reset_tokens FROM oracle_app;
GRANT SELECT, INSERT ON password_reset_tokens TO oracle_app;
GRANT UPDATE (consumed_at) ON password_reset_tokens TO oracle_app;
