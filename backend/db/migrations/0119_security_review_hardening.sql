-- ---------------------------------------------------------------------------
-- 0119 — database findings from the 2026-10-01 security launch review.
--
-- Each block names the finding it closes (docs/security-launch-review.md).
-- None changes what a correctly-behaving request can see; each removes a way
-- for a request — or a query running as the app role — to do more than it
-- should.
-- ---------------------------------------------------------------------------

-- HOOK-1. One verified caller ID per number, platform-wide. Verification asks
-- the shared platform Twilio account whether a number is a verified Outgoing
-- Caller ID; once one brokerage had verified its number there, any other
-- tenant could point its route at the same number, "verify", and place calls
-- under the first brokerage's identity. A number can be verified for one
-- route only.
CREATE UNIQUE INDEX IF NOT EXISTS uq_telephony_routes_verified_caller_id
    ON telephony_routes (voice_caller_id_e164)
    WHERE voice_caller_id_verified;

-- RLS-2. Audit rows must belong to the tenant writing them. The insert policy
-- was WITH CHECK (true): any tenant context could append forged entries to any
-- other tenant's trail (or with no tenant at all). The ledger writer
-- (audit_ledger.py) runs under the platform context, which this still admits.
DROP POLICY IF EXISTS audit_ledger_insert ON audit_ledger;
CREATE POLICY audit_ledger_insert ON audit_ledger
    FOR INSERT
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- RLS-3. The migration ledger is the runner's, not the application's. The app
-- role held INSERT/UPDATE/DELETE through default privileges, so a compromised
-- request path could hide drift from the checksum check or make the runner
-- (which runs as the owner) re-apply a migration. Reads stay (health, the
-- get_database_stats tool).
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON schema_migrations FROM oracle_app;

-- RLS-4. SECURITY DEFINER functions must not resolve tables through pg_temp.
-- With search_path=public and no explicit pg_temp, PostgreSQL searches the
-- session's temp schema FIRST: a session could CREATE TEMP TABLE listing_grants
-- and make app_has_listing_grant() — running as its superuser owner — report a
-- grant for any listing. Pinning pg_temp last makes the real tables win.
ALTER FUNCTION app_has_listing_grant(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION resolve_portal_token(text) SET search_path = public, pg_temp;

-- …and the application has no use for temporary tables at all.
DO $$
BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END
$$;

-- RLS-6. media_blobs carries bytes, never edits: 0022 granted SELECT/INSERT/
-- DELETE and the live grant had drifted to include UPDATE.
REVOKE UPDATE, TRUNCATE ON media_blobs FROM oracle_app;
