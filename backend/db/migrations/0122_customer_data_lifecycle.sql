-- ---------------------------------------------------------------------------
-- 0122 — the customer-data lifecycle: closure, erasure, legal holds, receipts.
--
-- Until now a brokerage had no state beyond "exists": no way to close it, no
-- record of what was erased, and no way to stop erasure for litigation. The
-- policy that drives all of this is backend/retention_policy.py; the map of
-- what lives where is docs/privacy-data-map.md.
--
-- Design decisions:
--
-- * The tenants row is NEVER deleted. Erasure removes the tenant's data table
--   by table (privacy_lifecycle.py) and leaves the row as a tombstone with its
--   name scrubbed. Deleting it would cascade through ~100 FKs, some RESTRICT,
--   and would take the privacy records and receipts below with it.
-- * privacy_operations / erasure_ledger / legal_holds have no FK to tenants,
--   so they survive whatever happens to the tenant's data. They hold no
--   customer content: counts, hashes, table names, timestamps.
-- * Lifecycle transitions are enforced in the database. A brokerage owner may
--   request closure (active → closing) and withdraw it during the grace period
--   (closing → active). Everything else — suspension, starting erasure,
--   marking erased — is the platform's alone, so a tenant session cannot
--   reverse an operator suspension or resurrect an erased account.
-- ---------------------------------------------------------------------------

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS lifecycle_state text NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS lifecycle_changed_at timestamptz,
    ADD COLUMN IF NOT EXISTS closure_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS closure_requested_by text,
    ADD COLUMN IF NOT EXISTS closure_reason text,
    ADD COLUMN IF NOT EXISTS erase_after timestamptz,
    ADD COLUMN IF NOT EXISTS erased_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_tenants_lifecycle_state') THEN
        ALTER TABLE tenants ADD CONSTRAINT chk_tenants_lifecycle_state
            CHECK (lifecycle_state IN ('active','suspended','closing','erasing','erased'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_tenants_closing_has_deadline') THEN
        ALTER TABLE tenants ADD CONSTRAINT chk_tenants_closing_has_deadline
            CHECK (lifecycle_state <> 'closing' OR (erase_after IS NOT NULL AND closure_requested_at IS NOT NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_tenants_lifecycle_due
    ON tenants (erase_after) WHERE lifecycle_state IN ('closing','erasing');

CREATE OR REPLACE FUNCTION tenants_guard_lifecycle() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lifecycle_state IS NOT DISTINCT FROM OLD.lifecycle_state THEN
        -- Same state: the deadline columns are still platform-owned once set,
        -- except that a tenant may not move its own erasure date.
        IF NOT app_is_platform_admin()
           AND (NEW.erase_after IS DISTINCT FROM OLD.erase_after
                OR NEW.erased_at IS DISTINCT FROM OLD.erased_at) THEN
            RAISE EXCEPTION 'tenant lifecycle dates are platform-controlled'
                USING ERRCODE = '42501';
        END IF;
        IF OLD.lifecycle_state = 'erased' AND NOT app_is_platform_admin() THEN
            RAISE EXCEPTION 'an erased tenant cannot be modified' USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END IF;

    IF app_is_platform_admin() THEN
        IF OLD.lifecycle_state = 'erased' THEN
            RAISE EXCEPTION 'an erased tenant cannot change state' USING ERRCODE = '42501';
        END IF;
    ELSIF OLD.lifecycle_state = 'active' AND NEW.lifecycle_state = 'closing' THEN
        NULL;  -- owner requests closure; the application checks the role
    ELSIF OLD.lifecycle_state = 'closing' AND NEW.lifecycle_state = 'active' THEN
        NULL;  -- owner withdraws closure during the grace period
    ELSE
        RAISE EXCEPTION 'tenant lifecycle transition % -> % requires the platform',
            OLD.lifecycle_state, NEW.lifecycle_state USING ERRCODE = '42501';
    END IF;
    NEW.lifecycle_changed_at := now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_tenants_guard_lifecycle ON tenants;
CREATE TRIGGER trg_tenants_guard_lifecycle BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION tenants_guard_lifecycle();

-- A tenant session must never be able to delete its own tenants row (that
-- cascade is exactly what erasure avoids). Erasure tombstones; nobody deletes.
REVOKE DELETE, TRUNCATE ON tenants FROM oracle_app;

-- True when the tenant may run background work and spend money. Jobs of the
-- privacy queue are exempt at the call site — erasure must run on a closing
-- tenant.
CREATE OR REPLACE FUNCTION app_tenant_operational(p_tenant uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT COALESCE((SELECT lifecycle_state = 'active' FROM tenants WHERE id = p_tenant), true)
$$;
REVOKE ALL ON FUNCTION app_tenant_operational(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_tenant_operational(uuid) TO oracle_app;

-- ── privacy_operations ─────────────────────────────────────────────────────
-- One row per lifecycle request: export, offboarding, closure, erasure, a data
-- subject request, a contact erasure, a provider disconnect. `progress` holds
-- per-phase checkpoints so a crashed erasure resumes where it stopped.
-- `receipt` is the customer-facing record of what was done, with counts and
-- a digest — never the data itself.
CREATE TABLE IF NOT EXISTS privacy_operations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL,
    kind          text NOT NULL CHECK (kind IN (
                      'export','offboard','closure','erasure','dsr_access',
                      'dsr_delete','dsr_correct','contact_erase',
                      'provider_disconnect','mls_purge','orphan_audit')),
    state         text NOT NULL DEFAULT 'requested' CHECK (state IN (
                      'requested','running','succeeded','failed','cancelled',
                      'blocked_legal_hold')),
    subject_kind  text CHECK (subject_kind IN ('tenant','user','contact')),
    -- For a contact: HMAC of the normalized email/phone, never the value.
    subject_ref   text,
    requested_by  text NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    reason        text,
    params        jsonb NOT NULL DEFAULT '{}'::jsonb,
    progress      jsonb NOT NULL DEFAULT '{}'::jsonb,
    result        jsonb NOT NULL DEFAULT '{}'::jsonb,
    receipt       jsonb,
    artifact_key  text,
    artifact_expires_at timestamptz,
    error         text,
    due_at        timestamptz,
    completed_at  timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    policy_version text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_privacy_operations_tenant
    ON privacy_operations (tenant_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_privacy_operations_open
    ON privacy_operations (state, due_at) WHERE state IN ('requested','running');
CREATE UNIQUE INDEX IF NOT EXISTS uq_privacy_operations_one_open_closure
    ON privacy_operations (tenant_id) WHERE kind IN ('closure','erasure') AND state IN ('requested','running');

ALTER TABLE privacy_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_operations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS privacy_operations_tenant_isolation ON privacy_operations;
CREATE POLICY privacy_operations_tenant_isolation ON privacy_operations
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
REVOKE ALL ON privacy_operations FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON privacy_operations FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON privacy_operations TO oracle_app;

-- ── legal_holds ────────────────────────────────────────────────────────────
-- While an unreleased hold covers a tenant (or a subject within it), erasure
-- of that scope stops in state blocked_legal_hold. Placed and released only by
-- the platform (on counsel's instruction); the brokerage can see its holds.
CREATE TABLE IF NOT EXISTS legal_holds (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    scope        text NOT NULL CHECK (scope IN ('tenant','user','contact')),
    subject_ref  text,
    reason       text NOT NULL CHECK (length(reason) BETWEEN 3 AND 2000),
    placed_by    text NOT NULL,
    placed_at    timestamptz NOT NULL DEFAULT now(),
    released_by  text,
    released_at  timestamptz,
    CHECK (scope = 'tenant' OR subject_ref IS NOT NULL),
    CHECK ((released_at IS NULL) = (released_by IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_legal_holds_active
    ON legal_holds (tenant_id) WHERE released_at IS NULL;

ALTER TABLE legal_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE legal_holds FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legal_holds_read ON legal_holds;
CREATE POLICY legal_holds_read ON legal_holds FOR SELECT
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant());
DROP POLICY IF EXISTS legal_holds_platform_write ON legal_holds;
CREATE POLICY legal_holds_platform_write ON legal_holds FOR INSERT
    WITH CHECK (app_is_platform_admin());
DROP POLICY IF EXISTS legal_holds_platform_release ON legal_holds;
CREATE POLICY legal_holds_platform_release ON legal_holds FOR UPDATE
    USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin());
REVOKE ALL ON legal_holds FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON legal_holds FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON legal_holds TO oracle_app;

-- ── erasure_ledger ─────────────────────────────────────────────────────────
-- What erasure removed, per table/object prefix: the evidence behind a
-- receipt and the input to scripts/reapply-erasures.py after a restore from a
-- backup taken before the erasure. Append-only.
CREATE TABLE IF NOT EXISTS erasure_ledger (
    id            bigserial PRIMARY KEY,
    operation_id  uuid NOT NULL,
    tenant_id     uuid NOT NULL,
    phase         text NOT NULL,
    target        text NOT NULL,
    action        text NOT NULL CHECK (action IN ('deleted','anonymized','tombstoned','revoked','retained','skipped')),
    rows_affected bigint NOT NULL DEFAULT 0,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_erasure_ledger_operation ON erasure_ledger (operation_id, id);

ALTER TABLE erasure_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE erasure_ledger FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS erasure_ledger_tenant_isolation ON erasure_ledger;
CREATE POLICY erasure_ledger_tenant_isolation ON erasure_ledger
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
REVOKE ALL ON erasure_ledger FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON erasure_ledger FROM oracle_app;
GRANT SELECT, INSERT ON erasure_ledger TO oracle_app;
GRANT USAGE ON SEQUENCE erasure_ledger_id_seq TO oracle_app;

-- ── suppression_tombstones ─────────────────────────────────────────────────
-- When a contact is erased, their opt-out must outlive them: otherwise the
-- next import of the same number is contactable again. Only an HMAC of the
-- normalized address is kept (keyed per tenant, crypto.derive_tenant_key), so
-- the tombstone can match a future import but cannot be reversed into a
-- phone number or email. Retention: CONSENT_SUPPRESSION in retention_policy.py.
CREATE TABLE IF NOT EXISTS suppression_tombstones (
    tenant_id     uuid NOT NULL,
    contact_hmac  text NOT NULL,
    channel       text NOT NULL,
    reason        text NOT NULL DEFAULT 'erased_opt_out',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, contact_hmac, channel)
);

ALTER TABLE suppression_tombstones ENABLE ROW LEVEL SECURITY;
ALTER TABLE suppression_tombstones FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS suppression_tombstones_tenant_isolation ON suppression_tombstones;
CREATE POLICY suppression_tombstones_tenant_isolation ON suppression_tombstones
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
REVOKE ALL ON suppression_tombstones FROM PUBLIC;
REVOKE UPDATE, TRUNCATE ON suppression_tombstones FROM oracle_app;
GRANT SELECT, INSERT, DELETE ON suppression_tombstones TO oracle_app;

-- ── Things that made a tenant impossible to erase ──────────────────────────
-- (a) Two composite FKs said ON DELETE SET NULL with no column list, so
--     deleting a command tried to NULL tenant_id (NOT NULL) too and failed.
ALTER TABLE mission_actions DROP CONSTRAINT IF EXISTS mission_actions_command_fk;
ALTER TABLE mission_actions ADD CONSTRAINT mission_actions_command_fk
    FOREIGN KEY (tenant_id, command_id) REFERENCES command_executions(tenant_id, id)
    ON DELETE SET NULL (command_id);
ALTER TABLE lead_response_events DROP CONSTRAINT IF EXISTS lead_response_events_command_fk;
ALTER TABLE lead_response_events ADD CONSTRAINT lead_response_events_command_fk
    FOREIGN KEY (tenant_id, command_id) REFERENCES command_executions(tenant_id, id)
    ON DELETE SET NULL (command_id) NOT VALID;

-- (b) Smart-plan revisions are immutable — rightly — but the trigger also
--     refused DELETE unconditionally, so no tenant with a plan could ever be
--     erased. Erasure (platform session + an erasure operation in progress) is
--     the one exception; edits stay forbidden for everyone.
CREATE OR REPLACE FUNCTION prevent_smart_plan_revision_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND app_is_platform_admin()
       AND COALESCE(current_setting('app.erasure_operation', true), '') <> '' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'smart plan revisions are immutable';
END;
$$;

-- (c) Record attachments are visible only to their owning agent, with no
--     platform clause, so a platform export could not include them and a
--     platform erasure could neither see nor delete them. Allowed only to a
--     platform session running a privacy operation for exactly that tenant.
DROP POLICY IF EXISTS ai_record_attachments_erasure ON ai_record_attachments;
DROP POLICY IF EXISTS ai_record_attachments_privacy ON ai_record_attachments;
CREATE POLICY ai_record_attachments_privacy ON ai_record_attachments
    FOR ALL
    USING (app_is_platform_admin()
           AND COALESCE(current_setting('app.privacy_operation', true), '') <> ''
           AND tenant_id = app_current_tenant());

-- ── The erasure primitives ─────────────────────────────────────────────────
-- Python (privacy_lifecycle.py) decides the order; these functions decide
-- whether a delete is allowed at all. Every guard is here, not in Python:
-- platform session, a running erasure operation for exactly this tenant, the
-- tenant in state 'erasing', no unreleased tenant-scope legal hold, never the
-- platform tenant, never a table on the retained list. SECURITY DEFINER
-- because oracle_app deliberately lacks DELETE on many of these tables.

CREATE OR REPLACE FUNCTION privacy_assert_erasure(p_operation uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant uuid;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'erasure requires a platform session' USING ERRCODE = '42501';
    END IF;
    SELECT tenant_id INTO v_tenant FROM privacy_operations
     WHERE id = p_operation AND kind = 'erasure' AND state = 'running';
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'no running erasure operation %', p_operation USING ERRCODE = '42501';
    END IF;
    IF v_tenant = '00000000-0000-0000-0000-000000000000'::uuid THEN
        RAISE EXCEPTION 'the platform tenant is never erased' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = v_tenant AND lifecycle_state = 'erasing') THEN
        RAISE EXCEPTION 'tenant % is not in state erasing', v_tenant USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM legal_holds WHERE tenant_id = v_tenant
                AND scope = 'tenant' AND released_at IS NULL) THEN
        RAISE EXCEPTION 'tenant % is under legal hold', v_tenant USING ERRCODE = 'P0001',
            HINT = 'legal_hold';
    END IF;
    PERFORM set_config('app.current_tenant', v_tenant::text, true);
    PERFORM set_config('app.erasure_operation', p_operation::text, true);
    PERFORM set_config('app.privacy_operation', p_operation::text, true);
    RETURN v_tenant;
END $$;

CREATE OR REPLACE FUNCTION privacy_retained_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY[
        'tenants','subscriptions','billing_usage_events','stripe_webhook_events',
        'audit_ledger','audit_anomaly_alerts','privacy_operations','erasure_ledger',
        'legal_holds','suppression_tombstones','schema_migrations'
    ]::text[]
$$;

-- Delete up to p_limit of the tenant's rows from one table. Returns the number
-- deleted; 0 means the table is done. Batching keeps each transaction short
-- and makes a crashed erasure resumable from wherever it stopped.
CREATE OR REPLACE FUNCTION privacy_erase_tenant_batch(p_operation uuid, p_table text, p_limit integer)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant uuid := privacy_assert_erasure(p_operation);
    v_type   text;
    v_rows   bigint;
BEGIN
    IF p_table = ANY (privacy_retained_tables()) THEN
        RAISE EXCEPTION 'table % is retained, not erased', p_table USING ERRCODE = '42501';
    END IF;
    SELECT format_type(a.atttypid, a.atttypmod) INTO v_type
      FROM pg_attribute a
     WHERE a.attrelid = to_regclass('public.' || quote_ident(p_table))
       AND a.attname = 'tenant_id' AND NOT a.attisdropped;
    IF v_type IS NULL THEN
        RAISE EXCEPTION 'table % has no tenant_id', p_table USING ERRCODE = '42P01';
    END IF;
    EXECUTE format(
        'DELETE FROM public.%I WHERE ctid = ANY (ARRAY(SELECT ctid FROM public.%I WHERE tenant_id = $1::%s LIMIT $2))',
        p_table, p_table, v_type)
      USING v_tenant::text, GREATEST(1, LEAST(p_limit, 50000));
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows > 0 THEN
        INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected)
        VALUES (p_operation, v_tenant, 'rows', p_table, 'deleted', v_rows);
    END IF;
    RETURN v_rows;
END $$;

-- Clear one nullable FK column so a reference cycle (clients <-> agent_contacts,
-- smart_plans <-> revisions, sites <-> revisions) can be deleted in order. Only
-- columns that really are part of a foreign key on that table are accepted.
CREATE OR REPLACE FUNCTION privacy_null_tenant_reference(p_operation uuid, p_table text, p_column text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant uuid := privacy_assert_erasure(p_operation);
    v_rel    regclass := to_regclass('public.' || quote_ident(p_table));
    v_rows   bigint;
BEGIN
    IF p_table = ANY (privacy_retained_tables()) OR p_column = 'tenant_id' THEN
        RAISE EXCEPTION 'refusing to clear %.%', p_table, p_column USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.conrelid = v_rel AND c.contype = 'f' AND a.attname = p_column AND NOT a.attnotnull
    ) THEN
        RAISE EXCEPTION '%.% is not a nullable foreign-key column', p_table, p_column USING ERRCODE = '42501';
    END IF;
    EXECUTE format('UPDATE public.%I SET %I = NULL WHERE tenant_id = $1 AND %I IS NOT NULL',
                   p_table, p_column, p_column) USING v_tenant;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END $$;

-- Count what is left, past every RLS policy that would hide rows from a
-- platform session (agent-scoped tables). Verification must not be fooled by
-- RLS into reporting zero.
CREATE OR REPLACE FUNCTION privacy_count_tenant_rows(p_operation uuid, p_table text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant uuid;
    v_type   text;
    v_rows   bigint;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'requires a platform session' USING ERRCODE = '42501';
    END IF;
    SELECT tenant_id INTO v_tenant FROM privacy_operations WHERE id = p_operation;
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'unknown operation %', p_operation USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.current_tenant', v_tenant::text, true);
    PERFORM set_config('app.privacy_operation', p_operation::text, true);
    SELECT format_type(a.atttypid, a.atttypmod) INTO v_type
      FROM pg_attribute a
     WHERE a.attrelid = to_regclass('public.' || quote_ident(p_table))
       AND a.attname = 'tenant_id' AND NOT a.attisdropped;
    IF v_type IS NULL THEN
        RETURN 0;
    END IF;
    EXECUTE format('SELECT count(*) FROM public.%I WHERE tenant_id = $1::%s', p_table, v_type)
       INTO v_rows USING v_tenant::text;
    RETURN v_rows;
END $$;

-- Retained evidence keeps its shape but stops naming people: replace an
-- identity column with a stable per-tenant pseudonym.
CREATE OR REPLACE FUNCTION privacy_pseudonymize_column(p_operation uuid, p_table text, p_column text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant uuid := privacy_assert_erasure(p_operation);
    v_rows   bigint;
BEGIN
    IF NOT (p_table = 'audit_anomaly_alerts' AND p_column = 'actor_id') THEN
        RAISE EXCEPTION 'no pseudonymization rule for %.%', p_table, p_column USING ERRCODE = '42501';
    END IF;
    EXECUTE format(
        'UPDATE public.%I SET %I = ''erased:'' || left(encode(digest(%I || $1::text, ''sha256''), ''hex''), 16)
          WHERE tenant_id = $1 AND %I IS NOT NULL AND %I NOT LIKE ''erased:%%''',
        p_table, p_column, p_column, p_column, p_column) USING v_tenant;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected)
    VALUES (p_operation, v_tenant, 'pseudonymize', p_table || '.' || p_column, 'anonymized', v_rows);
    RETURN v_rows;
END $$;

REVOKE ALL ON FUNCTION privacy_assert_erasure(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION privacy_erase_tenant_batch(uuid, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION privacy_null_tenant_reference(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION privacy_count_tenant_rows(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION privacy_pseudonymize_column(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION privacy_assert_erasure(uuid) TO oracle_app;
GRANT EXECUTE ON FUNCTION privacy_erase_tenant_batch(uuid, text, integer) TO oracle_app;
GRANT EXECUTE ON FUNCTION privacy_null_tenant_reference(uuid, text, text) TO oracle_app;
GRANT EXECUTE ON FUNCTION privacy_count_tenant_rows(uuid, text) TO oracle_app;
GRANT EXECUTE ON FUNCTION privacy_pseudonymize_column(uuid, text, text) TO oracle_app;
GRANT EXECUTE ON FUNCTION privacy_retained_tables() TO oracle_app;

-- Opens a platform session onto one tenant for a running export or DSR
-- (reads only): sets the tenant and the operation so the attachments policy
-- above admits it. Refuses an operation that is not running or not this kind.
CREATE OR REPLACE FUNCTION privacy_begin_read(p_operation uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant uuid;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'requires a platform session' USING ERRCODE = '42501';
    END IF;
    SELECT tenant_id INTO v_tenant FROM privacy_operations
     WHERE id = p_operation AND state = 'running' AND kind IN ('export','dsr_access');
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'no running export operation %', p_operation USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.current_tenant', v_tenant::text, true);
    PERFORM set_config('app.privacy_operation', p_operation::text, true);
    RETURN v_tenant;
END $$;
REVOKE ALL ON FUNCTION privacy_begin_read(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION privacy_begin_read(uuid) TO oracle_app;

-- ── One person's data (a data-subject deletion request) ────────────────────
-- The brokerage (controller) asked Neoh (processor) to delete one person's
-- records. Python finds the row ids under the brokerage's own RLS context;
-- this function deletes exactly those rows from an allowlisted table, for a
-- running dsr_delete operation of that tenant, unless a legal hold covers
-- the tenant or that subject. Transactions, contracts and other broker
-- records are not on the list: they are retained (state broker-record laws)
-- and reported as such.
CREATE OR REPLACE FUNCTION privacy_erase_subject_rows(p_operation uuid, p_table text, p_ids uuid[])
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant  uuid;
    v_subject text;
    v_rows    bigint;
BEGIN
    SELECT tenant_id, subject_ref INTO v_tenant, v_subject FROM privacy_operations
     WHERE id = p_operation AND kind = 'dsr_delete' AND state = 'running';
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'no running deletion request %', p_operation USING ERRCODE = '42501';
    END IF;
    IF v_tenant IS DISTINCT FROM app_current_tenant() AND NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'operation belongs to another tenant' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM legal_holds WHERE tenant_id = v_tenant AND released_at IS NULL
                AND (scope = 'tenant' OR (scope = 'contact' AND subject_ref = v_subject))) THEN
        RAISE EXCEPTION 'subject is under legal hold' USING ERRCODE = 'P0001', HINT = 'legal_hold';
    END IF;
    IF NOT (p_table = ANY (ARRAY[
        'sms_messages','inbound_voice_calls','outreach_consent','outreach_suppression',
        'outreach_attempt_log','email_outbox','interaction_logs','client_notes',
        'client_tasks','client_activities','client_tags','buyer_profiles',
        'contact_nurture_jobs','contact_property_relationships','contact_intake_sessions',
        'intake_handoff_tasks','smart_plan_step_runs','smart_plan_enrollments',
        'agent_call_intents','lead_intake_events','clients','agent_contacts'])) THEN
        RAISE EXCEPTION 'table % is not erasable per subject', p_table USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.privacy_operation', p_operation::text, true);
    EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1 AND id = ANY ($2)', p_table)
      USING v_tenant, p_ids;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected)
    VALUES (p_operation, v_tenant, 'subject', p_table, 'deleted', v_rows);
    RETURN v_rows;
END $$;
REVOKE ALL ON FUNCTION privacy_erase_subject_rows(uuid, text, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION privacy_erase_subject_rows(uuid, text, uuid[]) TO oracle_app;

-- ── Licensed MLS data when the licence ends ────────────────────────────────
-- Unlock MLS licence §29(b)/§35(b): on termination, licensed data is purged.
-- docs/mls-production-runbook.md §R used to say "never delete listing rows";
-- that is right for disabling a feed, wrong for a terminated licence. This
-- deletes one feed's rows in batches, only for a running mls_purge operation
-- naming that feed, only once no brokerage is still entitled to it. A listing
-- a brokerage's own transaction references is kept (and counted) — that is
-- the brokerage's transaction record, and counsel decides.
CREATE OR REPLACE FUNCTION privacy_purge_mls_feed(p_operation uuid, p_mls_id text, p_limit integer)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_rows bigint;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'requires a platform session' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM privacy_operations WHERE id = p_operation AND kind = 'mls_purge'
                    AND state = 'running' AND params->>'mls_id' = p_mls_id) THEN
        RAISE EXCEPTION 'no running mls_purge operation for %', p_mls_id USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM mls_feed_entitlements WHERE mls_id = p_mls_id) THEN
        RAISE EXCEPTION 'feed % is still granted to a brokerage; revoke entitlements first', p_mls_id
            USING ERRCODE = '42501';
    END IF;
    DELETE FROM oracle_mls_listings
     WHERE ctid = ANY (ARRAY(
        SELECT l.ctid FROM oracle_mls_listings l
         WHERE l.mls_id = p_mls_id
           AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.mls_listing_id = l.id)
         LIMIT GREATEST(1, LEAST(p_limit, 50000))));
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows > 0 THEN
        INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected, detail)
        SELECT p_operation, tenant_id, 'mls_purge', 'oracle_mls_listings', 'deleted', v_rows,
               jsonb_build_object('mls_id', p_mls_id)
          FROM privacy_operations WHERE id = p_operation;
    END IF;
    RETURN v_rows;
END $$;
REVOKE ALL ON FUNCTION privacy_purge_mls_feed(uuid, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION privacy_purge_mls_feed(uuid, text, integer) TO oracle_app;

-- ── RLS coverage for erasure ───────────────────────────────────────────────
-- The erasure functions are SECURITY DEFINER, but FORCE RLS binds the table
-- owner too unless it is a superuser/BYPASSRLS — so a table with no policy
-- that admits a platform DELETE would erase zero rows, silently, and the RLS
-- count would agree. interaction_logs was the only such table (append-only by
-- design). tests/privacy_lifecycle.sql asserts no erasable table lacks one.
DROP POLICY IF EXISTS interaction_logs_privacy_erase ON interaction_logs;
CREATE POLICY interaction_logs_privacy_erase ON interaction_logs FOR DELETE
    USING (app_is_platform_admin()
           AND COALESCE(current_setting('app.privacy_operation', true), '') <> ''
           AND tenant_id = app_current_tenant());

-- ── Retention timers that nothing enforced ─────────────────────────────────
-- A schedule nothing enforces is not a schedule (FTC, Blackbaud 2024). Each
-- sweep below is SECURITY DEFINER, platform-only, bounded, and returns
-- per-category counts for the periodic task's result.

-- Audit expiry keeps the hash chain verifiable: the entry_hash of the last
-- expired row is recorded as the anchor the next row must link to, and
-- audit_ledger.verify_chain() starts from the newest anchor instead of zeros.
CREATE TABLE IF NOT EXISTS audit_chain_checkpoints (
    id               bigserial PRIMARY KEY,
    anchored_at      timestamptz NOT NULL DEFAULT now(),
    last_expired_seq bigint NOT NULL,
    anchor_hash      text NOT NULL,
    rows_expired     bigint NOT NULL,
    cutoff           timestamptz NOT NULL
);
REVOKE ALL ON audit_chain_checkpoints FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON audit_chain_checkpoints FROM oracle_app;
GRANT SELECT ON audit_chain_checkpoints TO oracle_app;

DROP POLICY IF EXISTS audit_ledger_expiry ON audit_ledger;
CREATE POLICY audit_ledger_expiry ON audit_ledger FOR DELETE
    USING (app_is_platform_admin()
           AND COALESCE(current_setting('app.audit_expiry', true), '') = 'on');

CREATE OR REPLACE FUNCTION privacy_expire_audit(p_days integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_cutoff timestamptz;
    v_seq    bigint;
    v_hash   text;
    v_rows   bigint;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'requires a platform session' USING ERRCODE = '42501';
    END IF;
    IF p_days IS NULL OR p_days < 365 THEN
        RAISE EXCEPTION 'audit retention below one year is refused (got %)', p_days USING ERRCODE = '22023';
    END IF;
    v_cutoff := now() - make_interval(days => p_days);
    -- Expire a contiguous prefix of the chain only, and stop at the first row
    -- that is still inside retention: with clock skew between replicas a
    -- newer seq can carry an older timestamp, and "every row older than the
    -- cutoff" would then reach past rows that must be kept.
    SELECT seq, entry_hash INTO v_seq, v_hash FROM audit_ledger
     WHERE seq < COALESCE((SELECT min(seq) FROM audit_ledger WHERE created_at >= v_cutoff),
                          (SELECT max(seq) + 1 FROM audit_ledger))
     ORDER BY seq DESC LIMIT 1;
    IF v_seq IS NULL THEN
        RETURN 0;
    END IF;
    PERFORM set_config('app.audit_expiry', 'on', true);
    DELETE FROM audit_ledger WHERE seq <= v_seq;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    PERFORM set_config('app.audit_expiry', '', true);
    INSERT INTO audit_chain_checkpoints (last_expired_seq, anchor_hash, rows_expired, cutoff)
    VALUES (v_seq, v_hash, v_rows, v_cutoff);
    RETURN v_rows;
END $$;

CREATE OR REPLACE FUNCTION privacy_retention_sweep(
    p_token_days integer, p_job_days integer, p_tombstone_days integer, p_privacy_days integer)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    out jsonb := '{}'::jsonb;
    n   bigint;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'requires a platform session' USING ERRCODE = '42501';
    END IF;
    -- Capability tokens: dead links kept p_token_days after they died.
    IF p_token_days IS NOT NULL THEN
        DELETE FROM client_portals
         WHERE COALESCE(revoked_at, access_expires_at) < now() - make_interval(days => p_token_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('client_portals', n);
        DELETE FROM property_view_upload_links
         WHERE COALESCE(revoked_at, expires_at) < now() - make_interval(days => p_token_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('property_view_upload_links', n);
        DELETE FROM brokerage_invitations
         WHERE COALESCE(consumed_at, revoked_at, expires_at) < now() - make_interval(days => p_token_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('brokerage_invitations', n);
        DELETE FROM password_reset_tokens
         WHERE COALESCE(consumed_at, expires_at) < now() - make_interval(days => p_token_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('password_reset_tokens', n);
    END IF;
    -- Finished jobs: the skeleton stays (other rows point at it), the
    -- content — payloads, results, errors that quote customer data — goes.
    IF p_job_days IS NOT NULL THEN
        UPDATE automation_jobs
           SET payload = '{}'::jsonb, result = '{}'::jsonb, last_error = NULL, status_message = 'expired'
         WHERE state IN ('succeeded','cancelled','dead_letter','partial')
           AND updated_at < now() - make_interval(days => p_job_days)
           AND (payload <> '{}'::jsonb OR result <> '{}'::jsonb OR last_error IS NOT NULL);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('automation_jobs_emptied', n);
        DELETE FROM automation_job_attempts
         WHERE COALESCE(finished_at, started_at) < now() - make_interval(days => p_job_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('automation_job_attempts', n);
    END IF;
    IF p_tombstone_days IS NOT NULL THEN
        DELETE FROM suppression_tombstones WHERE created_at < now() - make_interval(days => p_tombstone_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('suppression_tombstones', n);
    END IF;
    IF p_privacy_days IS NOT NULL THEN
        DELETE FROM erasure_ledger WHERE recorded_at < now() - make_interval(days => p_privacy_days);
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('erasure_ledger', n);
        DELETE FROM privacy_operations
         WHERE completed_at < now() - make_interval(days => p_privacy_days)
           AND state IN ('succeeded','failed','cancelled');
        GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('privacy_operations', n);
    END IF;
    RETURN out;
END $$;

-- Privacy records and tombstones are otherwise undeletable by the app; the
-- sweep runs as their owner, still under FORCE RLS, so give it a policy.
DROP POLICY IF EXISTS privacy_operations_expiry ON privacy_operations;
CREATE POLICY privacy_operations_expiry ON privacy_operations FOR DELETE USING (app_is_platform_admin());
DROP POLICY IF EXISTS erasure_ledger_expiry ON erasure_ledger;
CREATE POLICY erasure_ledger_expiry ON erasure_ledger FOR DELETE USING (app_is_platform_admin());

REVOKE ALL ON FUNCTION privacy_expire_audit(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION privacy_retention_sweep(integer, integer, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION privacy_expire_audit(integer) TO oracle_app;
GRANT EXECUTE ON FUNCTION privacy_retention_sweep(integer, integer, integer, integer) TO oracle_app;
