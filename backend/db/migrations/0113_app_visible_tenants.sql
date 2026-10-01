-- ---------------------------------------------------------------------------
-- 0113 — app_visible_tenants(): the tenant set RLS allows, as an INDEXABLE value.
--
-- Nearly every tenant policy is
--     USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
-- and the planner cannot turn an OR with a non-column arm into an index
-- condition. So any query without another selective predicate filters the
-- WHOLE table — every tenant's rows — through the policy. Measured on the
-- Work list (Mission 7): a 200-client tenant scanned all 33,815 clients
-- ("Rows Removed by Filter: 33615"), and that page selection was two thirds of
-- the platform's hottest query. Its cost grows with the platform, not the tenant.
--
-- A query may add  AND tenant_id = ANY (app_visible_tenants())  to give the
-- planner an index condition. It is SAFE in both directions:
--   * it is ANDed with the policy, so it can only narrow what a request sees;
--   * for a platform admin it returns every tenant, so — unlike duplicating
--     half the policy as `tenant_id = app_current_tenant()` — it hides nothing
--     from an admin (see the client-portal incident in the RLS notes).
--
-- The policies themselves are unchanged. Rewriting 123 isolation predicates
-- is a security change that needs its own review, not a performance patch.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app_visible_tenants() RETURNS uuid[]
    LANGUAGE sql STABLE AS
$$ SELECT CASE WHEN app_is_platform_admin() THEN ARRAY(SELECT id FROM tenants)
               ELSE ARRAY[app_current_tenant()] END $$;

-- 0003 revoked EXECUTE from PUBLIC on every function; a new one is unusable by
-- the app until granted.
GRANT EXECUTE ON FUNCTION app_visible_tenants() TO oracle_app;
