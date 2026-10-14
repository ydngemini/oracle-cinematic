-- ---------------------------------------------------------------------------
-- 0142 — staged drafts and their approvals follow the client or contact they
-- are about (prompt 03, owner decision D-2026-10-13-01, ledger L03-06).
--
-- command_executions (the EMAIL/SMS/CALL/CALENDAR drafts an agent or the AI
-- stages for approval) and action_approvals (the immutable payload a person
-- approves) only had the tenant wall: GET /api/commands listed every agent's
-- drafts — message bodies, phone numbers, email addresses — to any agent in
-- the brokerage, and an approval row was readable by id (L03-03). The client
-- and contact ids lived only inside the target jsonb, so there was nothing for
-- a policy to follow.
--
-- This migration:
--   1. adds client_id / contact_id link columns (nullable, tenant-bound FKs,
--      ON DELETE SET NULL of the link only) to both tables;
--   2. derives them from the target in a BEFORE INSERT/UPDATE trigger, so the
--      previous release (which inserts without them) and this one both link
--      every row, and no writer can pass a link that disagrees with the target:
--        command_executions.target                 ->> client_id / contact_id
--        action_approvals.draft_payload -> target  ->> client_id / contact_id
--      An id that names no client/contact of the row's brokerage stays NULL.
--      The lookup must see records the CALLER cannot (otherwise a draft about
--      an invisible client would be linked to nothing and so be unrestricted),
--      so it runs with app.record_scope = 'brokerage' for its own duration
--      and restores the caller's value (the 0134 pattern; set_config in a
--      plpgsql body, never a function SET clause — managed PostgreSQL);
--   3. backfills existing rows through that same trigger;
--   4. adds the 0140 restrictive read/insert/update/delete policies, inline:
--      read needs the linked client AND contact visible, write needs them
--      editable (a view share lets you read a draft, not stage, change, approve
--      or send it); a NULL link does not restrict; platform admin, the broker
--      owner and brokerage-scope pipelines short-circuit to the tenant wall.
--
-- Who may decide a draft is unchanged and lives in the API
-- (approval_service._require_decider): the agent who requested it or a broker
-- owner. Seeing a colleague's draft about a POOL client (visible to every
-- agent) does not let you approve, reject or edit it.
--
-- Approvals that are not about a command (contract documents, role overrides,
-- site publishes, model runs, team memberships, tour links on a lead) carry no
-- draft_payload.target and stay unlinked: exactly as before.
--
-- Rollback: DROP POLICY command_executions_linked_* / action_approvals_linked_*;
-- DROP TRIGGER trg_command_executions_link ON command_executions; DROP TRIGGER
-- trg_action_approvals_link ON action_approvals; DROP FUNCTION
-- app_link_draft_target(); DROP the four link indexes, the two list indexes, STATISTICS action_approvals_requester_stats, the FKs
-- and the columns. Nothing else is rewritten (updated_at is preserved).
-- ---------------------------------------------------------------------------

-- 1. Link columns -------------------------------------------------------------
ALTER TABLE command_executions ADD COLUMN IF NOT EXISTS client_id uuid;
ALTER TABLE command_executions ADD COLUMN IF NOT EXISTS contact_id uuid;
ALTER TABLE action_approvals ADD COLUMN IF NOT EXISTS client_id uuid;
ALTER TABLE action_approvals ADD COLUMN IF NOT EXISTS contact_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'command_executions_tenant_client_fk') THEN
        ALTER TABLE command_executions ADD CONSTRAINT command_executions_tenant_client_fk
            FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id) ON DELETE SET NULL (client_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'command_executions_tenant_contact_fk') THEN
        ALTER TABLE command_executions ADD CONSTRAINT command_executions_tenant_contact_fk
            FOREIGN KEY (tenant_id, contact_id) REFERENCES agent_contacts (tenant_id, id) ON DELETE SET NULL (contact_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'action_approvals_tenant_client_fk') THEN
        ALTER TABLE action_approvals ADD CONSTRAINT action_approvals_tenant_client_fk
            FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id) ON DELETE SET NULL (client_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'action_approvals_tenant_contact_fk') THEN
        ALTER TABLE action_approvals ADD CONSTRAINT action_approvals_tenant_contact_fk
            FOREIGN KEY (tenant_id, contact_id) REFERENCES agent_contacts (tenant_id, id) ON DELETE SET NULL (contact_id);
    END IF;
END $$;

-- The FK actions look rows up by link; a client's drafts are found by it.
CREATE INDEX IF NOT EXISTS idx_command_executions_client
    ON command_executions (tenant_id, client_id) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_command_executions_contact
    ON command_executions (tenant_id, contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_action_approvals_client
    ON action_approvals (tenant_id, client_id) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_action_approvals_contact
    ON action_approvals (tenant_id, contact_id) WHERE contact_id IS NOT NULL;
-- GET /api/commands pages newest first (ORDER BY tenant_id, created_at DESC,
-- which is newest first within one brokerage). With no index in that order
-- every row of the brokerage was read and sorted. Measured as oracle_app,
-- agent A in a throwaway 21,000-draft brokerage (7k A / 7k B / 7k pool), local
-- dev DB, PostgreSQL 16.14: 63 ms before 0142 (tenant wall only, top-N sort
-- of all 21,000); with the 0142 policies and a sort, 1.7-2.4 s — the policy
-- sub-plans push the estimated cost past jit_above_cost and JIT compilation
-- takes 1.5-2.3 s of it; with this index and the tenant-led order the LIMIT
-- stops the scan: 22-54 ms (owner 0.7 ms). Tenant-led because the 0141
-- tenant wall is an index condition on tenant_id.
CREATE INDEX IF NOT EXISTS idx_command_executions_tenant_created
    ON command_executions (tenant_id, created_at DESC);
-- The approvals list (approval_service.list_approvals), the same way. An
-- agent's list filters lower(requested_by) = lower($1), which no index can
-- serve under RLS (lower() is not leakproof); statistics on the expression let
-- the planner see that one agent's requests are a fraction of the brokerage's,
-- so it walks this index newest first and stops at the LIMIT. Measured as
-- oracle_app, agent A owning ~5% of 21,000 approvals: 323 ms before 0142
-- (seq scan + sort, tenant wall per row); with the 0142 policies 3.2 s with
-- JIT, 154 ms with JIT off, 103 ms with JIT off and these statistics.
CREATE INDEX IF NOT EXISTS idx_action_approvals_tenant_requested
    ON action_approvals (tenant_id, requested_at DESC);
CREATE STATISTICS IF NOT EXISTS action_approvals_requester_stats
    ON (lower(requested_by)) FROM action_approvals;

COMMENT ON COLUMN command_executions.client_id IS
    'The client this draft is about (0142), derived from target->>client_id by trg_command_executions_link. NULL = not linked to a client.';
COMMENT ON COLUMN command_executions.contact_id IS
    'The contact this draft is about (0142), derived from target->>contact_id by trg_command_executions_link.';
COMMENT ON COLUMN action_approvals.client_id IS
    'The client the approved payload is about (0142), derived from draft_payload->target->>client_id.';
COMMENT ON COLUMN action_approvals.contact_id IS
    'The contact the approved payload is about (0142), derived from draft_payload->target->>contact_id.';

-- 2. Derive the links from the target ----------------------------------------
CREATE OR REPLACE FUNCTION app_link_draft_target()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_target jsonb;
    v_client text;
    v_contact text;
    v_prev text;
    uuid_re constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF TG_TABLE_NAME = 'command_executions' THEN
        v_target := NEW.target;
    ELSE
        v_target := NEW.draft_payload -> 'target';
    END IF;
    IF jsonb_typeof(v_target) = 'object' THEN
        v_client := lower(v_target ->> 'client_id');
        v_contact := lower(v_target ->> 'contact_id');
    END IF;
    NEW.client_id := NULL;
    NEW.contact_id := NULL;
    IF (v_client ~ uuid_re) OR (v_contact ~ uuid_re) THEN
        -- Whether the record EXISTS, not whether the caller may see it: the
        -- policies decide visibility from the link this sets.
        v_prev := COALESCE(current_setting('app.record_scope', true), '');
        PERFORM set_config('app.record_scope', 'brokerage', true);
        IF v_client ~ uuid_re THEN
            SELECT c.id INTO NEW.client_id FROM public.clients c
             WHERE c.id = v_client::uuid AND c.tenant_id = NEW.tenant_id;
        END IF;
        IF v_contact ~ uuid_re THEN
            SELECT ac.id INTO NEW.contact_id FROM public.agent_contacts ac
             WHERE ac.id = v_contact::uuid AND ac.tenant_id = NEW.tenant_id;
        END IF;
        PERFORM set_config('app.record_scope', v_prev, true);
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app_link_draft_target() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_link_draft_target() TO oracle_app;
COMMENT ON FUNCTION app_link_draft_target() IS
    '0142: sets client_id/contact_id from the draft target (command_executions.target, action_approvals.draft_payload->target). Returns no data to the caller.';

DROP TRIGGER IF EXISTS trg_command_executions_link ON command_executions;
CREATE TRIGGER trg_command_executions_link
    BEFORE INSERT OR UPDATE OF target, client_id, contact_id ON command_executions
    FOR EACH ROW EXECUTE FUNCTION app_link_draft_target();
DROP TRIGGER IF EXISTS trg_action_approvals_link ON action_approvals;
CREATE TRIGGER trg_action_approvals_link
    BEFORE INSERT OR UPDATE OF draft_payload, client_id, contact_id ON action_approvals
    FOR EACH ROW EXECUTE FUNCTION app_link_draft_target();

-- 3. Backfill through the trigger, without touching updated_at (reconciliation
-- reads it for executing rows) ------------------------------------------------
ALTER TABLE command_executions DISABLE TRIGGER trg_command_executions_updated;
UPDATE command_executions SET client_id = NULL
 WHERE target ?| ARRAY['client_id', 'contact_id'];
ALTER TABLE command_executions ENABLE TRIGGER trg_command_executions_updated;
UPDATE action_approvals SET client_id = NULL
 WHERE jsonb_typeof(draft_payload -> 'target') = 'object'
   AND (draft_payload -> 'target') ?| ARRAY['client_id', 'contact_id'];

-- 4. Restrictive policies, inline (0140) --------------------------------------
DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    t text;
    readable text;
    writable text;
BEGIN
    FOREACH t IN ARRAY ARRAY['command_executions', 'action_approvals'] LOOP
        readable := format(
            '%1$s OR ((%2$I.client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = %2$I.client_id))'
            ' AND (%2$I.contact_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %2$I.contact_id)))',
            wide, t);
        writable := format(
            '%1$s OR ((%2$I.client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = %2$I.client_id'
            ' AND app_client_access(c.assignee_user_id, c.id, true)))'
            ' AND (%2$I.contact_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %2$I.contact_id'
            ' AND app_contact_access(ac.assigned_user_id, ac.id, true))))',
            wide, t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_linked_read', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_linked_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_linked_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_linked_delete', t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       t || '_linked_read', t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       t || '_linked_insert', t, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       t || '_linked_update', t, writable, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       t || '_linked_delete', t, writable);
    END LOOP;
END $$;
