-- ---------------------------------------------------------------------------
-- 0143 — a lead that is a client's own house, and the rows hanging off it,
-- follow that client; lead-intake events follow their contact (prompt 03,
-- owner decision D-2026-10-13-01, ledger L03-07).
--
-- Leads are mostly harvested public-records prospects: no person in the
-- brokerage owns them, so under D-2026-10-13-01 they are the brokerage pool
-- and stay visible to every agent. `leads` has no assignee and this migration
-- does not add one (that would be a new owner decision). What it closes is the
-- one way a lead row exposes a person: `leads.seller_client_id` (0012), set
-- when a house is linked to a client (crm.link_client_house, create_listing).
-- Until now any agent could read a colleague's private client's house — its
-- address, asking price, dossier — and the interaction log and photos hanging
-- off it, by lead id. lead_intake_events carry an inbound lead's contact_id
-- and sealed payload with only the tenant wall.
--
-- The rule (RESTRICTIVE policies, ANDed with each table's tenant wall; the
-- 0140 pattern):
--   leads                read: seller_client_id is NULL, or that client is
--                        visible; write: NULL, or that client is editable.
--   interaction_logs     (rows with a lead_id) and property_media (rows with a
--                        lead_id) — read: the lead is visible; write: the
--                        lead's client is editable. interaction_logs' client_id
--                        link keeps 0131's policies; both apply.
--   lead_intake_events   read: contact_id is NULL, or that contact is visible;
--                        write: NULL, or editable.
--   A NULL link never restricts, so a harvested prospect, its media and its
--   log rows read exactly as before. Platform admin, the broker owner and
--   brokerage-scope pipelines (app.record_scope = 'brokerage') short-circuit.
--   "Visible"/"editable" are clients' and agent_contacts' own policies (the
--   EXISTS runs under their RLS), so the decision is never restated here.
--   app_linked_access() (0140) is the read-back: (seller_client_id, NULL) for
--   a lead, (NULL, contact_id) for an intake event.
--
-- Not changed, deliberately:
--   * contact_property_relationships (a contact's link to a lead) is already
--     the contact's (0134); the lead it points at is a public record and stays
--     in the pool. Hiding a harvested prospect because one agent linked a
--     contact to it would take it from every colleague.
--   * listings, and media attached only to a listing, stay brokerage-wide: a
--     listing is marketed, and is shared across brokerages (listing_grants).
--   * lead_pipeline_counts is a per-tenant/state rollup with no row identity;
--     it still counts a private client's house.
--
-- Performance (the main risk: 10.5M leads on the local dev DB). Inline, never
-- a function (0131: a function with sub-selects in a policy ran per row,
-- 6.2 s vs 10.7 ms). For an agent the short-circuit arms are InitPlans and
-- `seller_client_id IS NULL` decides every harvested row before the EXISTS is
-- reached; the EXISTS (a clients primary-key probe) runs only for the few rows
-- linked to a client. The tenant wall (0141) stays the index condition.
-- Measured before/after in the L03-07 ledger row.
--
-- Writers: harvest (harvesters/base.persist_leads, normalize_public_leads),
-- disposition_enforcer, reconstruction/video/upload-link workers and lead
-- intake run as platform admin or broker owner, so they short-circuit and are
-- unaffected. ON CONFLICT DO NOTHING (crm.link_client_house) ignores a
-- conflicting row it cannot see; the route answers 409 for it.
--
-- Previous release on this schema: unchanged for every row with a NULL link.
-- A lead linked to a client an agent cannot see answers 404 where it used to
-- answer the row; a write through a view-only share is refused (42501, which
-- server.py maps to 403) where the new code checks first.
--
-- Rollback: DROP POLICY leads_linked_read/_insert/_update/_delete ON leads;
-- the same four on lead_intake_events and property_media; DROP POLICY
-- interaction_logs_lead_read/_insert/_update/_delete ON interaction_logs.
-- No data is changed.
-- ---------------------------------------------------------------------------

-- 1. leads: follow seller_client_id. Written out (not generated) so the rule
-- on the 10.5M-row table reads plainly and run_migrations --reconcile can
-- probe the policies by name.
DROP POLICY IF EXISTS leads_linked_read ON leads;
DROP POLICY IF EXISTS leads_linked_insert ON leads;
DROP POLICY IF EXISTS leads_linked_update ON leads;
DROP POLICY IF EXISTS leads_linked_delete ON leads;
CREATE POLICY leads_linked_read ON leads AS RESTRICTIVE FOR SELECT USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR leads.seller_client_id IS NULL
    OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = leads.seller_client_id));
CREATE POLICY leads_linked_insert ON leads AS RESTRICTIVE FOR INSERT WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR leads.seller_client_id IS NULL
    OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = leads.seller_client_id
                  AND app_client_access(c.assignee_user_id, c.id, true)));
CREATE POLICY leads_linked_update ON leads AS RESTRICTIVE FOR UPDATE USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR leads.seller_client_id IS NULL
    OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = leads.seller_client_id
                  AND app_client_access(c.assignee_user_id, c.id, true)))
  WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR leads.seller_client_id IS NULL
    OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = leads.seller_client_id
                  AND app_client_access(c.assignee_user_id, c.id, true)));
CREATE POLICY leads_linked_delete ON leads AS RESTRICTIVE FOR DELETE USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR leads.seller_client_id IS NULL
    OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = leads.seller_client_id
                  AND app_client_access(c.assignee_user_id, c.id, true)));

-- 2. Rows hanging off a lead, and intake events, generated from one rule each.
DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    -- The client of a lead `l` is editable (or there is none).
    lead_client_edit constant text :=
        '(l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c'
        ' WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true)))';
    spec record;
    readable text;
    writable text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            -- table, policy prefix, read rule, write rule (%1$I = the table)
            ('lead_intake_events', 'lead_intake_events_linked',
             '(%1$I.contact_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %1$I.contact_id))',
             '(%1$I.contact_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_contacts ac WHERE ac.id = %1$I.contact_id'
             ' AND app_contact_access(ac.assigned_user_id, ac.id, true)))'),
            ('interaction_logs', 'interaction_logs_lead',
             '(%1$I.lead_id IS NULL OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = %1$I.lead_id))',
             '(%1$I.lead_id IS NULL OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = %1$I.lead_id AND %2$s))'),
            ('property_media', 'property_media_linked',
             '(%1$I.lead_id IS NULL OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = %1$I.lead_id))',
             '(%1$I.lead_id IS NULL OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = %1$I.lead_id AND %2$s))')
        ) AS v(t, prefix, read_rule, write_rule)
    LOOP
        IF to_regclass('public.' || spec.t) IS NULL THEN
            RAISE EXCEPTION '0143: expected table % is missing', spec.t;
        END IF;
        readable := format('%s OR %s', wide, format(spec.read_rule, spec.t));
        writable := format('%s OR %s', wide, format(spec.write_rule, spec.t, lead_client_edit));
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.prefix || '_read', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.prefix || '_insert', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.prefix || '_update', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.prefix || '_delete', spec.t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       spec.prefix || '_read', spec.t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       spec.prefix || '_insert', spec.t, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       spec.prefix || '_update', spec.t, writable, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       spec.prefix || '_delete', spec.t, writable);
    END LOOP;
END $$;

COMMENT ON POLICY leads_linked_read ON leads IS
    '0143: a lead linked to a client (seller_client_id) is visible only to those who can see that client; an unlinked lead (a harvested prospect) is the brokerage pool.';
COMMENT ON POLICY lead_intake_events_linked_read ON lead_intake_events IS
    '0143: an intake event linked to a contact is visible only to those who can see that contact.';
COMMENT ON POLICY interaction_logs_lead_read ON interaction_logs IS
    '0143: a log row on a lead is visible only when the lead is (leads_linked_read).';
COMMENT ON POLICY property_media_linked_read ON property_media IS
    '0143: media on a lead is visible only when the lead is (leads_linked_read); listing-only media is unchanged.';
