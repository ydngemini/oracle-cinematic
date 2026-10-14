-- ---------------------------------------------------------------------------
-- 0150 — two-step sign-in for tenant users (L17-19, ISO A.8.5 gap #2).
--
-- Owner decision 2026-10-14: authenticator-app (TOTP) codes for every tenant
-- user; required for broker owners at their next sign-in; optional for
-- agents; a broker owner can require it for the whole brokerage.
--
-- The operator's factor (0121, totp.py) is one environment secret. A tenant
-- user's factor is per account, so it needs storage:
--
--   user_mfa_factors        one row per user: the TOTP secret, sealed with the
--                           user's brokerage key (pgcrypto under the per-tenant
--                           HKDF key, crypto.py — never plaintext); when it was
--                           confirmed (NULL = setup started, not finished); the
--                           last accepted time step (a code is accepted once,
--                           the 0121 rule, here as a monotonic step); and the
--                           wrong-code counter and lockout.
--   user_mfa_recovery_codes one-time recovery codes, SHA-256 of an 80-bit
--                           random code; used_at marks a code spent.
--   tenants.require_mfa     the brokerage-wide switch a broker owner sets.
--
-- Access: only platform contexts (the auth module's pre-auth context,
-- erasure) may touch the two tables. No request made in a brokerage's own
-- context, owner or agent, can read another person's sealed secret, or its
-- own: every read and write goes through user_mfa.py on the platform login.
-- The policy is app_is_platform_admin() alone, which 0120 ties to the
-- platform login, so a forged role GUC on the request login matches nothing.
--
-- Previous release: it never reads these objects; the new column has a
-- default. Nothing here needs a superuser or a SET clause on a function.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_mfa_factors (
    user_id                 uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tenant_id               uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    totp_secret_ciphertext  bytea NOT NULL,
    confirmed_at            timestamptz,
    last_used_step          bigint,
    failed_attempts         integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE user_mfa_factors IS
    'Two-step sign-in (TOTP) per user; the secret is sealed with the tenant key (0150, L17-19).';
COMMENT ON COLUMN user_mfa_factors.last_used_step IS
    'The last accepted 30-second step; a code at or before it is refused (replay).';

CREATE INDEX IF NOT EXISTS idx_user_mfa_factors_tenant ON user_mfa_factors (tenant_id);

CREATE TABLE IF NOT EXISTS user_mfa_recovery_codes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code_hash   text NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, code_hash)
);

CREATE INDEX IF NOT EXISTS idx_user_mfa_recovery_codes_tenant ON user_mfa_recovery_codes (tenant_id);

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS require_mfa boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN tenants.require_mfa IS
    'Set by a broker owner: every member must use two-step sign-in (0150). Broker owners always must.';

ALTER TABLE user_mfa_factors ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_mfa_factors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_mfa_factors_platform_only ON user_mfa_factors;
CREATE POLICY user_mfa_factors_platform_only ON user_mfa_factors
    USING (app_is_platform_admin())
    WITH CHECK (app_is_platform_admin());

ALTER TABLE user_mfa_recovery_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_mfa_recovery_codes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_mfa_recovery_codes_platform_only ON user_mfa_recovery_codes;
CREATE POLICY user_mfa_recovery_codes_platform_only ON user_mfa_recovery_codes
    USING (app_is_platform_admin())
    WITH CHECK (app_is_platform_admin());

-- Default privileges hand the app arwd on new tables; say exactly what it gets.
REVOKE ALL ON user_mfa_factors FROM PUBLIC;
REVOKE ALL ON user_mfa_recovery_codes FROM PUBLIC;
REVOKE TRUNCATE ON user_mfa_factors FROM oracle_app;
REVOKE TRUNCATE ON user_mfa_recovery_codes FROM oracle_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_mfa_factors TO oracle_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_mfa_recovery_codes TO oracle_app;
