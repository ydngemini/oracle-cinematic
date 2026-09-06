-- 0092 — finish what 0091 started: earthdistance needs its whole call chain
--
-- 0091 granted ll_to_earth and earth_distance, and radius search still failed:
--
--     ERROR: permission denied for function earth
--
-- ll_to_earth does not stand alone. It builds a cube through the `earth`
-- domain's own functions and earth_distance compares them via cube operators,
-- so granting the two names the application source happens to mention leaves
-- the call chain broken one level down. Naming functions by hand is what
-- produced this class of bug three times already — 0050 granted the table and
-- not the function, 0008 granted four RLS helpers and missed the fifth, 0091
-- granted the two spelled out in the Python and missed their callees.
--
-- So this grants by MEMBERSHIP rather than by name: every function belonging to
-- the earthdistance and cube extensions. An extension's functions exist to be
-- called; 0003's blanket
--     REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC
-- swept them up as collateral, and this restores exactly that set to the app
-- role and nothing else. pg_depend is the authority on membership, so the set
-- cannot drift from what the extension actually installs.
--
-- Idempotent. Re-granting an existing privilege is a no-op.

DO $$
DECLARE
    fn record;
    granted int := 0;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure AS sig
          FROM pg_depend d
          JOIN pg_extension e ON e.oid = d.refobjid
          JOIN pg_proc p      ON p.oid = d.objid
         WHERE d.refclassid = 'pg_extension'::regclass
           AND d.classid    = 'pg_proc'::regclass
           AND e.extname IN ('earthdistance', 'cube')
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO oracle_app', fn.sig);
        granted := granted + 1;
    END LOOP;
    RAISE NOTICE 'granted EXECUTE on % earthdistance/cube functions', granted;
END $$;
