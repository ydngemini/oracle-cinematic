-- ---------------------------------------------------------------------------
-- 0120 — platform-admin power comes from the database role, not a session GUC.
--
-- app_is_platform_admin() was `app_current_role() = 'platform_admin'`: a
-- session variable any connection may set for itself. Every tenant policy is
-- `app_is_platform_admin() OR tenant_id = app_current_tenant()`, so a single
-- injected `SELECT set_config('app.current_role','platform_admin',true)` on an
-- ordinary request connection read and wrote every brokerage's rows — and
-- passed the admin gates of the SECURITY DEFINER purge/job functions too
-- (security launch review RLS-1, 2026-10-01; reproduced as oracle_app_login).
--
-- Now an admin context must ALSO be running on a login that is a member of
-- platform_admin_role. The application holds two pools: request contexts on
-- oracle_app_login (never a member), platform contexts on
-- oracle_platform_login (member). db/connection.tenant_tx picks the pool from
-- the verified context, so nothing a query does can move it from one to the
-- other: role membership is fixed by the login, and only a superuser can
-- change session_user.
--
-- session_user, not current_user: inside a SECURITY DEFINER function
-- current_user is the (superuser) owner, who is a member of every role, so the
-- definer functions' admin gates would have opened for everyone.
--
-- What this does NOT change: a query on a request connection can still set
-- app.current_tenant to another tenant's id. Closing that needs a context the
-- connection cannot forge (see docs/security-launch-review.md, RLS-1 residual);
-- it is bounded by the absence of SQL injection, not by this migration.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_platform_login') THEN
        CREATE ROLE oracle_platform_login LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

-- Table and function privileges are oracle_app's, exactly as for request
-- connections; the only difference is the marker membership.
GRANT oracle_app TO oracle_platform_login;
GRANT platform_admin_role TO oracle_platform_login;

-- The marker no longer rides along with every application connection.
REVOKE platform_admin_role FROM oracle_app;
REVOKE platform_admin_role FROM oracle_app_login;

CREATE OR REPLACE FUNCTION app_is_platform_admin() RETURNS boolean
    LANGUAGE sql STABLE AS
$$ SELECT app_current_role() = 'platform_admin'
          AND pg_has_role(session_user, 'platform_admin_role', 'MEMBER') $$;

COMMENT ON FUNCTION app_is_platform_admin() IS
    'Platform admin = admin context AND a login in platform_admin_role (0120). The GUC alone grants nothing.';
