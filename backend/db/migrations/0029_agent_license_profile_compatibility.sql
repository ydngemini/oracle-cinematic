-- ===========================================================================
-- 0029_agent_license_profile_compatibility.sql
--
-- Reconcile the original state-compliance agent_licenses table (0013) with
-- the brokerage-profile fields introduced in 0027.  0027 used CREATE TABLE
-- IF NOT EXISTS, so an existing 0013 table kept its legacy shape and profile
-- endpoints that use user_id / verification_status failed at runtime.
--
-- This is additive and idempotent: legacy state-compliance consumers retain
-- agent_id, expiry_date, status and CE columns while brokerage consumers gain
-- user_id, expires_on and verification metadata.  No licence row is dropped.
-- ===========================================================================

ALTER TABLE agent_licenses
    ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS expires_on date,
    ADD COLUMN IF NOT EXISTS verification_status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS verified_by text,
    ADD COLUMN IF NOT EXISTS verified_at timestamptz;

-- Carry the legacy expiry field forward and link rows to their tenant-local
-- authenticated user where that identity already exists.
UPDATE agent_licenses
   SET expires_on = expiry_date
 WHERE expires_on IS NULL
   AND expiry_date IS NOT NULL;

UPDATE agent_licenses AS license
   SET user_id = users.id
  FROM users
 WHERE license.user_id IS NULL
   AND license.tenant_id = users.tenant_id
   AND lower(license.agent_id) = lower(users.agent_id);

-- New profile rows always have a user_id.  Older rows without a matching user
-- remain readable through the legacy state-compliance route instead of being
-- deleted or assigned across tenants.
CREATE INDEX IF NOT EXISTS idx_al_tenant_user
    ON agent_licenses(tenant_id, user_id)
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_al_user_state
    ON agent_licenses(user_id, state_code)
    WHERE user_id IS NOT NULL;
