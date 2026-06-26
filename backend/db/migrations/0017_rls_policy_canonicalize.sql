-- ===========================================================================
-- 0017_rls_policy_canonicalize.sql — converge tenant-isolation RLS to the 0001
-- canonical helper functions.
--
-- Tables in 0013 (state compliance) and 0015 (outreach consent) shipped their
-- `tenant_isolation` policy with a hand-rolled check:
--
--     USING (
--         current_setting('app.current_tenant', true) = '00000000-…-000000000000'
--         OR tenant_id::text = current_setting('app.current_tenant', true)
--     )
--
-- That sentinel-UUID arm is NOT how platform admins are identified. Admin
-- sessions carry app.current_role = 'platform_admin' with a REAL tenant GUC, so
-- the sentinel never matches and admins silently lose cross-tenant visibility on
-- these tables. The standard 0001 posture keys off the ROLE, not a magic tenant:
--
--     USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
--
-- This migration rewrites all seven affected policies to that canonical form.
-- 0015's source was also corrected in place; this file converges any database
-- that already applied the buggy 0013/0015 (init scripts only run once per
-- fresh volume, so existing volumes need this forward-only fix).
--
-- Idempotent: DROP POLICY IF EXISTS + recreate; safe to re-apply.
-- ===========================================================================

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        -- 0013_state_compliance.sql
        'agent_licenses',
        'agent_ce_log',
        'transactions',
        'compliance_checklist_items',
        -- 0015_outreach_consent.sql
        'outreach_consent',
        'outreach_suppression',
        'outreach_attempt_log'
    ]
    LOOP
        -- Skip cleanly if an upstream migration that creates the table was not
        -- applied (DROP POLICY IF EXISTS only suppresses a missing *policy*, not
        -- a missing *table*).
        CONTINUE WHEN to_regclass(t) IS NULL;
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (app_is_platform_admin() OR tenant_id = app_current_tenant())',
            t
        );
    END LOOP;
END $$;
