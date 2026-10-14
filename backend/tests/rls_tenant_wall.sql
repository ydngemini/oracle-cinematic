-- rls_tenant_wall.sql — 0141's indexable tenant wall is the old wall, exactly,
-- and ordinary requests now plan on tenant indexes (L16-01, audit A10).
--
--   A. Catalog: every rewritten policy has the one canonical expression, sits
--      on a table where it is provably equivalent (tenant_id uuid NOT NULL),
--      and kept its write check verbatim; no eligible table is left on the old
--      OR form.
--   B. Pointwise: for every tenant id (and ids with no tenant row, and both
--      ends of uuid's order), the old and new expressions agree in
--      every session context (agent, owner, another brokerage, platform admin
--      on the platform login, a forged platform_admin GUC on the request login,
--      the platform login in a non-admin role, no context).
--   C. Plans: as an agent of a small brokerage next to a large one, the client
--      count, the client page and the contact phone-hash lookup use an index —
--      custom and generic plans (asyncpg prepares). A sequential scan fails.
--   D. Rows: for every rewritten table and context, the rows visible through
--      RLS are the same set under the new policies and, flipped back inside
--      this transaction, under the old ones.
--   E. Duplicates (0144, L16-03): no table carries two identical permissive
--      policies, and recreating the dropped twin on agent_licenses and
--      transactions changes no visible row in any context.
--
-- Run as superuser against a database with migrations through 0144 applied:
--     psql -U postgres -d oracle -v ON_ERROR_STOP=1 -f tests/rls_tenant_wall.sql
-- It seeds, asserts, and rolls everything back. Any failure raises and stops.

\set ON_ERROR_STOP on
-- One snapshot for the whole run: D compares two passes over live tables, and
-- on a shared database other sessions keep writing between them.
BEGIN ISOLATION LEVEL REPEATABLE READ;

-- ── A. Catalog ─────────────────────────────────────────────────────────────
DO $$
DECLARE
  old_form   text := '(app_is_platform_admin() OR (tenant_id = app_current_tenant()))';
  old_form_r text := '((tenant_id = app_current_tenant()) OR app_is_platform_admin())';
  bad text;
  n int;
  total int;
BEGIN
  -- One canonical expression across every rewritten policy.
  SELECT count(DISTINCT pg_get_expr(polqual, polrelid)), count(*) INTO n, total
    FROM pg_policy WHERE pg_get_expr(polqual, polrelid) LIKE '%app_tenant_floor()%';
  IF n <> 1 THEN RAISE EXCEPTION 'expected one tenant-wall expression, found %', n; END IF;
  IF total < 100 THEN RAISE EXCEPTION 'only % policies use the tenant wall', total; END IF;
  RAISE NOTICE 'A: % policies use the tenant wall', total;

  -- Each sits where it is equivalent: a permissive policy on tenant_id uuid NOT NULL.
  SELECT string_agg(c.relname || '.' || p.polname, ', ') INTO bad
    FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
    LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
   WHERE pg_get_expr(p.polqual, p.polrelid) LIKE '%app_tenant_floor()%'
     AND NOT (p.polpermissive AND a.atttypid = 'uuid'::regtype AND a.attnotnull);
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'tenant wall on a table where it is not equivalent: %', bad; END IF;

  -- Writes are unchanged: every rewritten ALL/UPDATE policy checks the old expression.
  SELECT string_agg(c.relname || '.' || p.polname, ', ') INTO bad
    FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
   WHERE pg_get_expr(p.polqual, p.polrelid) LIKE '%app_tenant_floor()%'
     AND p.polcmd IN ('*', 'w')
     AND pg_get_expr(p.polwithcheck, p.polrelid) IS DISTINCT FROM old_form
     AND pg_get_expr(p.polwithcheck, p.polrelid) IS DISTINCT FROM old_form_r;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'write check changed: %', bad; END IF;

  -- No eligible table is still on the old form (a new table copying the old
  -- policy would seq-scan every brokerage again).
  SELECT string_agg(c.relname || '.' || p.polname, ', ') INTO bad
    FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
   WHERE c.relnamespace = 'public'::regnamespace AND p.polpermissive
     AND pg_get_expr(p.polqual, p.polrelid) IN (old_form, old_form_r)
     AND a.atttypid = 'uuid'::regtype AND a.attnotnull;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'tenant policies still on the unindexable OR form (use the 0141 wall): %', bad;
  END IF;

  -- The hot tables are covered.
  SELECT string_agg(t, ', ') INTO bad
    FROM unnest(ARRAY['clients','agent_contacts','leads','client_activities','client_notes',
                      'client_tasks','sms_messages','users','record_shares',
                      'erasure_ledger','suppression_tombstones']) t
   WHERE NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = t::regclass AND p.polpermissive
                       AND pg_get_expr(p.polqual, p.polrelid) LIKE '%app_tenant_floor()%');
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'hot tables without the tenant wall: %', bad; END IF;

  -- No table carries the same permissive policy twice (0144). Permissive
  -- policies OR together: a twin is either a widening or dead weight.
  SELECT string_agg(rel || ' x' || cnt, ', ') INTO bad FROM (
    SELECT c.relname AS rel, count(*) AS cnt
      FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
     WHERE c.relnamespace = 'public'::regnamespace AND p.polpermissive
     GROUP BY c.relname, p.polcmd, p.polroles, pg_get_expr(p.polqual, p.polrelid),
              pg_get_expr(p.polwithcheck, p.polrelid)
    HAVING count(*) > 1) d;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'duplicate permissive policies: %', bad; END IF;

  -- The bounds: STABLE invoker-rights plpgsql, no SET clause, executable by both logins.
  SELECT string_agg(f, ', ') INTO bad
    FROM unnest(ARRAY['app_tenant_floor()', 'app_tenant_ceiling()']) f
   WHERE NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang
                      WHERE p.oid = f::regprocedure AND p.provolatile = 's' AND NOT p.prosecdef
                        AND p.proconfig IS NULL AND l.lanname = 'plpgsql')
      OR NOT has_function_privilege('oracle_app_login', f, 'EXECUTE')
      OR NOT has_function_privilege('oracle_platform_login', f, 'EXECUTE')
      OR has_function_privilege('public', f, 'EXECUTE');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'tenant-wall bounds must be STABLE invoker plpgsql, granted to the app only: %', bad;
  END IF;
END $$;

-- ── Seed: brokerage A (small) beside brokerage B (large) ───────────────────
INSERT INTO tenants (id, slug, name) VALUES
  ('a1410000-0000-4000-8000-00000000000a', 'wall-a', 'Wall A'),
  ('b1410000-0000-4000-8000-00000000000b', 'wall-b', 'Wall B')
ON CONFLICT (id) DO NOTHING;
INSERT INTO clients (tenant_id, full_name)
  SELECT 'a1410000-0000-4000-8000-00000000000a', 'A client ' || g FROM generate_series(1, 250) g;
INSERT INTO clients (tenant_id, full_name)
  SELECT 'b1410000-0000-4000-8000-00000000000b', 'B client ' || g FROM generate_series(1, 20000) g;
INSERT INTO agent_contacts (tenant_id, phone_lookup_hash)
  SELECT 'a1410000-0000-4000-8000-00000000000a', md5('a' || g) || md5('A' || g) FROM generate_series(1, 40) g;
INSERT INTO agent_contacts (tenant_id, phone_lookup_hash)
  SELECT 'b1410000-0000-4000-8000-00000000000b', md5('b' || g) || md5('B' || g) FROM generate_series(1, 20000) g;
INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score) VALUES
  ('a1410000-1111-4000-8000-00000000000a', 'a1410000-0000-4000-8000-00000000000a', 'WALL-A', 'DE', 1),
  ('b1410000-1111-4000-8000-00000000000b', 'b1410000-0000-4000-8000-00000000000b', 'WALL-B', 'DE', 2);
INSERT INTO agent_licenses (tenant_id, agent_id, license_number, state_code)
  SELECT t, 'wall-agent-' || g, 'WALL-' || g, 'DE'
    FROM unnest(ARRAY['a1410000-0000-4000-8000-00000000000a', 'b1410000-0000-4000-8000-00000000000b']::uuid[]) t,
         generate_series(1, 3) g;
INSERT INTO transactions (tenant_id, state_code, created_by)
  SELECT t, 'DE', 'wall-a-agent'
    FROM unnest(ARRAY['a1410000-0000-4000-8000-00000000000a', 'b1410000-0000-4000-8000-00000000000b']::uuid[]) t,
         generate_series(1, 3) g;
ANALYZE clients;
ANALYZE agent_contacts;

-- Session contexts used by B, C and D: label, login, tenant, role, agent.
CREATE TEMP TABLE wall_contexts (ord int, label text, login text, tenant text, role text, agent text);
INSERT INTO wall_contexts VALUES
  (1, 'agent of A',                         'oracle_app_login',      'a1410000-0000-4000-8000-00000000000a', 'agent',          'wall-a-agent'),
  (2, 'owner of A',                         'oracle_app_login',      'a1410000-0000-4000-8000-00000000000a', 'broker_owner',   'wall-a-owner'),
  (3, 'agent of B',                         'oracle_app_login',      'b1410000-0000-4000-8000-00000000000b', 'agent',          'wall-b-agent'),
  (4, 'platform admin, platform login',     'oracle_platform_login', '00000000-0000-0000-0000-000000000000', 'platform_admin', 'ops'),
  (5, 'forged admin GUC, request login',    'oracle_app_login',      'a1410000-0000-4000-8000-00000000000a', 'platform_admin', 'forged'),
  (6, 'agent role on the platform login',   'oracle_platform_login', 'a1410000-0000-4000-8000-00000000000a', 'agent',          'wall-a-agent'),
  (7, 'no context',                         'oracle_app_login',      '',                                     '',               '');
GRANT SELECT ON wall_contexts TO PUBLIC;

-- ── B. Pointwise equivalence over every tenant id ──────────────────────────
DO $$
DECLARE
  old_form text := '(app_is_platform_admin() OR (tenant_id = app_current_tenant()))';
  new_form text;
  domain uuid[];
  x record;
  mismatched bigint; visible bigint; edge_old bigint; edge_new bigint;
  expected bigint;
BEGIN
  SELECT DISTINCT pg_get_expr(polqual, polrelid) INTO new_form
    FROM pg_policy WHERE pg_get_expr(polqual, polrelid) LIKE '%app_tenant_floor()%';
  -- Every tenant, plus an id with no tenant row (orphans exist; erasure_ledger
  -- outlives its tenant) and both ends of uuid's order.
  SELECT array_agg(id) || ARRAY['dead0000-0000-4000-8000-000000000000',
                                '00000000-0000-0000-0000-000000000000',
                                'ffffffff-ffff-ffff-ffff-ffffffffffff']::uuid[]
    INTO domain FROM tenants;
  FOR x IN SELECT * FROM wall_contexts ORDER BY ord LOOP
    EXECUTE format('SET LOCAL SESSION AUTHORIZATION %I', x.login);
    PERFORM set_config('app.current_tenant', x.tenant, true),
            set_config('app.current_role', x.role, true),
            set_config('app.current_agent', x.agent, true),
            set_config('app.record_scope', 'actor', true);
    EXECUTE format(
      'SELECT count(*) FILTER (WHERE COALESCE((%1$s), false) IS DISTINCT FROM COALESCE((%2$s), false)),
              count(*) FILTER (WHERE COALESCE((%2$s), false))
         FROM unnest(%3$L::uuid[]) AS d(tenant_id)', old_form, new_form, domain)
      INTO mismatched, visible;
    -- The one value the precondition (NOT NULL) excludes.
    EXECUTE format(
      'SELECT count(*) FILTER (WHERE COALESCE((%1$s), false)), count(*) FILTER (WHERE COALESCE((%2$s), false))
         FROM (VALUES (NULL::uuid)) AS d(tenant_id)',
      old_form, new_form) INTO edge_old, edge_new;
    EXECUTE 'RESET SESSION AUTHORIZATION';
    expected := CASE x.ord WHEN 4 THEN cardinality(domain) WHEN 7 THEN 0 ELSE 1 END;
    IF mismatched <> 0 THEN
      RAISE EXCEPTION '[%] old and new tenant wall disagree on % tenant ids', x.label, mismatched;
    END IF;
    IF visible <> expected THEN
      RAISE EXCEPTION '[%] tenant wall admits % of % tenant ids, expected %', x.label, visible, cardinality(domain), expected;
    END IF;
    -- Only a platform admin ever saw a NULL tenant; NOT NULL makes it impossible.
    IF (x.ord = 4 AND (edge_old, edge_new) <> (1::bigint, 0::bigint)) OR (x.ord <> 4 AND (edge_old, edge_new) <> (0::bigint, 0::bigint)) THEN
      RAISE EXCEPTION '[%] NULL/orphan behaviour unexpected: old %, new %', x.label, edge_old, edge_new;
    END IF;
    RAISE NOTICE 'B [%]: 0 mismatches over % tenant ids, % visible', x.label, cardinality(domain), visible;
  END LOOP;
END $$;

-- ── C. Plans: an index, never a sequential scan ────────────────────────────
DO $$
DECLARE
  x record;
  q record;
  plan jsonb;
  mode text;
BEGIN
  FOR x IN SELECT * FROM wall_contexts WHERE ord IN (1, 2, 5) ORDER BY ord LOOP
    EXECUTE format('SET LOCAL SESSION AUTHORIZATION %I', x.login);
    PERFORM set_config('app.current_tenant', x.tenant, true),
            set_config('app.current_role', x.role, true),
            set_config('app.current_agent', x.agent, true),
            set_config('app.record_scope', 'actor', true);
    FOR q IN SELECT * FROM (VALUES
        ('client count',     'clients',        'SELECT count(*) FROM clients', ''),
        ('client page',      'clients',        'SELECT id, full_name FROM clients WHERE archived_at IS NULL ORDER BY lead_score DESC LIMIT $1', '50'),
        ('phone-hash lookup','agent_contacts', 'SELECT id FROM agent_contacts WHERE phone_lookup_hash = $1 AND deleted_at IS NULL', quote_literal(md5('a1') || md5('A1')))
      ) v(label, rel, sql, arg) LOOP
      FOREACH mode IN ARRAY ARRAY['auto', 'force_generic_plan'] LOOP
        PERFORM set_config('plan_cache_mode', mode, true);
        EXECUTE format('PREPARE wall_q AS %s', q.sql);
        EXECUTE format('EXPLAIN (FORMAT JSON) EXECUTE wall_q%s', CASE WHEN q.arg = '' THEN '' ELSE '(' || q.arg || ')' END)
          INTO plan;
        EXECUTE 'DEALLOCATE wall_q';
        IF jsonb_path_exists(plan, '$.** ? (@."Node Type" == "Seq Scan" && @."Relation Name" == $r)',
                             jsonb_build_object('r', q.rel)) THEN
          RAISE EXCEPTION '[%] % (%) seq-scans %: %', x.label, q.label, mode, q.rel, plan;
        END IF;
        RAISE NOTICE 'C [%] % (%): index', x.label, q.label, mode;
      END LOOP;
    END LOOP;
    PERFORM set_config('plan_cache_mode', 'auto', true);
    EXECUTE 'RESET SESSION AUTHORIZATION';
  END LOOP;
END $$;

-- ── D. Same visible rows under the old and the new policies ────────────────
DO $$
DECLARE
  old_form text := '(app_is_platform_admin() OR (tenant_id = app_current_tenant()))';
  tables text[];
  big text[];
  pols jsonb;
  p jsonb;
  x record;
  t text;
  phase text;
  n bigint; h bigint;
  seen jsonb := '{}';
  key text;
  diffs int := 0;
  scope text;
BEGIN
  SELECT array_agg(DISTINCT c.relname ORDER BY c.relname),
         jsonb_agg(jsonb_build_object('rel', c.relname, 'pol', p.polname, 'cmd', p.polcmd))
    INTO tables, pols
    FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
   WHERE pg_get_expr(p.polqual, p.polrelid) LIKE '%app_tenant_floor()%';
  -- A full count of leads would read the whole platform; compare the seeded
  -- brokerages there (indexable in both phases), everything elsewhere.
  SELECT array_agg(relname) INTO big FROM pg_class
   WHERE relname = ANY (tables) AND relnamespace = 'public'::regnamespace AND reltuples > 200000;
  big := COALESCE(big, '{}');

  FOREACH phase IN ARRAY ARRAY['new', 'old'] LOOP
    IF phase = 'old' THEN
      FOR p IN SELECT * FROM jsonb_array_elements(pols) LOOP
        EXECUTE format('ALTER POLICY %I ON %I USING (%s)', p->>'pol', p->>'rel', old_form);
      END LOOP;
    END IF;
    FOR x IN SELECT * FROM wall_contexts ORDER BY ord LOOP
      EXECUTE format('SET LOCAL SESSION AUTHORIZATION %I', x.login);
      PERFORM set_config('app.current_tenant', x.tenant, true),
              set_config('app.current_role', x.role, true),
              set_config('app.current_agent', x.agent, true),
              set_config('app.record_scope', 'actor', true);
      FOREACH t IN ARRAY tables LOOP
        scope := CASE WHEN t = ANY (big)
                      THEN ' WHERE tenant_id = ANY (''{a1410000-0000-4000-8000-00000000000a,b1410000-0000-4000-8000-00000000000b}''::uuid[])'
                      ELSE '' END;
        EXECUTE format('SELECT count(*), COALESCE(sum(hashtext(ctid::text)), 0) FROM %I%s', t, scope) INTO n, h;
        key := x.ord || '|' || t;
        IF phase = 'new' THEN
          seen := seen || jsonb_build_object(key, jsonb_build_array(n, h));
        ELSIF seen->key <> jsonb_build_array(n, h) THEN
          diffs := diffs + 1;
          RAISE WARNING 'D [%] %: new % rows vs old % rows (or a different set)', x.label, t, seen->key->0, n;
        END IF;
      END LOOP;
      EXECUTE 'RESET SESSION AUTHORIZATION';
    END LOOP;
  END LOOP;
  IF diffs > 0 THEN RAISE EXCEPTION '% (context, table) pairs see different rows', diffs; END IF;
  RAISE NOTICE 'D: % tables x % contexts: identical visible rows (% restricted to the seeded brokerages: %)',
    cardinality(tables), (SELECT count(*) FROM wall_contexts), cardinality(big), big;
  -- Sanity: the comparison saw something in each kind of context.
  IF (seen->('1|clients')->>0)::bigint < 250 OR (seen->('4|clients')->>0)::bigint < 20250
     OR (seen->('3|clients')->>0)::bigint < 20000 OR (seen->('7|clients')->>0)::bigint <> 0 THEN
    RAISE EXCEPTION 'D: unexpected client visibility %', seen->('1|clients');
  END IF;
END $$;

-- ── E. The dropped twin policies (0144) changed no visible row ─────────────
DO $$
DECLARE
  t text;
  x record;
  k record;
  phase text;
  n bigint; h bigint;
  seen jsonb := '{}';
  key text;
  diffs int := 0;
BEGIN
  FOREACH phase IN ARRAY ARRAY['without twin', 'with twin'] LOOP
    IF phase = 'with twin' THEN
      FOREACH t IN ARRAY ARRAY['agent_licenses', 'transactions'] LOOP
        SELECT pg_get_expr(polqual, polrelid) AS q, pg_get_expr(polwithcheck, polrelid) AS c INTO k
          FROM pg_policy WHERE polrelid = t::regclass AND polname = t || '_tenant_isolation';
        IF NOT FOUND THEN RAISE EXCEPTION 'E: %_tenant_isolation is missing', t; END IF;
        IF EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = t::regclass AND polname = 'tenant_isolation') THEN
          RAISE EXCEPTION 'E: %.tenant_isolation still exists (0144 not applied?)', t;
        END IF;
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (%s) WITH CHECK (%s)', t, k.q, k.c);
      END LOOP;
    END IF;
    FOR x IN SELECT * FROM wall_contexts ORDER BY ord LOOP
      EXECUTE format('SET LOCAL SESSION AUTHORIZATION %I', x.login);
      PERFORM set_config('app.current_tenant', x.tenant, true),
              set_config('app.current_role', x.role, true),
              set_config('app.current_agent', x.agent, true),
              set_config('app.record_scope', 'actor', true);
      FOREACH t IN ARRAY ARRAY['agent_licenses', 'transactions'] LOOP
        EXECUTE format('SELECT count(*), COALESCE(sum(hashtext(ctid::text)), 0) FROM %I', t) INTO n, h;
        key := x.ord || '|' || t;
        IF phase = 'without twin' THEN
          seen := seen || jsonb_build_object(key, jsonb_build_array(n, h));
        ELSIF seen->key <> jsonb_build_array(n, h) THEN
          diffs := diffs + 1;
          RAISE WARNING 'E [%] %: % rows without the twin vs % with it (or a different set)', x.label, t, seen->key->0, n;
        END IF;
      END LOOP;
      EXECUTE 'RESET SESSION AUTHORIZATION';
    END LOOP;
  END LOOP;
  IF diffs > 0 THEN RAISE EXCEPTION 'E: % (context, table) pairs see different rows with the twin', diffs; END IF;
  -- Sanity: agent of A sees A's 3 + 3, admin sees at least both brokerages', no context sees none.
  IF (seen->('1|agent_licenses')->>0)::bigint <> 3 OR (seen->('1|transactions')->>0)::bigint <> 3
     OR (seen->('4|agent_licenses')->>0)::bigint < 6 OR (seen->('7|transactions')->>0)::bigint <> 0 THEN
    RAISE EXCEPTION 'E: unexpected visibility %', seen;
  END IF;
  RAISE NOTICE 'E: agent_licenses and transactions: identical visible rows with and without the twin in % contexts',
    (SELECT count(*) FROM wall_contexts);
END $$;

ROLLBACK;
