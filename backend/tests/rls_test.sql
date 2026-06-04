-- ===========================================================================
-- rls_test.sql — runtime proof that the Postgres Row-Level Security policies in
-- 0001_init_tenancy.sql + 0002_listing_grants.sql actually enforce Hybrid
-- Tenant Isolation (ADR 006). Run AFTER those migrations, as a superuser, via
-- tests/run_rls.sh. Assertions execute as the non-superuser group role
-- `oracle_app` (via SET LOCAL ROLE) so RLS genuinely binds.
--
-- This is the DB-side mirror of tests/test_tenancy.py — same scenarios, so a
-- green run on both proves the Python predicates and SQL policies are lockstep.
-- ===========================================================================

\set ON_ERROR_STOP on

-- --- assertion helper (arithmetic + RAISE only; safe under any role) --------
CREATE OR REPLACE FUNCTION _assert(actual int, expected int, msg text)
    RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'ASSERT FAIL [%]: got %, want %', msg, actual, expected;
    END IF;
    RAISE NOTICE '  ok  % (= %)', msg, expected;
END $$;

-- --- seed (runs as superuser → RLS bypassed, so we can plant cross-tenant) ---
-- Tenant A = Apex (11111111…), Tenant B = Beta (22222222…)
INSERT INTO tenants (id, slug, name) VALUES
    ('11111111-1111-1111-1111-111111111111', 'apex', 'Apex Holdings'),
    ('22222222-2222-2222-2222-222222222222', 'beta', 'Beta Realty');

INSERT INTO users (tenant_id, agent_id, role) VALUES
    ('11111111-1111-1111-1111-111111111111', 'apex_owner', 'broker_owner'),
    ('22222222-2222-2222-2222-222222222222', 'beta_owner', 'broker_owner');

INSERT INTO clients (tenant_id, full_name) VALUES
    ('11111111-1111-1111-1111-111111111111', 'A client'),
    ('22222222-2222-2222-2222-222222222222', 'B client');

INSERT INTO leads (tenant_id, parcel_id, state, motivation_score) VALUES
    ('11111111-1111-1111-1111-111111111111', 'PA-1', 'PA', 90),
    ('22222222-2222-2222-2222-222222222222', 'NJ-1', 'NJ', 85);

-- L1 A-private, L2 A-shared(MLS), L3 B-private, L4 B-private-but-granted-to-A
INSERT INTO listings (id, tenant_id, address, price, is_shared_mls) VALUES
    ('aaaaaaaa-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', '1 Private Ave',  100, false),
    ('aaaaaaaa-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111', '2 Shared Blvd',  200, true),
    ('bbbbbbbb-0000-0000-0000-000000000003', '22222222-2222-2222-2222-222222222222', '3 Beta Private', 300, false),
    ('bbbbbbbb-0000-0000-0000-000000000004', '22222222-2222-2222-2222-222222222222', '4 Beta Granted', 400, false);

INSERT INTO listing_grants (listing_id, grantor_tenant_id, grantee_tenant_id, permission) VALUES
    ('bbbbbbbb-0000-0000-0000-000000000004',
     '22222222-2222-2222-2222-222222222222',
     '11111111-1111-1111-1111-111111111111', 'co_broke');

-- ===========================================================================
-- Scenario A — broker_owner of tenant A (READ visibility)
-- ===========================================================================
BEGIN;
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant', '11111111-1111-1111-1111-111111111111', true);
SELECT set_config('app.current_role',   'broker_owner', true);
\echo 'Scenario A (tenant A, broker_owner):'
SELECT _assert((SELECT count(*)::int FROM clients),  1, 'A sees only own clients');
SELECT _assert((SELECT count(*)::int FROM leads),    1, 'A sees only own leads');
SELECT _assert((SELECT count(*)::int FROM users),    1, 'A sees only own users');
SELECT _assert((SELECT count(*)::int FROM listings), 3, 'A sees own(2)+granted(1), not B-private');
SELECT _assert((SELECT count(*)::int FROM listings WHERE id='bbbbbbbb-0000-0000-0000-000000000003'), 0, 'A cannot see B private listing L3');
SELECT _assert((SELECT count(*)::int FROM listings WHERE id='bbbbbbbb-0000-0000-0000-000000000004'), 1, 'A CAN see B granted listing L4 (co-broke)');
ROLLBACK;

-- ===========================================================================
-- Scenario B — broker_owner of tenant B (READ visibility)
-- ===========================================================================
BEGIN;
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant', '22222222-2222-2222-2222-222222222222', true);
SELECT set_config('app.current_role',   'broker_owner', true);
\echo 'Scenario B (tenant B, broker_owner):'
SELECT _assert((SELECT count(*)::int FROM clients),  1, 'B sees only own clients');
SELECT _assert((SELECT count(*)::int FROM listings), 3, 'B sees own(2)+A-shared(1), not A-private');
SELECT _assert((SELECT count(*)::int FROM listings WHERE id='aaaaaaaa-0000-0000-0000-000000000001'), 0, 'B cannot see A private listing L1');
SELECT _assert((SELECT count(*)::int FROM listings WHERE id='aaaaaaaa-0000-0000-0000-000000000002'), 1, 'B sees A shared listing L2 (public MLS)');
ROLLBACK;

-- ===========================================================================
-- Scenario ADMIN — platform_admin (god-mode bypass)
-- ===========================================================================
BEGIN;
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant', '00000000-0000-0000-0000-000000000000', true);
SELECT set_config('app.current_role',   'platform_admin', true);
\echo 'Scenario ADMIN (platform_admin):'
SELECT _assert((SELECT count(*)::int FROM clients),  2, 'admin sees all clients');
SELECT _assert((SELECT count(*)::int FROM listings), 4, 'admin sees all listings');
ROLLBACK;

-- ===========================================================================
-- Scenario ANON — no tenant, role resolves to 'anon'
-- ===========================================================================
BEGIN;
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant', '', true);
SELECT set_config('app.current_role',   '', true);
\echo 'Scenario ANON (unauthenticated context):'
SELECT _assert((SELECT count(*)::int FROM clients),  0, 'anon sees no private data');
SELECT _assert((SELECT count(*)::int FROM listings), 1, 'anon sees only public MLS pool (L2)');
ROLLBACK;

-- ===========================================================================
-- Scenario WRITES — owner-only mutation, even for shared/granted listings
-- ===========================================================================
BEGIN;
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant', '11111111-1111-1111-1111-111111111111', true);
SELECT set_config('app.current_role',   'broker_owner', true);
\echo 'Scenario WRITES (as tenant A):'
DO $$
DECLARE n int;
BEGIN
    UPDATE listings SET price = 111 WHERE id = 'aaaaaaaa-0000-0000-0000-000000000001';
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM _assert(n, 1, 'A updates own listing L1 -> 1 row');

    UPDATE listings SET price = 999 WHERE id = 'bbbbbbbb-0000-0000-0000-000000000003';
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM _assert(n, 0, 'A cannot update B private L3 -> 0 rows (RLS USING)');

    -- A holds a co-broke GRANT on L4 but grants never confer write rights
    UPDATE listings SET price = 999 WHERE id = 'bbbbbbbb-0000-0000-0000-000000000004';
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM _assert(n, 0, 'A cannot write granted L4 -> 0 rows (read-grant != write)');

    -- cross-tenant INSERT must be rejected by WITH CHECK (error 42501)
    BEGIN
        INSERT INTO clients (tenant_id, full_name)
        VALUES ('22222222-2222-2222-2222-222222222222', 'hijack');
        RAISE EXCEPTION 'SECURITY FAIL: A inserted a client into tenant B';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE '  ok  A blocked from inserting into tenant B (WITH CHECK)';
    END;

    -- legitimate own-tenant INSERT still works
    INSERT INTO clients (tenant_id, full_name)
    VALUES ('11111111-1111-1111-1111-111111111111', 'legit');
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM _assert(n, 1, 'A inserts own-tenant client -> 1 row');
END $$;
ROLLBACK;

-- Cross-check: B cannot write the listing A merely shared to the MLS pool
BEGIN;
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant', '22222222-2222-2222-2222-222222222222', true);
SELECT set_config('app.current_role',   'broker_owner', true);
DO $$
DECLARE n int;
BEGIN
    UPDATE listings SET price = 1 WHERE id = 'aaaaaaaa-0000-0000-0000-000000000002';
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM _assert(n, 0, 'B cannot write A shared listing L2 -> 0 rows');
END $$;
ROLLBACK;

\echo ''
\echo 'RLS: all assertions passed.'
