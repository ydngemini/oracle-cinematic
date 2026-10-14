-- ---------------------------------------------------------------------------
-- 0140 — texts, emails, calls and the rows derived from them follow the client
-- or contact they belong to (prompt 03 T03.06/T03.07, owner decision
-- D-2026-10-13-01, ledger L03-05).
--
-- 0131 scoped clients and 0134 contacts per agent: an agent sees their own
-- records, the brokerage pool and what is shared with them. The rows ABOUT
-- those people still had only the tenant wall, so any agent could read a
-- colleague's client's text bodies, call records, AI-call transcript excerpts
-- or contract documents by id (L03-03).
--
-- The rule (RESTRICTIVE policies, ANDed with each table's tenant wall):
--   * read  — the linked client is visible AND the linked contact is visible;
--   * write — the linked client is editable AND the linked contact is editable
--             (a view share lets you read a conversation, not send in it);
--   * a link that is NULL does not restrict, so a row with neither link reads
--     exactly as before (an inbound text from an unknown number, for one);
--   * platform admin, the broker owner and brokerage-scope pipelines
--     (app.record_scope = 'brokerage': inbound SMS/voice matching, webhooks)
--     short-circuit to the tenant wall, as in 0131/0134.
--   "Visible"/"editable" are clients' and agent_contacts' own policies: the
--   EXISTS runs under their RLS, so the decision is never restated here.
--
-- Tables (link columns):
--   sms_messages (client, contact)     email_outbox (client)
--   live_call_sessions (client, contact) + negotiation_events (its child: live
--     transcript excerpts and offers)
--   inbound_voice_calls (client, contact)
--   contract_synthesis_artifacts (client)
--   smart_plan_enrollments (contact) + smart_plan_step_runs (its child)
--   agent_call_intents (contact)
--   lead_response_events (client, contact), outcome_events (client),
--   mission_candidates (client) — ledgers and planning rows written by
--     pipelines or as a side effect of an action the caller already had the
--     right to take. Their WRITE needs the linked record VISIBLE, not editable:
--     a showing outcome or a first-response stage is a fact about a record, not
--     a change to it, and refusing it would only lose the fact.
--   interaction_logs was scoped by 0131 (client); leads stay brokerage-wide.
--
-- Compliance stays brokerage-wide, deliberately. Consent, opt-out (STOP) and
-- the voice frequency cap are decided from outreach_consent /
-- outreach_suppression / suppression_tombstones / outreach_attempt_log
-- (outreach_compliance.ConsentLedger.gather_state), none of which this
-- migration touches, and none of which is read from the tables above
-- (sms_messages.opt_out_event and inbound_voice_calls.opt_out_requested are
-- records, never inputs). A compliance check that saw fewer rows would fail
-- OPEN; the live test proves an agent who cannot see a colleague's client is
-- still blocked by that client's STOP and still counts the colleague's calls.
--
-- Policies are written INLINE (0131: a SQL function with sub-selects in a
-- policy runs per row, 6.2 s vs 10.7 ms). app_linked_access() states the same
-- rule for the API to read back (404 vs 403) and for the two child tables'
-- write policies, where it runs once per written row.
--
-- Also: mission_candidates.client_id was never written (the subject id lives
-- in subject_id); backfilled here for subject_type 'client' so the policy has
-- something to follow, and missions/executor.py now writes it.
--
-- Rollback: DROP POLICY <t>_linked_read/_insert/_update/_delete on each table
-- below; DROP FUNCTION app_linked_access(uuid, uuid, boolean). The
-- mission_candidates.client_id backfill is additive (the column existed, NULL).
-- ---------------------------------------------------------------------------

-- 1. The decision, for the API to read back --------------------------------
CREATE OR REPLACE FUNCTION app_linked_access(p_client uuid, p_contact uuid, p_need_edit boolean)
    RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, public AS
$$ SELECT app_is_platform_admin()
       OR app_current_role() = 'broker_owner'
       OR COALESCE(current_setting('app.record_scope', true), '') = 'brokerage'
       OR ((p_client IS NULL OR EXISTS (
                SELECT 1 FROM public.clients c
                 WHERE c.id = p_client
                   AND (NOT p_need_edit OR app_client_access(c.assignee_user_id, c.id, true))))
           AND (p_contact IS NULL OR EXISTS (
                SELECT 1 FROM public.agent_contacts ac
                 WHERE ac.id = p_contact
                   AND (NOT p_need_edit OR app_contact_access(ac.assigned_user_id, ac.id, true))))) $$;
REVOKE ALL ON FUNCTION app_linked_access(uuid, uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_linked_access(uuid, uuid, boolean) TO oracle_app;
COMMENT ON FUNCTION app_linked_access(uuid, uuid, boolean) IS
    '0140: may the caller see (or, with p_need_edit, change) a row linked to this client and/or contact? A NULL link does not restrict. The policies state the same rule inline.';

-- 2. mission_candidates.client_id, for the policy to follow -----------------
UPDATE mission_candidates mc
   SET client_id = c.id
  FROM clients c
 WHERE mc.client_id IS NULL
   AND mc.subject_type = 'client'
   AND mc.subject_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
   AND c.id = mc.subject_id::uuid
   AND c.tenant_id = mc.tenant_id;

-- 3. Rows linked to a client and/or contact ----------------------------------
DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    spec record;
    client_read text;
    client_edit text;
    contact_read text;
    contact_edit text;
    readable text;
    writable text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            -- table,                       client, contact, write needs edit
            ('sms_messages',                 true,  true,  true),
            ('email_outbox',                 true,  false, true),
            ('live_call_sessions',           true,  true,  true),
            ('inbound_voice_calls',          true,  true,  true),
            ('contract_synthesis_artifacts', true,  false, true),
            ('smart_plan_enrollments',       false, true,  true),
            ('agent_call_intents',           false, true,  true),
            ('lead_response_events',         true,  true,  false),
            ('outcome_events',               true,  false, false),
            ('mission_candidates',           true,  false, false)
        ) AS v(t, has_client, has_contact, write_edits)
    LOOP
        IF to_regclass('public.' || spec.t) IS NULL THEN
            RAISE EXCEPTION '0140: expected table % is missing', spec.t;
        END IF;
        client_read := 'true';
        client_edit := 'true';
        contact_read := 'true';
        contact_edit := 'true';
        IF spec.has_client THEN
            client_read := format(
                '(%1$I.client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = %1$I.client_id))',
                spec.t);
            client_edit := format(
                '(%1$I.client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = %1$I.client_id'
                ' AND app_client_access(c.assignee_user_id, c.id, true)))', spec.t);
        END IF;
        IF spec.has_contact THEN
            contact_read := format(
                '(%1$I.contact_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %1$I.contact_id))',
                spec.t);
            contact_edit := format(
                '(%1$I.contact_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %1$I.contact_id'
                ' AND app_contact_access(ac.assigned_user_id, ac.id, true)))', spec.t);
        END IF;
        readable := format('%s OR (%s AND %s)', wide, client_read, contact_read);
        IF spec.write_edits THEN
            writable := format('%s OR (%s AND %s)', wide, client_edit, contact_edit);
        ELSE
            writable := readable;
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_read', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_insert', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_update', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_delete', spec.t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       spec.t || '_linked_read', spec.t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       spec.t || '_linked_insert', spec.t, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       spec.t || '_linked_update', spec.t, writable, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       spec.t || '_linked_delete', spec.t, writable);
    END LOOP;
END $$;

-- 4. Children inherit their parent row ---------------------------------------
-- Read: the parent must be visible (the EXISTS runs under the parent's RLS
-- from section 3). Write: the parent's links must be editable.
DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    spec record;
    readable text;
    writable text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('negotiation_events',   'call_session_id', 'live_call_sessions',     'p.client_id', 'p.contact_id'),
            ('smart_plan_step_runs', 'enrollment_id',   'smart_plan_enrollments', 'NULL::uuid',  'p.contact_id')
        ) AS v(t, fk, parent, client_expr, contact_expr)
    LOOP
        IF to_regclass('public.' || spec.t) IS NULL THEN
            RAISE EXCEPTION '0140: expected table % is missing', spec.t;
        END IF;
        readable := format('%s OR EXISTS (SELECT 1 FROM public.%I p WHERE p.id = %I.%I)',
                           wide, spec.parent, spec.t, spec.fk);
        writable := format('%s OR EXISTS (SELECT 1 FROM public.%I p WHERE p.id = %I.%I'
                           ' AND app_linked_access(%s, %s, true))',
                           wide, spec.parent, spec.t, spec.fk, spec.client_expr, spec.contact_expr);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_read', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_insert', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_update', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_linked_delete', spec.t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       spec.t || '_linked_read', spec.t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       spec.t || '_linked_insert', spec.t, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       spec.t || '_linked_update', spec.t, writable, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       spec.t || '_linked_delete', spec.t, writable);
    END LOOP;
END $$;
