-- ===========================================================================
-- 0003_hardening.sql
-- Oracle — Database hardening (decision 007). The SQL-applicable half of the
-- "Postgres Vault". Cluster-level knobs (force_ssl, scram, pgaudit preload)
-- live in the Aurora parameter group — see infra/HARDENING.md.
--
-- Layers added here:
--   1. Least privilege at the schema level  (REVOKE PUBLIC)
--   2. Passwordless IAM auth login role      (rds_iam)
--   3. Tamper-evident audit                  (pgaudit object + session audit)
--
-- FORCE RLS is already established in 0001 on every tenant table.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Least privilege. Postgres grants CONNECT/USAGE/CREATE to PUBLIC by
--    default — strip it so only explicitly-granted roles touch anything.
-- ---------------------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
-- oracle_app (group role from 0001) keeps its grants; nothing else has any.

-- ---------------------------------------------------------------------------
-- 2. Passwordless auth. The backend authenticates with a short-lived (15 min)
--    IAM token, never a static password. On RDS/Aurora the `rds_iam` role
--    enables IAM DB auth for a login role. The login role inherits row access
--    via the oracle_app group (so RLS still binds — it is NOT an owner).
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app_login') THEN
        CREATE ROLE oracle_app_login LOGIN;
    END IF;
END $$;

-- `rds_iam` is provided by RDS/Aurora. Guard so this migration still applies on
-- a vanilla local Postgres used for tests (where the role won't exist).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
        GRANT rds_iam TO oracle_app_login;
    END IF;
END $$;

GRANT oracle_app TO oracle_app_login;        -- inherit table grants, NOT ownership
ALTER ROLE oracle_app_login SET search_path = public;

-- ---------------------------------------------------------------------------
-- 3. Tamper-evident audit (pgaudit). Requires pgaudit in
--    shared_preload_libraries (parameter group) before CREATE EXTENSION.
--    Two-pronged:
--      * session audit  — log every write/DDL/role change by the app role
--      * object audit    — log all reads of the sensitive tables via an
--                          audit role + pgaudit.role, so a leak claim can be
--                          answered with "exactly who SELECTed what, when".
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgaudit;

-- Session audit: every mutation, schema change, and grant by the app.
ALTER ROLE oracle_app_login SET pgaudit.log = 'write, ddl, role';

-- Object audit: a NOLOGIN marker role; granting it SELECT on a table makes
-- pgaudit log every read of that table. pgaudit.role is set in the parameter
-- group to 'pgaudit_reader' (see infra/HARDENING.md).
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgaudit_reader') THEN
        CREATE ROLE pgaudit_reader NOLOGIN;
    END IF;
END $$;

GRANT SELECT ON clients, leads, listings, listing_grants TO pgaudit_reader;
