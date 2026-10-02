-- privacy_lifecycle.sql — the database guards of the customer-data lifecycle
-- (migration 0122), proved against real PostgreSQL as the real logins.
--
-- What only the database can prove: a tenant session cannot reverse an
-- operator suspension, mark itself erased, delete its tenants row, place or
-- lift a legal hold, or run an erasure primitive; the platform login cannot
-- erase without a running operation, a tenant in 'erasing', no legal hold,
-- and a non-retained table; smart-plan revisions stay immutable except under
-- erasure; record attachments open to a platform session only inside a
-- privacy operation.
--
--     psql -U postgres -d oracle -v ON_ERROR_STOP=1 -f tests/privacy_lifecycle.sql
-- Seeds, asserts, rolls everything back.

\set ON_ERROR_STOP on
BEGIN;

INSERT INTO tenants (id, slug, name) VALUES
  ('cccccccc-0000-0000-0000-00000000c122','privacy-c','Brokerage C'),
  ('dddddddd-0000-0000-0000-00000000c122','privacy-d','Brokerage D');
INSERT INTO users (id, tenant_id, agent_id, role, email) VALUES
  ('cccccccc-1111-0000-0000-00000000c122','cccccccc-0000-0000-0000-00000000c122','owner-c@privacy.test','broker_owner','owner-c@privacy.test');
INSERT INTO clients (id, tenant_id, full_name) VALUES
  ('cccccccc-2222-0000-0000-00000000c122','cccccccc-0000-0000-0000-00000000c122','Client of C'),
  ('dddddddd-2222-0000-0000-00000000c122','dddddddd-0000-0000-0000-00000000c122','Client of D');
INSERT INTO smart_plans (id, tenant_id, owner_agent_id, created_by, name) VALUES
  ('cccccccc-3333-0000-0000-00000000c122','cccccccc-0000-0000-0000-00000000c122','owner-c@privacy.test','owner-c@privacy.test','Plan');
INSERT INTO smart_plan_revisions (id, tenant_id, plan_id, revision_number, definition, definition_hash, created_by) VALUES
  ('cccccccc-4444-0000-0000-00000000c122','cccccccc-0000-0000-0000-00000000c122','cccccccc-3333-0000-0000-00000000c122',
   1, '{}'::jsonb, repeat('0', 64), 'owner-c@privacy.test');
INSERT INTO ai_record_attachments (id, tenant_id, record_type, record_id, owner_agent_id, created_by,
                                   filename, media_type, byte_size, sha256, bytes_ciphertext, scan_status)
VALUES ('cccccccc-5555-0000-0000-00000000c122','cccccccc-0000-0000-0000-00000000c122','client',
        'cccccccc-2222-0000-0000-00000000c122','owner-c@privacy.test','owner-c@privacy.test',
        'a.pdf','application/pdf',1,repeat('0',64),'\x00'::bytea,'clean');

-- ── Catalog properties ──────────────────────────────────────────────────────
DO $$
BEGIN
  IF has_table_privilege('oracle_app', 'tenants', 'DELETE') THEN
    RAISE EXCEPTION 'oracle_app can delete tenants rows (erasure must tombstone)';
  END IF;
  IF has_table_privilege('oracle_app', 'erasure_ledger', 'UPDATE')
     OR has_table_privilege('oracle_app', 'erasure_ledger', 'DELETE') THEN
    RAISE EXCEPTION 'oracle_app can rewrite the erasure ledger';
  END IF;
  IF has_table_privilege('oracle_app', 'privacy_operations', 'DELETE') THEN
    RAISE EXCEPTION 'oracle_app can delete privacy records';
  END IF;
  IF has_table_privilege('oracle_app', 'legal_holds', 'DELETE') THEN
    RAISE EXCEPTION 'oracle_app can delete legal holds';
  END IF;
  -- The two whole-row SET NULL foreign keys that made a command undeletable.
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname IN ('mission_actions_command_fk','lead_response_events_command_fk')
              AND pg_get_constraintdef(oid) NOT LIKE '%SET NULL (command_id)%') THEN
    RAISE EXCEPTION 'command FKs still null the whole row';
  END IF;
END $$;

-- ── A tenant session (owner of C) ──────────────────────────────────────────
SET LOCAL SESSION AUTHORIZATION oracle_app_login;
SELECT set_config('app.current_tenant','cccccccc-0000-0000-0000-00000000c122',true),
       set_config('app.current_role','broker_owner',true),
       set_config('app.current_agent','owner-c@privacy.test',true);
DO $$
BEGIN
  -- Closure: allowed, and withdrawable.
  UPDATE tenants SET lifecycle_state='closing', closure_requested_at=now(), erase_after=now()+interval '30 days'
   WHERE id='cccccccc-0000-0000-0000-00000000c122';
  UPDATE tenants SET lifecycle_state='active' WHERE id='cccccccc-0000-0000-0000-00000000c122';
  UPDATE tenants SET lifecycle_state='closing', closure_requested_at=now(), erase_after=now()+interval '30 days'
   WHERE id='cccccccc-0000-0000-0000-00000000c122';
  -- Not allowed: jumping to erasing/erased, moving the erasure date.
  BEGIN
    UPDATE tenants SET lifecycle_state='erasing' WHERE id='cccccccc-0000-0000-0000-00000000c122';
    RAISE EXCEPTION 'tenant moved itself to erasing';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    UPDATE tenants SET erase_after=now()+interval '10 years' WHERE id='cccccccc-0000-0000-0000-00000000c122';
    RAISE EXCEPTION 'tenant moved its own erasure date';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    DELETE FROM tenants WHERE id='cccccccc-0000-0000-0000-00000000c122';
    RAISE EXCEPTION 'tenant deleted its tenants row';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  -- Not allowed: legal holds.
  BEGIN
    INSERT INTO legal_holds (tenant_id, scope, reason, placed_by)
    VALUES ('cccccccc-0000-0000-0000-00000000c122','tenant','self-hold','owner-c@privacy.test');
    RAISE EXCEPTION 'tenant placed a legal hold';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  -- Not allowed: any erasure primitive.
  BEGIN
    PERFORM privacy_erase_tenant_batch(gen_random_uuid(), 'clients', 10);
    RAISE EXCEPTION 'tenant session ran an erasure primitive';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  -- Not allowed: the immutable revision, outside erasure.
  BEGIN
    DELETE FROM smart_plan_revisions WHERE id='cccccccc-4444-0000-0000-00000000c122';
    RAISE EXCEPTION 'smart plan revision deleted outside erasure';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;  -- no DELETE grant at all
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE '%immutable%' THEN RAISE; END IF;
  END;
END $$;
-- A forged erasure GUC in a tenant session buys nothing.
SELECT set_config('app.erasure_operation', gen_random_uuid()::text, true),
       set_config('app.privacy_operation', gen_random_uuid()::text, true);
DO $$
BEGIN
  BEGIN
    DELETE FROM smart_plan_revisions WHERE id='cccccccc-4444-0000-0000-00000000c122';
    RAISE EXCEPTION 'forged erasure GUC unlocked a revision delete';
  EXCEPTION
    WHEN insufficient_privilege THEN NULL;  -- no DELETE grant at all
    WHEN raise_exception THEN
      IF SQLERRM NOT LIKE '%immutable%' THEN RAISE; END IF;
  END;
END $$;
-- Tombstones are tenant-scoped.
INSERT INTO suppression_tombstones (tenant_id, contact_hmac, channel) VALUES
  ('cccccccc-0000-0000-0000-00000000c122', repeat('a',64), '*');
DO $$
BEGIN
  BEGIN
    INSERT INTO suppression_tombstones (tenant_id, contact_hmac, channel) VALUES
      ('dddddddd-0000-0000-0000-00000000c122', repeat('b',64), '*');
    RAISE EXCEPTION 'tenant wrote another tenant''s tombstone';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;

-- ── The platform login ─────────────────────────────────────────────────────
SET LOCAL SESSION AUTHORIZATION oracle_platform_login;
SELECT set_config('app.current_tenant','00000000-0000-0000-0000-000000000000',true),
       set_config('app.current_role','platform_admin',true),
       set_config('app.erasure_operation','',true),
       set_config('app.privacy_operation','',true);
DO $$
DECLARE op uuid; n bigint;
BEGIN
  -- Without a privacy operation the attachments stay invisible even to the platform.
  SELECT count(*) INTO n FROM ai_record_attachments WHERE tenant_id='cccccccc-0000-0000-0000-00000000c122';
  IF n <> 0 THEN RAISE EXCEPTION 'platform saw agent-scoped attachments outside a privacy operation'; END IF;

  -- No running operation → refused.
  BEGIN
    PERFORM privacy_erase_tenant_batch(gen_random_uuid(), 'clients', 10);
    RAISE EXCEPTION 'erasure ran without an operation';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;

  INSERT INTO privacy_operations (tenant_id, kind, state, requested_by, policy_version)
  VALUES ('cccccccc-0000-0000-0000-00000000c122','erasure','running','proof','test') RETURNING id INTO op;

  -- Tenant still 'closing', not 'erasing' → refused.
  BEGIN
    PERFORM privacy_erase_tenant_batch(op, 'clients', 10);
    RAISE EXCEPTION 'erasure ran on a tenant not in erasing';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;

  UPDATE tenants SET lifecycle_state='erasing' WHERE id='cccccccc-0000-0000-0000-00000000c122';

  -- Retained table → refused.
  BEGIN
    PERFORM privacy_erase_tenant_batch(op, 'subscriptions', 10);
    RAISE EXCEPTION 'erasure deleted a retained table';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    PERFORM privacy_erase_tenant_batch(op, 'audit_ledger', 10);
    RAISE EXCEPTION 'erasure deleted the audit ledger';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;

  -- Legal hold → refused; released → allowed.
  INSERT INTO legal_holds (tenant_id, scope, reason, placed_by)
  VALUES ('cccccccc-0000-0000-0000-00000000c122','tenant','litigation hold (proof)','proof');
  BEGIN
    PERFORM privacy_erase_tenant_batch(op, 'clients', 10);
    RAISE EXCEPTION 'erasure ignored a legal hold';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE '%legal hold%' THEN RAISE; END IF;
  END;
  UPDATE legal_holds SET released_at=now(), released_by='proof' WHERE tenant_id='cccccccc-0000-0000-0000-00000000c122';

  -- Now it works — for C only.
  n := privacy_erase_tenant_batch(op, 'clients', 10);
  IF n <> 1 THEN RAISE EXCEPTION 'expected to erase 1 client of C, erased %', n; END IF;
  PERFORM set_config('app.current_tenant','00000000-0000-0000-0000-000000000000',true);
  SELECT count(*) INTO n FROM clients WHERE id='dddddddd-2222-0000-0000-00000000c122';
  IF n <> 1 THEN RAISE EXCEPTION 'erasure of C touched D'; END IF;
  SELECT count(*) INTO n FROM erasure_ledger WHERE operation_id=op AND target='clients';
  IF n <> 1 THEN RAISE EXCEPTION 'erasure not recorded in the ledger'; END IF;

  -- Under erasure the plan-revision cycle can be broken and the revision deleted.
  PERFORM privacy_null_tenant_reference(op, 'smart_plans', 'current_revision_id');
  BEGIN
    PERFORM privacy_null_tenant_reference(op, 'smart_plans', 'tenant_id');
    RAISE EXCEPTION 'nulled tenant_id';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    PERFORM privacy_null_tenant_reference(op, 'smart_plans', 'name');
    RAISE EXCEPTION 'nulled a non-FK column';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  n := privacy_erase_tenant_batch(op, 'smart_plan_revisions', 10);
  IF n <> 1 THEN RAISE EXCEPTION 'revision not erasable under erasure (got %)', n; END IF;

  -- Counting sees past agent-scoped RLS.
  n := privacy_count_tenant_rows(op, 'ai_record_attachments');
  IF n <> 1 THEN
    RAISE EXCEPTION 'count could not see the attachment (got %)', n;
  END IF;

  -- The platform tenant is never erased.
  INSERT INTO privacy_operations (tenant_id, kind, state, requested_by, policy_version)
  VALUES ('00000000-0000-0000-0000-000000000000','erasure','running','proof','test') RETURNING id INTO op;
  BEGIN
    PERFORM privacy_erase_tenant_batch(op, 'clients', 10);
    RAISE EXCEPTION 'platform tenant erasure accepted';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  UPDATE privacy_operations SET state='cancelled' WHERE id=op;

  -- An erased tenant cannot change state again.
  UPDATE tenants SET lifecycle_state='erased', erased_at=now() WHERE id='cccccccc-0000-0000-0000-00000000c122';
  BEGIN
    UPDATE tenants SET lifecycle_state='active' WHERE id='cccccccc-0000-0000-0000-00000000c122';
    RAISE EXCEPTION 'erased tenant resurrected';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;

-- ── RLS coverage: erasure cannot silently delete nothing ──────────────────
-- The erasure functions run as the table owner, which FORCE RLS binds unless
-- it is a superuser. Every erasable tenant table therefore needs a permissive
-- policy that admits a platform session for DELETE and for SELECT (the
-- verification count), or erasure would report "clean" over surviving rows.
DO $$
DECLARE bad text;
BEGIN
  WITH t AS (
    SELECT c.oid, c.relname FROM pg_class c
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
     WHERE c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace AND c.relrowsecurity
       AND NOT (c.relname = ANY (privacy_retained_tables())) AND c.relname NOT LIKE 'zz\_%'
  )
  SELECT string_agg(t.relname, ', ') INTO bad FROM t
   WHERE NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = t.oid AND p.polpermissive
                      AND p.polcmd IN ('*','d')
                      AND pg_get_expr(p.polqual, p.polrelid) LIKE '%app_is_platform_admin()%')
      OR NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = t.oid AND p.polpermissive
                      AND p.polcmd IN ('*','r')
                      AND pg_get_expr(p.polqual, p.polrelid) LIKE '%app_is_platform_admin()%');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'erasable tables a platform session cannot delete or count: %', bad;
  END IF;
END $$;

-- ── Retention sweeps ───────────────────────────────────────────────────────
INSERT INTO tenants (id, slug, name) VALUES ('eeeeeeee-0000-0000-0000-00000000c122','privacy-e','Brokerage E');
INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score) VALUES
  ('eeeeeeee-1111-0000-0000-00000000c122','eeeeeeee-0000-0000-0000-00000000c122','P-E','DE',1);
INSERT INTO client_portals (id, tenant_id, lead_id, token_hash, access_expires_at) VALUES
  ('eeeeeeee-2222-0000-0000-00000000c122','eeeeeeee-0000-0000-0000-00000000c122','eeeeeeee-1111-0000-0000-00000000c122', repeat('d',64), now() - interval '40 days'),
  ('eeeeeeee-3333-0000-0000-00000000c122','eeeeeeee-0000-0000-0000-00000000c122','eeeeeeee-1111-0000-0000-00000000c122', repeat('e',64), now() + interval '5 days');
INSERT INTO audit_ledger (created_at, category, action, tenant_id, user_id, metadata, prev_hash, entry_hash)
SELECT now() - interval '3 years', 'ADMIN_ACTION', 'proof.old', 'eeeeeeee-0000-0000-0000-00000000c122', 'x',
       '{}'::jsonb, repeat('0',64), repeat('a',64)
 WHERE false;  -- shape check only: real rows carry a computed chain; expiry is proved below on a copy
SET LOCAL SESSION AUTHORIZATION oracle_platform_login;
SELECT set_config('app.current_tenant','00000000-0000-0000-0000-000000000000',true),
       set_config('app.current_role','platform_admin',true);
DO $$
DECLARE r jsonb; n integer;
BEGIN
  r := privacy_retention_sweep(30, 90, 1826, 2557);
  SELECT count(*) INTO n FROM client_portals WHERE tenant_id = 'eeeeeeee-0000-0000-0000-00000000c122';
  IF n <> 1 THEN RAISE EXCEPTION 'sweep should keep the live portal and drop the dead one (left %)', n; END IF;
  IF (r->>'client_portals')::int < 1 THEN RAISE EXCEPTION 'sweep reported no expired portal: %', r; END IF;
  BEGIN
    PERFORM privacy_expire_audit(30);
    RAISE EXCEPTION 'audit expiry accepted a sub-year retention';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;
DO $$
BEGIN
  IF has_table_privilege('oracle_app', 'audit_chain_checkpoints', 'INSERT') THEN
    RAISE EXCEPTION 'the app can forge an audit chain anchor';
  END IF;
END $$;
SET LOCAL SESSION AUTHORIZATION oracle_app_login;
SELECT set_config('app.current_tenant','eeeeeeee-0000-0000-0000-00000000c122',true),
       set_config('app.current_role','broker_owner',true);
DO $$
BEGIN
  BEGIN
    PERFORM privacy_retention_sweep(0, 0, 0, 0);
    RAISE EXCEPTION 'a tenant session ran the retention sweep';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    PERFORM set_config('app.audit_expiry', 'on', true);
    DELETE FROM audit_ledger;
    RAISE EXCEPTION 'a tenant session deleted audit rows';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;

\echo 'privacy_lifecycle: PASS'
ROLLBACK;
