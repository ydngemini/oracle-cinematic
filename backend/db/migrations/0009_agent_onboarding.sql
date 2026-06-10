-- ===========================================================================
-- 0009_agent_onboarding.sql
-- Oracle — formalize the Memory Core profile tables + 1-minute onboarding
-- fields (client-management roadmap, "Live Pulse" pillar).
--
-- user_profiles / user_interactions previously existed ONLY via
-- scripts/ignite_memory.py (MEMORY_SCHEMA_SQL stub in memory_core/
-- session_manager.py) — never in the migration chain, never under RLS.
-- This migration makes them first-class. CREATE IF NOT EXISTS keeps both
-- provisioning paths (ignite script on dev, migrations on prod) converging
-- on one shape.
--
-- Type note: tenant_id here is text (the stub's shape — ignite seeds
-- non-UUID demo tenants), so the RLS policies compare on the TEXT side
-- (app_current_tenant()::text). Casting the column to uuid instead would
-- make policy evaluation throw on any legacy demo row.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Tables (stub shape, verbatim) — no-ops where ignite already ran.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id           text PRIMARY KEY,
    tenant_id         text NOT NULL,
    target_mao_pct    numeric(4,3) DEFAULT 0.70,
    max_rehab_budget  numeric(12,2),
    target_markets    jsonb DEFAULT '[]'::jsonb,
    profile_summary   text DEFAULT '',
    summary_updated_at timestamptz
);

CREATE TABLE IF NOT EXISTS user_interactions (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL,
    tenant_id       text NOT NULL,
    role            text NOT NULL,
    content         text NOT NULL,
    token_estimate  int  NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_interactions_user_time
    ON user_interactions (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 2. Onboarding fields. Three lethal data points the mobile gate captures;
--    target ZIPs reuse the existing target_markets jsonb (already hydrated
--    into the dashboard by SESSION_RESTORED).
-- ---------------------------------------------------------------------------
ALTER TABLE user_profiles
    ADD COLUMN IF NOT EXISTS experience_level text
        CHECK (experience_level IS NULL OR experience_level IN ('scout', 'closer', 'fund')),
    ADD COLUMN IF NOT EXISTS monthly_deal_target text
        CHECK (monthly_deal_target IS NULL OR monthly_deal_target IN ('1-2', '3-5', '6+'));

-- ---------------------------------------------------------------------------
-- 3. RLS — ENABLE + FORCE per house style (0001/0008). Text-side compare per
--    the type note above. ignite_memory.py runs as the postgres superuser
--    locally, which bypasses RLS, so seeding is unaffected.
-- ---------------------------------------------------------------------------
ALTER TABLE user_profiles     ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles     FORCE  ROW LEVEL SECURITY;
ALTER TABLE user_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_interactions FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_profiles_tenant_isolation ON user_profiles;
CREATE POLICY user_profiles_tenant_isolation ON user_profiles
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant()::text)
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant()::text);

DROP POLICY IF EXISTS user_interactions_tenant_isolation ON user_interactions;
CREATE POLICY user_interactions_tenant_isolation ON user_interactions
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant()::text)
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant()::text);

-- ---------------------------------------------------------------------------
-- 4. Grants. user_interactions is an append-only conversation ledger —
--    same posture as interaction_logs in 0008: no UPDATE/DELETE for the app.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON user_profiles     TO oracle_app;
GRANT SELECT, INSERT         ON user_interactions TO oracle_app;
REVOKE UPDATE, DELETE        ON user_interactions FROM oracle_app;
GRANT USAGE ON SEQUENCE user_interactions_id_seq  TO oracle_app;
