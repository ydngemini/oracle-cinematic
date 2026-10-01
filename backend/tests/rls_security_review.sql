-- rls_security_review.sql — the database half of the 2026-10-01 security
-- launch review, proved against real PostgreSQL as the application role.
--
-- The Python suite runs on fakes; these are properties only the database can
-- prove: tenant isolation for reads AND writes on the tables that carry
-- customer data and money, the session-revocation check (0117), the audit
-- insert policy, privilege hygiene, and SECURITY DEFINER search_path safety.
--
-- Run as superuser against a database with migrations through 0119 applied:
--     psql -U postgres -d oracle -v ON_ERROR_STOP=1 -f tests/rls_security_review.sql
-- It seeds, asserts, and rolls everything back. Any failure raises and stops.

\set ON_ERROR_STOP on
BEGIN;

-- ── Catalog properties (no seed needed) ─────────────────────────────────────
DO $$
DECLARE bad text;
BEGIN
  -- Every table with a tenant_id column is RLS-enabled AND forced.
  SELECT string_agg(c.relname, ', ') INTO bad
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind = 'r'
     AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                  AND a.attname = 'tenant_id' AND NOT a.attisdropped)
     AND NOT (c.relrowsecurity AND c.relforcerowsecurity);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'tenant tables without RLS+FORCE: %', bad; END IF;

  -- No write policy admits every row (audit_ledger_insert was WITH CHECK (true)).
  SELECT string_agg(tablename || '.' || policyname, ', ') INTO bad
    FROM pg_policies
   WHERE schemaname = 'public' AND cmd IN ('INSERT', 'UPDATE', 'ALL')
     AND with_check = 'true';
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'write policies with WITH CHECK (true): %', bad; END IF;

  -- Every SECURITY DEFINER function the app can execute pins pg_temp last.
  SELECT string_agg(p.oid::regprocedure::text, ', ') INTO bad
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'public' AND p.prosecdef
     AND has_function_privilege('oracle_app', p.oid, 'EXECUTE')
     AND NOT coalesce(array_to_string(p.proconfig, ',') LIKE '%search_path=%pg_temp%', false);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'SECURITY DEFINER without pg_temp in search_path: %', bad; END IF;

  -- The app cannot rewrite the migration ledger or make temp tables.
  IF has_table_privilege('oracle_app', 'schema_migrations', 'UPDATE')
     OR has_table_privilege('oracle_app', 'schema_migrations', 'DELETE')
     OR has_table_privilege('oracle_app', 'schema_migrations', 'INSERT') THEN
    RAISE EXCEPTION 'oracle_app can write schema_migrations';
  END IF;
  IF has_database_privilege('oracle_app', current_database(), 'TEMP') THEN
    RAISE EXCEPTION 'oracle_app can create temporary tables (definer shadowing)';
  END IF;

  -- Processed Stripe events cannot be rewritten or erased by the app.
  IF has_table_privilege('oracle_app', 'stripe_webhook_events', 'UPDATE')
     OR has_table_privilege('oracle_app', 'stripe_webhook_events', 'DELETE') THEN
    RAISE EXCEPTION 'oracle_app can rewrite stripe_webhook_events';
  END IF;

  -- Audit rows are append-only for the app.
  IF has_table_privilege('oracle_app', 'audit_ledger', 'UPDATE')
     OR has_table_privilege('oracle_app', 'audit_ledger', 'DELETE') THEN
    RAISE EXCEPTION 'oracle_app can rewrite audit_ledger';
  END IF;
END $$;

-- ── Seed two brokerages ──────────────────────────────────────────────────────
INSERT INTO tenants (id, slug, name) VALUES
  ('aaaaaaaa-0000-0000-0000-0000000005ec','secreview-a','Brokerage A'),
  ('bbbbbbbb-0000-0000-0000-0000000005ec','secreview-b','Brokerage B');
INSERT INTO users (id, tenant_id, agent_id, role, email, session_epoch) VALUES
  ('aaaaaaaa-1111-0000-0000-0000000005ec','aaaaaaaa-0000-0000-0000-0000000005ec','sec-agent-a@review.test','agent','sec-agent-a@review.test', 3),
  ('bbbbbbbb-1111-0000-0000-0000000005ec','bbbbbbbb-0000-0000-0000-0000000005ec','sec-owner-b@review.test','broker_owner','sec-owner-b@review.test', 0);
INSERT INTO clients (id, tenant_id, full_name) VALUES
  ('aaaaaaaa-2222-0000-0000-0000000005ec','aaaaaaaa-0000-0000-0000-0000000005ec','Client of A'),
  ('bbbbbbbb-2222-0000-0000-0000000005ec','bbbbbbbb-0000-0000-0000-0000000005ec','Client of B');
INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status) VALUES
  ('bbbbbbbb-0000-0000-0000-0000000005ec','cus_secreview_b','sub_secreview_b','active');

SET LOCAL ROLE oracle_app;

-- ── Session revocation (0117) ─────────────────────────────────────────────────
DO $$
BEGIN
  -- The live session: right account, tenant, role and epoch.
  PERFORM app_begin_session('aaaaaaaa-0000-0000-0000-0000000005ec','agent','sec-agent-a@review.test',
                            true,'aaaaaaaa-1111-0000-0000-0000000005ec',3);
  -- A token from before the last epoch bump (password change, reset, demotion).
  BEGIN
    PERFORM app_begin_session('aaaaaaaa-0000-0000-0000-0000000005ec','agent','sec-agent-a@review.test',
                              true,'aaaaaaaa-1111-0000-0000-0000000005ec',2);
    RAISE EXCEPTION 'stale epoch accepted';
  EXCEPTION WHEN invalid_authorization_specification THEN NULL; END;
  -- A validly signed claim of a role the account does not hold.
  BEGIN
    PERFORM app_begin_session('aaaaaaaa-0000-0000-0000-0000000005ec','broker_owner','sec-agent-a@review.test',
                              true,'aaaaaaaa-1111-0000-0000-0000000005ec',3);
    RAISE EXCEPTION 'forged role accepted';
  EXCEPTION WHEN invalid_authorization_specification THEN NULL; END;
  -- The account claimed into another tenant.
  BEGIN
    PERFORM app_begin_session('bbbbbbbb-0000-0000-0000-0000000005ec','agent','sec-agent-a@review.test',
                              true,'aaaaaaaa-1111-0000-0000-0000000005ec',3);
    RAISE EXCEPTION 'forged tenant accepted';
  EXCEPTION WHEN invalid_authorization_specification THEN NULL; END;
  -- Legacy token shape (no uid) is still epoch-checked.
  BEGIN
    PERFORM app_begin_session('aaaaaaaa-0000-0000-0000-0000000005ec','agent','SEC-AGENT-A@review.test',
                              true,NULL,0);
    RAISE EXCEPTION 'legacy pre-bump token accepted';
  EXCEPTION WHEN invalid_authorization_specification THEN NULL; END;
END $$;

-- ── Tenant A's identity: reads and writes against Brokerage B ─────────────────
SELECT app_begin_session('aaaaaaaa-0000-0000-0000-0000000005ec','agent','sec-agent-a@review.test',
                         true,'aaaaaaaa-1111-0000-0000-0000000005ec',3);
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM clients WHERE tenant_id = 'bbbbbbbb-0000-0000-0000-0000000005ec';
  IF n <> 0 THEN RAISE EXCEPTION 'A read % of B''s clients', n; END IF;
  SELECT count(*) INTO n FROM subscriptions WHERE tenant_id = 'bbbbbbbb-0000-0000-0000-0000000005ec';
  IF n <> 0 THEN RAISE EXCEPTION 'A read B''s subscription'; END IF;
  SELECT count(*) INTO n FROM users WHERE tenant_id = 'bbbbbbbb-0000-0000-0000-0000000005ec';
  IF n <> 0 THEN RAISE EXCEPTION 'A read B''s users'; END IF;

  UPDATE clients SET full_name = 'pwned' WHERE id = 'bbbbbbbb-2222-0000-0000-0000000005ec';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'A updated B''s client'; END IF;
  DELETE FROM clients WHERE id = 'bbbbbbbb-2222-0000-0000-0000000005ec';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'A deleted B''s client'; END IF;
  UPDATE subscriptions SET status = 'canceled' WHERE stripe_subscription_id = 'sub_secreview_b';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'A changed B''s billing state'; END IF;

  BEGIN
    INSERT INTO clients (tenant_id, full_name) VALUES ('bbbbbbbb-0000-0000-0000-0000000005ec','planted');
    RAISE EXCEPTION 'A inserted into B';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    UPDATE clients SET tenant_id = 'bbbbbbbb-0000-0000-0000-0000000005ec'
     WHERE id = 'aaaaaaaa-2222-0000-0000-0000000005ec';
    RAISE EXCEPTION 'A moved its own row into B';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status)
    VALUES ('bbbbbbbb-0000-0000-0000-0000000005ec','cus_x','sub_planted','active');
    RAISE EXCEPTION 'A granted B a subscription';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  -- RLS-2: an audit row for another tenant (no RETURNING: the SELECT policy
  -- would refuse the returned row and mask what the INSERT policy decided).
  BEGIN
    INSERT INTO audit_ledger (tenant_id, user_id, category, action, metadata, prev_hash, entry_hash)
    VALUES ('bbbbbbbb-0000-0000-0000-0000000005ec','attacker','EXPORT_LEAD','forged','{}','x','y');
    RAISE EXCEPTION 'A forged an audit row into B';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  -- RLS-4: no temp table to shadow a definer's lookup.
  BEGIN
    CREATE TEMP TABLE listing_grants (listing_id uuid, grantee_tenant_id uuid);
    RAISE EXCEPTION 'app role created a temp table';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;

-- ── No context at all: nothing ───────────────────────────────────────────────
SELECT set_config('app.current_tenant', '', true), set_config('app.current_role', '', true);
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM clients WHERE id IN
    ('aaaaaaaa-2222-0000-0000-0000000005ec','bbbbbbbb-2222-0000-0000-0000000005ec');
  IF n <> 0 THEN RAISE EXCEPTION 'unset context read % client rows', n; END IF;
END $$;

\echo 'rls_security_review: PASS'
ROLLBACK;
