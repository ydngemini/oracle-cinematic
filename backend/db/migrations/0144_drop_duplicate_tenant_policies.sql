-- ---------------------------------------------------------------------------
-- 0144 — drop the duplicate tenant wall on agent_licenses and transactions
-- (L16-03, audit A10).
--
-- Each of these tables can carry the tenant wall twice, as two PERMISSIVE
-- policies:
--   * `tenant_isolation`            — 0013, recreated by 0017;
--   * `<table>_tenant_isolation`    — 0027 (agent_licenses), 0039 (transactions).
-- On a database that ran the files in order, transactions has only the 0039
-- one (0039 drops `tenant_isolation`), and agent_licenses has both. A database
-- that recorded 0017 after 0039 (the local dev DB did, 2026-08-18) has both on
-- both tables.
--
-- Permissive policies OR together, so a second one is never harmless by
-- construction: if it differs it widens access, and an OR of two quals is
-- not an index condition unless the planner can fold them. Re-verified on the
-- local dev DB at 0141 (pg_policies): the two are IDENTICAL — same command
-- (ALL), same roles (PUBLIC), same USING (the 0141 wall) and same WITH CHECK
-- (the old OR form). Dropping one changes nothing a request can see or write;
-- tests/rls_tenant_wall.sql section E proves it row by row in the seven
-- session contexts.
--
-- So this drops `tenant_isolation` ONLY where `<table>_tenant_isolation` exists
-- and matches it exactly (permissive, command, roles, USING, WITH CHECK). Any
-- difference keeps both and raises a WARNING: a policy that grants something
-- extra is a finding for review, not something a migration deletes.
--
-- Previous release: unaffected (no function, grant or visible row changes).
-- Lock footprint: DROP POLICY takes ACCESS EXCLUSIVE on the table, briefly,
-- inside the runner's one transaction (lock_timeout 5 s).
-- Rollback (recreates the dropped twin from the kept one):
--   CREATE POLICY tenant_isolation ON <table>
--     USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
--     WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    t text;
    dup record;
    keep record;
BEGIN
    FOREACH t IN ARRAY ARRAY['agent_licenses', 'transactions'] LOOP
        CONTINUE WHEN to_regclass('public.' || t) IS NULL;
        SELECT p.polpermissive, p.polcmd, p.polroles,
               pg_get_expr(p.polqual, p.polrelid)      AS qual,
               pg_get_expr(p.polwithcheck, p.polrelid) AS chk
          INTO dup
          FROM pg_policy p
         WHERE p.polrelid = ('public.' || t)::regclass AND p.polname = 'tenant_isolation';
        CONTINUE WHEN NOT FOUND;
        SELECT p.polpermissive, p.polcmd, p.polroles,
               pg_get_expr(p.polqual, p.polrelid)      AS qual,
               pg_get_expr(p.polwithcheck, p.polrelid) AS chk
          INTO keep
          FROM pg_policy p
         WHERE p.polrelid = ('public.' || t)::regclass AND p.polname = t || '_tenant_isolation';
        IF NOT FOUND THEN
            RAISE WARNING '0144: %.tenant_isolation kept: no %_tenant_isolation to fall back on', t, t;
            CONTINUE;
        END IF;
        IF dup.polpermissive AND keep.polpermissive
           AND dup.polcmd = keep.polcmd
           AND dup.polroles = keep.polroles
           AND dup.qual IS NOT DISTINCT FROM keep.qual
           AND dup.chk IS NOT DISTINCT FROM keep.chk THEN
            EXECUTE format('DROP POLICY tenant_isolation ON public.%I', t);
            RAISE NOTICE '0144: dropped %.tenant_isolation (identical to %_tenant_isolation)', t, t;
        ELSE
            RAISE WARNING '0144: %.tenant_isolation kept: it differs from %_tenant_isolation (USING % / %, CHECK % / %)',
                t, t, dup.qual, keep.qual, dup.chk, keep.chk;
        END IF;
    END LOOP;
END $$;
