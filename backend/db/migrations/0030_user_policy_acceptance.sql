-- 0030_user_policy_acceptance.sql
-- Record a one-time platform-policy acknowledgement for newly registered
-- accounts. Existing users default to not-required so this does not
-- retroactively lock them out; registration explicitly marks new users pending.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS policy_acceptance_required boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS user_policy_acceptances (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    policy_version text NOT NULL,
    accepted_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, policy_version)
);

CREATE INDEX IF NOT EXISTS idx_user_policy_acceptances_tenant_user
    ON user_policy_acceptances (tenant_id, user_id, accepted_at DESC);

ALTER TABLE user_policy_acceptances ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_policy_acceptances FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_policy_acceptances_tenant_isolation ON user_policy_acceptances;
CREATE POLICY user_policy_acceptances_tenant_isolation ON user_policy_acceptances
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
