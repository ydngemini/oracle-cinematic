-- ---------------------------------------------------------------------------
-- 0131 — an agent sees their own clients, the brokerage pool, and what is
-- shared with them (prompt 03 T03.03/T03.05/T03.06, owner decision
-- D-2026-10-13-01).
--
-- Until now clients_tenant_isolation was the only policy: every agent read
-- every client in the brokerage. The owner decided:
--   * a client with no assignee is the BROKERAGE POOL — every agent sees it;
--   * an agent-created client is NOT auto-assigned (it lands in the pool);
--   * a share is per record and grants 'view' or 'edit', chosen per share.
--
-- Shape:
--   * clients.assignee_user_id (uuid → users.id) is the canonical assignment.
--     assignee_id (text) stays for old callers; a trigger resolves it to the
--     user, rewrites it to the canonical spelling, and REJECTS a string that
--     is nobody in the brokerage (23503) — before this an unresolvable string
--     would have silently dropped the client into the pool.
--   * record_shares holds per-user grants (view|edit), with expiry and
--     revocation; rows are revoked, never deleted.
--   * Narrowing is done with RESTRICTIVE policies. Permissive policies OR
--     together (F03.01), so a new permissive policy could never narrow the
--     tenant-wide one; restrictive ones AND with it.
--   * The decision is app_client_access(), which the API reads back (SELECT
--     app_client_access(...) FROM clients) so it never re-implements it. The
--     clients policies state the same rule inline for speed (see 5); the live
--     test holds policy, function and the Python mirror (tenancy.client_access)
--     to the same answers.
--   * Child tables (notes, activities, tasks, tags, buyer profile, AI state,
--     deals, deal parties, showings, interaction log) inherit the parent:
--     read needs the client visible, writes need it editable.
--
-- Who keeps brokerage-wide access: platform admin (0120 login membership),
-- the broker owner, and system pipelines that set app.record_scope =
-- 'brokerage' (client portal, inbound SMS/voice matching, the client-AI
-- steward). That GUC is a session variable like app.current_role and
-- app.current_tenant, so it is bounded by the same RLS-1 residual (no SQL
-- injection), not by this migration. tenancy.apply_rls_context never sets it
-- for a verified user session.
--
-- Not covered yet (ledger L03-04): agent_contacts, leads, and the
-- communications tables (sms_messages, email_outbox, live_call_sessions,
-- inbound_voice_calls) — those feed consent and opt-out checks, which must
-- stay brokerage-wide, and need their own review.
-- ---------------------------------------------------------------------------

-- 1. The caller as a users.id ------------------------------------------------
CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, public AS
$$ SELECT u.id FROM public.users u
    WHERE u.tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
      AND lower(u.agent_id) = lower(NULLIF(current_setting('app.current_agent', true), ''))
    LIMIT 1 $$;
REVOKE ALL ON FUNCTION app_current_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_user_id() TO oracle_app;
COMMENT ON FUNCTION app_current_user_id() IS
    'users.id of app.current_agent in app.current_tenant (case-insensitive, as app_begin_session). NULL for system identities.';

-- 2. Canonical assignment ----------------------------------------------------
ALTER TABLE clients ADD COLUMN IF NOT EXISTS assignee_user_id uuid REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_clients_tenant_assignee_user ON clients (tenant_id, assignee_user_id);

-- Backfill before the trigger exists. A legacy value that names nobody stays
-- NULL (the pool — exactly what everyone could see before) and is counted.
UPDATE clients c
   SET assignee_user_id = u.id
  FROM users u
 WHERE c.assignee_user_id IS NULL
   AND c.assignee_id IS NOT NULL
   AND u.tenant_id = c.tenant_id
   AND lower(u.agent_id) = lower(btrim(c.assignee_id));

DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM clients
     WHERE assignee_id IS NOT NULL AND btrim(assignee_id) <> '' AND assignee_user_id IS NULL;
    IF n > 0 THEN
        RAISE NOTICE '0131: % client(s) name an assignee who is not a member; left in the brokerage pool', n;
    END IF;
END $$;

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
        NEW.assignee_id := NULL;
        NEW.assignee_user_id := NULL;
        RETURN NEW;
    END IF;
    SELECT u.id, u.agent_id INTO v_id, v_agent
      FROM public.users u
     WHERE u.tenant_id = NEW.tenant_id
       AND lower(u.agent_id) = lower(btrim(NEW.assignee_id))
     LIMIT 1;
    IF v_id IS NULL THEN
        RAISE EXCEPTION 'assignee is not a member of this brokerage'
              USING ERRCODE = '23503';
    END IF;
    NEW.assignee_id := v_agent;
    NEW.assignee_user_id := v_id;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION clients_sync_assignee_user() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_clients_sync_assignee_user ON clients;
CREATE TRIGGER trg_clients_sync_assignee_user
    BEFORE INSERT OR UPDATE ON clients
    FOR EACH ROW EXECUTE FUNCTION clients_sync_assignee_user();

-- 3. Per-record shares -------------------------------------------------------
CREATE TABLE IF NOT EXISTS record_shares (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    resource_kind      text NOT NULL CHECK (resource_kind IN ('client')),
    resource_id        uuid NOT NULL,
    grantee_user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission         text NOT NULL CHECK (permission IN ('view', 'edit')),
    granted_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    granted_by         text NOT NULL,
    note               text CHECK (note IS NULL OR length(note) <= 500),
    created_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz,
    revoked_at         timestamptz,
    revoked_by         text,
    CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_record_shares_active
    ON record_shares (tenant_id, resource_kind, resource_id, grantee_user_id)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_record_shares_grantee
    ON record_shares (grantee_user_id, resource_kind, resource_id)
    WHERE revoked_at IS NULL;

ALTER TABLE record_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE record_shares FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS record_shares_tenant_isolation ON record_shares;
CREATE POLICY record_shares_tenant_isolation ON record_shares
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
-- Revoked, never deleted: the share history is the audit trail.
GRANT SELECT, INSERT, UPDATE ON record_shares TO oracle_app;

-- 4. The one decision --------------------------------------------------------
CREATE OR REPLACE FUNCTION app_client_access(p_assignee uuid, p_client uuid, p_need_edit boolean)
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
               AND s.resource_kind = 'client'
               AND s.resource_id = p_client
               AND s.revoked_at IS NULL
               AND (s.expires_at IS NULL OR s.expires_at > now())
               AND (NOT p_need_edit OR s.permission = 'edit')) $$;
REVOKE ALL ON FUNCTION app_client_access(uuid, uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_client_access(uuid, uuid, boolean) TO oracle_app;
COMMENT ON FUNCTION app_client_access(uuid, uuid, boolean) IS
    'D-2026-10-13-01: admin, owner, brokerage-scope pipeline, pool, assignee, or an active share (edit when p_need_edit). Mirror: tenancy.client_access.';

-- 5. Clients: restrictive narrowing of clients_tenant_isolation ---------------
-- The same rule as app_client_access(), written inline: a SQL function with
-- sub-selects is never inlined, so as a policy it ran once PER ROW (6.3 s for
-- an agent listing a 21k-client brokerage). Inline, the caller's id and their
-- share list are InitPlans, computed once per query. The live access-matrix
-- test asserts the policy and the function agree for every actor.
DROP POLICY IF EXISTS clients_agent_read   ON clients;
DROP POLICY IF EXISTS clients_agent_update ON clients;
DROP POLICY IF EXISTS clients_agent_delete ON clients;
DO $$
DECLARE
    brokerage_wide text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
       OR assignee_user_id IS NULL
       OR assignee_user_id = (SELECT app_current_user_id())$q$;
    shared text := $q$ OR id IN (SELECT s.resource_id FROM public.record_shares s
                    WHERE s.grantee_user_id = (SELECT app_current_user_id())
                      AND s.resource_kind = 'client' AND s.revoked_at IS NULL
                      AND (s.expires_at IS NULL OR s.expires_at > now())%s)$q$;
    readable text := brokerage_wide || format(shared, '');
    editable text := brokerage_wide || format(shared, $q$ AND s.permission = 'edit'$q$);
BEGIN
    EXECUTE format('CREATE POLICY clients_agent_read ON clients AS RESTRICTIVE FOR SELECT USING (%s)', readable);
    -- WITH CHECK is the same rule on the NEW row: after the update the actor
    -- must still be able to edit the client. So an agent can take a pool
    -- client or hand their own back to the pool, but only the owner (or a
    -- brokerage pipeline) gives a client to a colleague —
    -- record_access.resolve_assignment says so first, with a 403 and a reason.
    EXECUTE format('CREATE POLICY clients_agent_update ON clients AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                   editable, editable);
    EXECUTE format('CREATE POLICY clients_agent_delete ON clients AS RESTRICTIVE FOR DELETE USING (%s)', editable);
END $$;

-- 6. Child tables inherit the parent client ----------------------------------
-- Read: the parent must be visible (the EXISTS runs under clients' own RLS).
-- Write: the parent must be editable. A row with no client_id is unaffected.
-- Deals also stay visible to the agent who opened them.
DO $$
DECLARE
    t text;
    readable text;
    editable text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'client_notes', 'client_activities', 'client_tasks', 'client_tags',
        'buyer_profiles', 'client_ai_state', 'transactions', 'transaction_parties',
        'showings', 'interaction_logs'
    ] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '0131: expected table % is missing', t;
        END IF;
        readable := format(
            '%1$I.client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = %1$I.client_id)', t);
        editable := format(
            '%1$I.client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = %1$I.client_id'
            ' AND app_client_access(c.assignee_user_id, c.id, true))', t);
        IF t = 'transactions' THEN
            readable := readable || ' OR lower(transactions.created_by) = lower(app_current_agent())';
            editable := editable || ' OR lower(transactions.created_by) = lower(app_current_agent())';
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_client_read', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_client_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_client_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_client_delete', t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       t || '_client_read', t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       t || '_client_insert', t, editable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       t || '_client_update', t, editable, editable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       t || '_client_delete', t, editable);
    END LOOP;
END $$;
