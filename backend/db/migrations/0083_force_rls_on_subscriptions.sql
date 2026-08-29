-- 0083 — FORCE row-level security everywhere it is merely enabled
--
-- `subscriptions` was the only tenant-scoped table with RLS enabled but not
-- FORCED: 111 of 112 were forced, it was not. Enabled-but-not-forced means the
-- table OWNER is exempt from every policy, so isolation on that table holds only
-- as long as nothing connects as the owner.
--
-- Production connects as oracle_app_login, which owns nothing, so this was not
-- live exposure — it is the difference between "isolated" and "isolated unless
-- someone changes the connection role". That distinction is worth closing on
-- the billing table in particular, because the row holds stripe_customer_id and
-- a leak there is one tenant reading another's payment identity.
--
-- The loop rather than a single ALTER: the same omission is easy to repeat, and
-- a table added between this migration and the next fresh deploy would carry it
-- silently. Anything tenant-scoped with a policy gets forced.
--
-- Idempotent. FORCE on an already-forced table is a no-op.

DO $$
DECLARE
    target text;
    fixed  int := 0;
BEGIN
    FOR target IN
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relrowsecurity
           AND NOT c.relforcerowsecurity
           AND EXISTS (
               SELECT 1 FROM information_schema.columns col
                WHERE col.table_schema = 'public'
                  AND col.table_name = c.relname
                  AND col.column_name = 'tenant_id'
           )
    LOOP
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', target);
        RAISE NOTICE 'forced row-level security on %', target;
        fixed := fixed + 1;
    END LOOP;

    RAISE NOTICE '0083: forced row-level security on % table(s)', fixed;
END $$;
