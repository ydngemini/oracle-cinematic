-- ---------------------------------------------------------------------------
-- 0134 — contacts follow the same rule as clients (0131): an agent sees the
-- contacts assigned to them, the brokerage pool, and what is shared with them
-- (prompt 03 T03.03/T03.05/T03.06, owner decision D-2026-10-13-01, ledger
-- L03-04).
--
-- Until now agent_contacts and its four intake tables had only the tenant
-- wall (0054): every agent read every contact — the encrypted name, email and
-- phone, consent, birthdays, intake transcripts — in the brokerage.
--
-- Shape (the 0131 pattern, applied to agent_contacts):
--   * agent_contacts.assigned_user_id (uuid → users.id) is the canonical
--     assignment. assigned_agent_id (text) stays for old callers; a trigger
--     resolves it to the user, rewrites it to the canonical spelling, and
--     REJECTS a string that is not an active member of the brokerage (23503,
--     the same check 0059 made; its trigger is replaced by this one).
--   * record_shares accepts resource_kind 'contact' (view|edit per share).
--   * RESTRICTIVE policies narrow agent_contacts_tenant_isolation, written
--     INLINE (a policy calling a SQL function with sub-selects runs it per row:
--     0131 measured 6.2 s vs 10.7 ms). app_contact_access() states the same
--     rule for the API to read back; the live test holds policy, function and
--     tenancy.contact_access to the same answers.
--   * contact_property_relationships, contact_intake_sessions,
--     intake_handoff_tasks and contact_nurture_jobs inherit the parent contact:
--     read needs it visible, writes need it editable.
--   * F03.02 (found while building this): an EDIT share let its holder assign
--     a colleague's client to themselves. app_may_reassign() now guards both
--     assignment triggers (clients' 0131 trigger is replaced with the guard
--     added): a share changes the record, it never takes it.
--
-- Who keeps brokerage-wide access: platform admin, the broker owner, and
-- system pipelines with app.record_scope = 'brokerage' (inbound SMS/voice
-- matching and intake, lead intake runs as the owner). A verified user session
-- never carries it (tenancy.apply_rls_context).
--
-- Compliance reads stay brokerage-wide. Consent, opt-out and frequency live in
-- outreach_consent / outreach_suppression / outreach_attempt_log (0015), which
-- this migration does not touch. Two reads of agent_contacts are compliance or
-- brokerage functions rather than "seeing the contact", and get a narrow
-- function that sets app.record_scope = 'brokerage' for its own duration only
-- and returns no PII:
--   * app_client_contact_place(client) — the state and timezone that quiet
--     hours and state rules need before an outreach to a client the caller can
--     already see (ai_tools_gated._outreach_target);
--   * app_open_contact_counts() — open contacts per assignee, for lead-routing
--     capacity (lead_routing_api).
-- Like app.record_scope itself these are bounded by the RLS-1 residual (no
-- SQL injection), not by this migration.
--
-- Rollback: DROP the policies named below, app_contact_access,
-- app_client_contact_place, app_open_contact_counts and the contact trigger;
-- re-run 0131 section 2's clients_sync_assignee_user (then drop
-- app_may_reassign); recreate trg_agent_contacts_validate_assignee from 0059;
-- restore the record_shares CHECK to ('client') after revoking any 'contact' rows;
-- DROP COLUMN agent_contacts.assigned_user_id. No data is rewritten except the
-- canonical spelling of assigned_agent_id (a case change).
-- ---------------------------------------------------------------------------

-- 1. Canonical assignment ----------------------------------------------------
ALTER TABLE agent_contacts ADD COLUMN IF NOT EXISTS assigned_user_id uuid REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_agent_contacts_tenant_assigned_user
    ON agent_contacts (tenant_id, assigned_user_id);
-- The contact list (contacts_api.search_contact_rows) pages by updated_at,id.
-- Without an index in that order every visible row was sorted and ran its
-- legacy-client LATERAL lookup first: measured as oracle_app on a 21,000-
-- contact brokerage, 1,760 ms for any agent before 0134, 280-470 ms for an
-- agent seeing 14,050 after it, 3 ms with this index (EXPLAIN ANALYZE, local
-- dev DB, PostgreSQL 16.14). Not tenant-led: the tenant wall's OR form cannot
-- use a tenant prefix (A10 / 0113).
CREATE INDEX IF NOT EXISTS idx_agent_contacts_list_order
    ON agent_contacts (updated_at DESC, id DESC) WHERE deleted_at IS NULL;
COMMENT ON COLUMN agent_contacts.assigned_user_id IS
    'Canonical assignee (users.id), derived from assigned_agent_id by trigger (0134). NULL = the brokerage pool.';

-- 0059's validation trigger is replaced by the sync trigger below (which makes
-- the same active-member check). Dropped first so the backfill's canonical
-- respelling is not refused for a contact whose assignee has since left.
DROP TRIGGER IF EXISTS trg_agent_contacts_validate_assignee ON agent_contacts;
DROP FUNCTION IF EXISTS validate_agent_contact_assignee();

-- Backfill before the trigger exists, without touching updated_at (the contact
-- list is ordered by it). A legacy value that names nobody stays NULL — the
-- pool, exactly what everyone could see before — and is counted.
ALTER TABLE agent_contacts DISABLE TRIGGER trg_agent_contacts_updated;
UPDATE agent_contacts ac
   SET assigned_user_id = u.id,
       assigned_agent_id = u.agent_id
  FROM users u
 WHERE ac.assigned_user_id IS NULL
   AND ac.assigned_agent_id IS NOT NULL
   AND u.tenant_id = ac.tenant_id
   AND lower(u.agent_id) = lower(btrim(ac.assigned_agent_id));
ALTER TABLE agent_contacts ENABLE TRIGGER trg_agent_contacts_updated;

DO $$
DECLARE n bigint; d bigint;
BEGIN
    SELECT count(*) INTO n FROM agent_contacts
     WHERE assigned_agent_id IS NOT NULL AND btrim(assigned_agent_id) <> '' AND assigned_user_id IS NULL;
    IF n > 0 THEN
        RAISE NOTICE '0134: % contact(s) name an assignee who is not a member; left in the brokerage pool', n;
    END IF;
    -- A contact and its client can name different assignees (a client moved in
    -- the CRM never moved its contact). Counted for the owner, not changed.
    SELECT count(*) INTO d FROM agent_contacts ac JOIN clients c
        ON c.tenant_id = ac.tenant_id AND (c.id = ac.legacy_client_id OR c.contact_id = ac.id)
     WHERE ac.assigned_user_id IS DISTINCT FROM c.assignee_user_id;
    IF d > 0 THEN
        RAISE NOTICE '0134: % contact(s) are assigned differently from their linked client', d;
    END IF;
END $$;

-- Who may change an assignment (F03.02, grant escalation). The owner, a
-- platform admin and a brokerage pipeline move any record; an agent takes one
-- from the pool or gives back their own. Before this, an agent holding an EDIT
-- share on a colleague's client could assign it to themselves (the 0131 WITH
-- CHECK only asks whether the actor can edit the new row, and the new assignee
-- is the actor) and lock the assignee out. A share lets you change the record,
-- never take it. A session with no app identity (migrations, an operator's
-- psql) is not an agent and is not limited here; RLS does not apply to it
-- either.
CREATE OR REPLACE FUNCTION app_may_reassign(p_old uuid, p_new uuid) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, public AS
$$ SELECT p_old IS NULL
       OR p_old IS NOT DISTINCT FROM p_new
       OR NULLIF(current_setting('app.current_tenant', true), '') IS NULL
       OR app_is_platform_admin()
       OR app_current_role() = 'broker_owner'
       OR COALESCE(current_setting('app.record_scope', true), '') = 'brokerage'
       OR p_old = app_current_user_id() $$;
REVOKE ALL ON FUNCTION app_may_reassign(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_may_reassign(uuid, uuid) TO oracle_app;
COMMENT ON FUNCTION app_may_reassign(uuid, uuid) IS
    'F03.02: only the owner, a platform admin or a brokerage pipeline moves a colleague''s record; an agent takes from the pool or returns their own (0134).';

CREATE OR REPLACE FUNCTION agent_contacts_sync_assignee_user() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_id    uuid;
    v_agent text;
BEGIN
    -- assigned_user_id is derived: a write that leaves assigned_agent_id alone keeps it.
    IF TG_OP = 'UPDATE'
       AND NEW.assigned_agent_id IS NOT DISTINCT FROM OLD.assigned_agent_id
       AND NEW.tenant_id = OLD.tenant_id THEN
        NEW.assigned_user_id := OLD.assigned_user_id;
        RETURN NEW;
    END IF;
    IF NEW.assigned_agent_id IS NULL OR btrim(NEW.assigned_agent_id) = '' THEN
        v_id := NULL;
        v_agent := NULL;
    ELSE
        -- Only an ACTIVE member can be given a contact (0059 made the same check).
        SELECT u.id, u.agent_id INTO v_id, v_agent
          FROM public.users u
         WHERE u.tenant_id = NEW.tenant_id
           AND lower(u.agent_id) = lower(btrim(NEW.assigned_agent_id))
           AND u.is_active
         LIMIT 1;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'assignee is not a member of this brokerage (or is not active)'
                  USING ERRCODE = '23503';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND NOT app_may_reassign(OLD.assigned_user_id, v_id) THEN
        RAISE EXCEPTION 'only the broker owner can reassign a colleague''s contact (row-level security)'
              USING ERRCODE = '42501';
    END IF;
    NEW.assigned_agent_id := v_agent;
    NEW.assigned_user_id := v_id;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION agent_contacts_sync_assignee_user() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_agent_contacts_sync_assignee_user ON agent_contacts;
CREATE TRIGGER trg_agent_contacts_sync_assignee_user
    BEFORE INSERT OR UPDATE ON agent_contacts
    FOR EACH ROW EXECUTE FUNCTION agent_contacts_sync_assignee_user();

-- The same guard for clients: 0131's trigger, plus app_may_reassign.
CREATE OR REPLACE FUNCTION clients_sync_assignee_user() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_id    uuid;
    v_agent text;
BEGIN
    -- assignee_user_id is derived: a write that leaves assignee_id alone keeps it.
    IF TG_OP = 'UPDATE'
       AND NEW.assignee_id IS NOT DISTINCT FROM OLD.assignee_id
       AND NEW.tenant_id = OLD.tenant_id THEN
        NEW.assignee_user_id := OLD.assignee_user_id;
        RETURN NEW;
    END IF;
    IF NEW.assignee_id IS NULL OR btrim(NEW.assignee_id) = '' THEN
        v_id := NULL;
        v_agent := NULL;
    ELSE
        SELECT u.id, u.agent_id INTO v_id, v_agent
          FROM public.users u
         WHERE u.tenant_id = NEW.tenant_id
           AND lower(u.agent_id) = lower(btrim(NEW.assignee_id))
         LIMIT 1;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'assignee is not a member of this brokerage'
                  USING ERRCODE = '23503';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND NOT app_may_reassign(OLD.assignee_user_id, v_id) THEN
        RAISE EXCEPTION 'only the broker owner can reassign a colleague''s client (row-level security)'
              USING ERRCODE = '42501';
    END IF;
    NEW.assignee_id := v_agent;
    NEW.assignee_user_id := v_id;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION clients_sync_assignee_user() FROM PUBLIC;


-- 2. Shares cover contacts ---------------------------------------------------
ALTER TABLE record_shares DROP CONSTRAINT IF EXISTS record_shares_resource_kind_check;
ALTER TABLE record_shares ADD CONSTRAINT record_shares_resource_kind_check
    CHECK (resource_kind IN ('client', 'contact'));

-- 3. The one decision, for the API to read back ------------------------------
CREATE OR REPLACE FUNCTION app_contact_access(p_assignee uuid, p_contact uuid, p_need_edit boolean)
    RETURNS boolean
    LANGUAGE sql STABLE AS
$$ SELECT app_is_platform_admin()
       OR app_current_role() = 'broker_owner'
       OR COALESCE(current_setting('app.record_scope', true), '') = 'brokerage'
       OR p_assignee IS NULL
       OR p_assignee = (SELECT app_current_user_id())
       OR EXISTS (
            SELECT 1 FROM public.record_shares s
             WHERE s.grantee_user_id = (SELECT app_current_user_id())
               AND s.resource_kind = 'contact'
               AND s.resource_id = p_contact
               AND s.revoked_at IS NULL
               AND (s.expires_at IS NULL OR s.expires_at > now())
               AND (NOT p_need_edit OR s.permission = 'edit')) $$;
REVOKE ALL ON FUNCTION app_contact_access(uuid, uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_contact_access(uuid, uuid, boolean) TO oracle_app;
COMMENT ON FUNCTION app_contact_access(uuid, uuid, boolean) IS
    'D-2026-10-13-01 for agent_contacts: admin, owner, brokerage-scope pipeline, pool, assignee, or an active contact share (edit when p_need_edit). Mirror: tenancy.contact_access.';

-- 4. agent_contacts: restrictive narrowing of the tenant wall -----------------
DROP POLICY IF EXISTS agent_contacts_agent_read   ON agent_contacts;
DROP POLICY IF EXISTS agent_contacts_agent_insert ON agent_contacts;
DROP POLICY IF EXISTS agent_contacts_agent_update ON agent_contacts;
DROP POLICY IF EXISTS agent_contacts_agent_delete ON agent_contacts;
DO $$
DECLARE
    brokerage_wide text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
       OR assigned_user_id IS NULL
       OR assigned_user_id = (SELECT app_current_user_id())$q$;
    shared text := $q$ OR id IN (SELECT s.resource_id FROM public.record_shares s
                    WHERE s.grantee_user_id = (SELECT app_current_user_id())
                      AND s.resource_kind = 'contact' AND s.revoked_at IS NULL
                      AND (s.expires_at IS NULL OR s.expires_at > now())%s)$q$;
    readable text := brokerage_wide || format(shared, '');
    editable text := brokerage_wide || format(shared, $q$ AND s.permission = 'edit'$q$);
BEGIN
    EXECUTE format('CREATE POLICY agent_contacts_agent_read ON agent_contacts AS RESTRICTIVE FOR SELECT USING (%s)', readable);
    -- An agent creates a contact for the pool or for themselves; only the owner
    -- (or a brokerage pipeline) creates one for a colleague.
    EXECUTE format('CREATE POLICY agent_contacts_agent_insert ON agent_contacts AS RESTRICTIVE FOR INSERT WITH CHECK (%s)', editable);
    -- WITH CHECK is the rule on the NEW row: an agent may take a pool contact
    -- or return their own, but handing one to a colleague is the owner's call.
    EXECUTE format('CREATE POLICY agent_contacts_agent_update ON agent_contacts AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                   editable, editable);
    EXECUTE format('CREATE POLICY agent_contacts_agent_delete ON agent_contacts AS RESTRICTIVE FOR DELETE USING (%s)', editable);
END $$;

-- 5. Intake tables inherit the parent contact ---------------------------------
-- contact_id is NOT NULL on all four. Read: the parent must be visible (the
-- EXISTS runs under agent_contacts' own RLS). Write: it must be editable.
DO $$
DECLARE
    t text;
    readable text;
    editable text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'contact_property_relationships', 'contact_intake_sessions',
        'intake_handoff_tasks', 'contact_nurture_jobs'
    ] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '0134: expected table % is missing', t;
        END IF;
        readable := format(
            'EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %1$I.contact_id)', t);
        editable := format(
            'EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %1$I.contact_id'
            ' AND app_contact_access(ac.assigned_user_id, ac.id, true))', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_contact_read', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_contact_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_contact_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_contact_delete', t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       t || '_contact_read', t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       t || '_contact_insert', t, editable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       t || '_contact_update', t, editable, editable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       t || '_contact_delete', t, editable);
    END LOOP;
END $$;

-- 6. Compliance and brokerage reads that are not "seeing the contact" ---------
-- Each sets app.record_scope = 'brokerage' for its own query and puts the
-- caller's value back before returning, so the caller's scope is unchanged
-- afterwards. (Not a `SET app.record_scope` clause on the function: on managed
-- PostgreSQL the migrating role is not a superuser, and PostgreSQL refuses a
-- function SET clause for an unregistered custom parameter from a
-- non-superuser — 42501, which is what failed this migration on staging.)
-- If the query raises, the transaction or the caller's savepoint rolls the
-- set_config back with it.
CREATE OR REPLACE FUNCTION app_client_contact_place(p_client uuid)
    RETURNS TABLE (state_code char(2), timezone text)
    LANGUAGE plpgsql VOLATILE
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_prev text := COALESCE(current_setting('app.record_scope', true), '');
BEGIN
    PERFORM set_config('app.record_scope', 'brokerage', true);
    RETURN QUERY
        SELECT ac.state_code, ac.timezone
          FROM public.agent_contacts ac
         WHERE ac.deleted_at IS NULL
           AND (ac.legacy_client_id = p_client
                OR ac.id = (SELECT c.contact_id FROM public.clients c WHERE c.id = p_client))
         ORDER BY (ac.state_code IS NOT NULL) DESC, ac.updated_at DESC
         LIMIT 1;
    PERFORM set_config('app.record_scope', v_prev, true);
END
$$;
REVOKE ALL ON FUNCTION app_client_contact_place(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_client_contact_place(uuid) TO oracle_app;
COMMENT ON FUNCTION app_client_contact_place(uuid) IS
    'Quiet-hours/state inputs for outreach to a client the caller already read under its own scope (0134). Returns no PII.';

CREATE OR REPLACE FUNCTION app_open_contact_counts()
    RETURNS TABLE (assigned_user_id uuid, open_contacts integer)
    LANGUAGE plpgsql VOLATILE
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_prev text := COALESCE(current_setting('app.record_scope', true), '');
BEGIN
    PERFORM set_config('app.record_scope', 'brokerage', true);
    RETURN QUERY
        SELECT ac.assigned_user_id, count(*)::int
          FROM public.agent_contacts ac
         WHERE ac.deleted_at IS NULL AND ac.assigned_user_id IS NOT NULL
         GROUP BY ac.assigned_user_id;
    PERFORM set_config('app.record_scope', v_prev, true);
END
$$;
REVOKE ALL ON FUNCTION app_open_contact_counts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_open_contact_counts() TO oracle_app;
COMMENT ON FUNCTION app_open_contact_counts() IS
    'Open contacts per assignee in the caller''s brokerage, for lead-routing capacity (0134). Counts only.';
