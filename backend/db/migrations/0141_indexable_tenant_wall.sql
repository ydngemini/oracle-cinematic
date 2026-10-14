-- ---------------------------------------------------------------------------
-- 0141 — the tenant wall becomes an index condition (L16-01, audit A10).
--
-- The permissive tenant policy on most tables is
--     USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
-- The planner cannot turn an OR with a non-column arm into an index
-- condition, so every query without another selective predicate filtered the
-- WHOLE table — every brokerage's rows — through the policy. Measured on the
-- local dev DB (52 tenants, 35,961 clients, 9.05M leads; protocol in
-- performance/rls_tenant_wall_plans.sh): a 253-client brokerage counting its
-- clients seq-scanned 35,961 rows; a contact phone-hash lookup seq-scanned
-- every brokerage's contacts; the leads pipeline page for a small brokerage
-- walked the platform-wide ranking index and did not finish in 5 s.
--
-- This rewrites those policies, in place and under their own names, to
--
--     USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
--
-- where both bounds are app_current_tenant() for an ordinary request, and the
-- lowest and highest uuid for a platform admin.
--
-- Why exactly this shape (each alternative was measured and failed):
--   * A range on tenant_id whose bounds do not depend on the row is an index
--     condition on every tenant_id-leading index.
--   * The bounds are plpgsql functions. SQL functions would be inlined, and
--     app_current_role()'s COALESCE would make the expression "leaky" to the
--     planner; a leaky RLS qual is never used as an index condition (it stayed
--     a seq scan). A plpgsql call is opaque and leak-safe, and the planner
--     still pre-evaluates it (STABLE) when it estimates, so an ordinary
--     request is costed with its own brokerage's statistics.
--   * Wrapping the bounds as InitPlans `(SELECT ...)` keeps estimates blind:
--     a small brokerage's leads page went back to the platform-wide scan.
--   * `tenant_id = ANY (app_visible_tenants())` (0113) is indexable too, but
--     for a platform admin it lists `tenants`, which hides every row whose
--     tenant row is gone. The local dev DB has such rows despite the foreign
--     keys (15 in lead_pipeline_counts, 120 in
--     tenant_contract_template_registrations), and erasure_ledger and
--     suppression_tombstones outlive their tenant by design. A range admits
--     them, as the old form did. As a plain call it also ran per row in
--     filters (2.5 s for 36k rows as an admin).
--   * WITH CHECK keeps the previous expression verbatim: write checks are per
--     row and never planned, so they gain nothing, and writes stay exactly as
--     they were.
--
-- Semantics are identical for every context and every row of a table whose
-- tenant_id is uuid NOT NULL (only those are rewritten):
--   * not a platform admin (any login, any app.current_role — including a
--     forged 'platform_admin' GUC on the request login, 0120): the bounds are
--     both app_current_tenant(), and on uuid's total order
--     `x >= t AND x <= t` is `x = t`; a NULL tenant context is NULL, invisible,
--     in both forms;
--   * platform admin (admin context on a login in platform_admin_role): the
--     old form admits every row; the range admits every non-NULL tenant_id.
-- audit_ledger (nullable tenant_id) and the two text-keyed tables keep the OR
-- form. tests/rls_tenant_wall.sql proves it per policy and context.
--
-- The restrictive agent-scope policies (0131, 0134) are untouched; they AND
-- with these exactly as before. app_is_platform_admin(), app_current_tenant()
-- and app_visible_tenants() are unchanged, so the previous release runs on
-- this schema unchanged.
--
-- Lock footprint: ALTER POLICY takes ACCESS EXCLUSIVE on each table, all in
-- the runner's one transaction, bounded by its lock_timeout (5 s).
--
-- Rollback (restores the OR form; WITH CHECK is already the old expression):
--   DO $$ DECLARE r record; BEGIN
--     FOR r IN SELECT c.relname, p.polname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
--               WHERE pg_get_expr(p.polqual, p.polrelid) LIKE '%app_tenant_floor()%'
--     LOOP EXECUTE format('ALTER POLICY %I ON %I USING (app_is_platform_admin() OR tenant_id = app_current_tenant())',
--                         r.polname, r.relname); END LOOP; END $$;
--   DROP FUNCTION app_tenant_floor(); DROP FUNCTION app_tenant_ceiling();
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app_tenant_floor() RETURNS uuid
    LANGUAGE plpgsql STABLE AS
$$
BEGIN
    -- plpgsql, not SQL, so the planner cannot inline it into the policy; see the header.
    IF public.app_is_platform_admin() THEN
        RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END IF;
    RETURN public.app_current_tenant();
END
$$;

CREATE OR REPLACE FUNCTION app_tenant_ceiling() RETURNS uuid
    LANGUAGE plpgsql STABLE AS
$$
BEGIN
    IF public.app_is_platform_admin() THEN
        RETURN 'ffffffff-ffff-ffff-ffff-ffffffffffff'::uuid;
    END IF;
    RETURN public.app_current_tenant();
END
$$;

COMMENT ON FUNCTION app_tenant_floor() IS
    'Low bound of the 0141 tenant wall: app_current_tenant(), or the lowest uuid for a platform admin (app_is_platform_admin()).';
COMMENT ON FUNCTION app_tenant_ceiling() IS
    'High bound of the 0141 tenant wall: app_current_tenant(), or the highest uuid for a platform admin (app_is_platform_admin()).';

-- 0003 revoked EXECUTE from PUBLIC on every function; grant explicitly.
REVOKE ALL ON FUNCTION app_tenant_floor() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_tenant_ceiling() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_tenant_floor() TO oracle_app;
GRANT EXECUTE ON FUNCTION app_tenant_ceiling() TO oracle_app;

DO $$
DECLARE
    old_form   text := '(app_is_platform_admin() OR (tenant_id = app_current_tenant()))';
    old_form_r text := '((tenant_id = app_current_tenant()) OR app_is_platform_admin())';
    wall text := 'tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling()';
    r record;
    n int := 0;
BEGIN
    FOR r IN
        SELECT c.relname, p.polname, p.polcmd,
               pg_get_expr(p.polqual, p.polrelid)      AS qual,
               pg_get_expr(p.polwithcheck, p.polrelid) AS chk
          FROM pg_policy p
          JOIN pg_class c ON c.oid = p.polrelid
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
         WHERE c.relnamespace = 'public'::regnamespace
           AND p.polpermissive
           AND pg_get_expr(p.polqual, p.polrelid) IN (old_form, old_form_r)
           AND a.atttypid = 'uuid'::regtype
           AND a.attnotnull
         ORDER BY c.relname, p.polname
    LOOP
        IF r.polcmd IN ('r', 'd') THEN       -- SELECT / DELETE: no WITH CHECK
            EXECUTE format('ALTER POLICY %I ON %I USING (%s)', r.polname, r.relname, wall);
        ELSE                                 -- ALL / UPDATE: keep the old check
            EXECUTE format('ALTER POLICY %I ON %I USING (%s) WITH CHECK (%s)',
                           r.polname, r.relname, wall, COALESCE(r.chk, r.qual));
        END IF;
        n := n + 1;
    END LOOP;
    -- Zero means the catalog text no longer matches (a server that deparses
    -- the old form differently): fail loudly rather than ship nothing.
    IF n = 0 THEN
        RAISE EXCEPTION '0141: no tenant-isolation policy matched the expected form';
    END IF;
    RAISE NOTICE '0141: % tenant-isolation policies now use the indexable wall', n;
END $$;
