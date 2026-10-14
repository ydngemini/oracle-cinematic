-- ---------------------------------------------------------------------------
-- 0151 — the rest of a lead's rows follow the lead (prompt 03, owner decision
-- D-2026-10-13-01, ledger L03-09).
--
-- 0143 made a lead that is a client's own house (leads.seller_client_id)
-- follow that client, with its photos (property_media), its log rows
-- (interaction_logs) and lead-intake events. Every other table that hangs off
-- a lead by lead_id still had only the tenant wall, so any agent in the
-- brokerage could read, by id or by listing, a colleague's private client's
-- client-portal links, contract documents (and their encrypted body), floor
-- plans and their revisions, 360 scenes, upload links, 3D-build jobs and
-- capture sessions, tour variants, voice walkthroughs and video jobs — and
-- live_call_sessions rows whose only link is the lead.
--
-- The rule (RESTRICTIVE policies `<table>_lead_read/_insert/_update/_delete`,
-- ANDed with each table's tenant wall; the 0143 pattern):
--   read   the lead is visible — an EXISTS on leads that runs under the
--          caller's own RLS, so 0143 (and through it 0131) decides;
--   write  the lead's client is editable (or the lead has none);
--   a NULL lead never restricts (a listing-only floor plan, scene, upload
--   link, build or video reads exactly as before: listings stay
--   brokerage-wide, owner question in L03-07).
--   Platform admin, the broker owner and brokerage-scope pipelines
--   (app.record_scope = 'brokerage') short-circuit, as everywhere since 0131.
--
-- Tables and the lead they follow:
--   client_portals, contract_documents, property_floorplans,
--   property_pano_scenes, property_view_upload_links, reconstruction_jobs,
--   spatial_tour_variants, capture_sessions, voice_walkthrough_jobs — lead_id;
--   video_studio_jobs — source->>'lead_id' (the job has no lead column; the
--     API writes the id there, a non-uuid value is treated as no lead);
--   property_floorplan_revisions — its floor plan (floorplan_id, NOT NULL), so
--     a revision is visible exactly when the head plan is;
--   live_call_sessions — ONLY rows with no client and no contact link: a call
--     about a client or contact keeps following that record (0140) and is not
--     narrowed further by the house it mentions. (email_outbox needs nothing:
--     its client_id is NOT NULL, so every row already follows its client.)
--   negotiation_events (a call's live excerpts and offers, 0140) already reads
--     through its call, so it follows; its WRITE policies are re-created here
--     so a lead-only call's events need the lead's client editable too.
--
-- Not changed, deliberately (each in the L03-09 ledger row):
--   listings, marketplace_publications (marketed, shared across brokerages:
--   the listings owner question in L03-07); showings (always carry a client,
--   0131); transactions rows with no client (and their offers, parties and
--   milestones) — a separate slice; lead_response_events (a metrics ledger
--   that already follows its client/contact, 0140); outreach_consent
--   (compliance stays brokerage-wide).
--
-- Unauthenticated capability URLs are unaffected: the client portal
-- (resolve_portal_token is SECURITY DEFINER; the portal's media/dossier/
-- activity reads run as a brokerage-scope pipeline), the public upload link
-- (platform admin) and the signed 3D-asset stream (a brokerage-scope pipeline
-- since this release; it was an agent context with no user, which 0143 had
-- already left unable to stream a client's house). Workers: reconstruction and
-- voice walkthroughs run as their submitter in their current role (a build
-- whose submitter has since lost edit access fails instead of writing; C3.4);
-- the claim, recovery and orphan sweeps, the tour-variant and contract-vault
-- jobs and the command worker run as platform admin; the video worker now runs
-- as a brokerage-scope pipeline (it was an agent context with no user).
--
-- Performance: inline, never a function (0131: a function with sub-selects in
-- a policy ran per row). For an agent, the short-circuit arms are InitPlans and
-- `lead_id IS NULL` decides listing rows before the EXISTS; the EXISTS is a
-- leads primary-key probe, which runs leads_linked_read (0143) — a NULL
-- seller_client_id decides it, else one clients primary-key probe. These
-- tables are small per brokerage. Measured before/after in the L03-09 row.
--
-- Previous release on this schema: unchanged for rows whose lead has no client
-- (every harvested prospect) and for every listing-only row. A row on a
-- colleague's private client's house answers 404 where it used to answer the
-- row; a write through a view-only share is refused (42501, which server.py
-- maps to 403) where the new code checks first. No column, no data change.
--
-- Rollback: DROP POLICY <t>_lead_read/_insert/_update/_delete ON <t> for each
-- table above, and re-run 0140's section 4 to restore negotiation_events'
-- write policies.
-- ---------------------------------------------------------------------------

-- 1. client_portals: written out, so the rule on the table behind the public
-- portal reads plainly and run_migrations --reconcile can probe it by name.
DROP POLICY IF EXISTS client_portals_lead_read ON client_portals;
DROP POLICY IF EXISTS client_portals_lead_insert ON client_portals;
DROP POLICY IF EXISTS client_portals_lead_update ON client_portals;
DROP POLICY IF EXISTS client_portals_lead_delete ON client_portals;
CREATE POLICY client_portals_lead_read ON client_portals AS RESTRICTIVE FOR SELECT USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR client_portals.lead_id IS NULL
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = client_portals.lead_id));
CREATE POLICY client_portals_lead_insert ON client_portals AS RESTRICTIVE FOR INSERT WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR client_portals.lead_id IS NULL
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = client_portals.lead_id
                  AND (l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c
                         WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true)))));
CREATE POLICY client_portals_lead_update ON client_portals AS RESTRICTIVE FOR UPDATE USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR client_portals.lead_id IS NULL
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = client_portals.lead_id
                  AND (l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c
                         WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true)))))
  WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR client_portals.lead_id IS NULL
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = client_portals.lead_id
                  AND (l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c
                         WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true)))));
CREATE POLICY client_portals_lead_delete ON client_portals AS RESTRICTIVE FOR DELETE USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR client_portals.lead_id IS NULL
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = client_portals.lead_id
                  AND (l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c
                         WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true)))));

-- 2. Every other lead child, generated from one rule each.
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
    lead_expr text;
    readable text;
    writable text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            -- table, the lead the row follows (%1$I = the table), the links
            -- that, when set, mean the row follows something else instead
            ('contract_documents',         '%1$I.lead_id', NULL),
            ('property_floorplans',        '%1$I.lead_id', NULL),
            ('property_pano_scenes',       '%1$I.lead_id', NULL),
            ('property_view_upload_links', '%1$I.lead_id', NULL),
            ('reconstruction_jobs',        '%1$I.lead_id', NULL),
            ('spatial_tour_variants',      '%1$I.lead_id', NULL),
            ('capture_sessions',           '%1$I.lead_id', NULL),
            ('voice_walkthrough_jobs',     '%1$I.lead_id', NULL),
            ('video_studio_jobs',
             '(CASE WHEN %1$I.source ->> ''lead_id'' ~* ''^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'''
             ' THEN (%1$I.source ->> ''lead_id'')::uuid END)', NULL),
            ('live_call_sessions',         '%1$I.lead_id', '%1$I.client_id IS NOT NULL OR %1$I.contact_id IS NOT NULL')
        ) AS v(t, lead_rule, linked_elsewhere)
    LOOP
        IF to_regclass('public.' || spec.t) IS NULL THEN
            RAISE EXCEPTION '0151: expected table % is missing', spec.t;
        END IF;
        lead_expr := format(spec.lead_rule, spec.t);
        readable := format('%s OR %s IS NULL OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = %s)',
                           wide, lead_expr, lead_expr);
        writable := format('%s OR %s IS NULL OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = %s AND %s)',
                           wide, lead_expr, lead_expr, lead_client_edit);
        IF spec.linked_elsewhere IS NOT NULL THEN
            readable := format('%s OR %s', format(spec.linked_elsewhere, spec.t), readable);
            writable := format('%s OR %s', format(spec.linked_elsewhere, spec.t), writable);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_lead_read', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_lead_insert', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_lead_update', spec.t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', spec.t || '_lead_delete', spec.t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       spec.t || '_lead_read', spec.t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       spec.t || '_lead_insert', spec.t, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       spec.t || '_lead_update', spec.t, writable, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       spec.t || '_lead_delete', spec.t, writable);
    END LOOP;
END $$;

-- 3. A floor plan's revisions follow the plan (and through it, the lead).
DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    readable text;
    writable text;
BEGIN
    readable := wide || ' OR EXISTS (SELECT 1 FROM public.property_floorplans f'
                        ' WHERE f.id = property_floorplan_revisions.floorplan_id)';
    writable := wide || ' OR EXISTS (SELECT 1 FROM public.property_floorplans f'
                        ' WHERE f.id = property_floorplan_revisions.floorplan_id AND (f.lead_id IS NULL'
                        ' OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = f.lead_id'
                        ' AND (l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c'
                        ' WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true))))))';
    DROP POLICY IF EXISTS property_floorplan_revisions_lead_read ON property_floorplan_revisions;
    DROP POLICY IF EXISTS property_floorplan_revisions_lead_insert ON property_floorplan_revisions;
    DROP POLICY IF EXISTS property_floorplan_revisions_lead_update ON property_floorplan_revisions;
    DROP POLICY IF EXISTS property_floorplan_revisions_lead_delete ON property_floorplan_revisions;
    EXECUTE format('CREATE POLICY property_floorplan_revisions_lead_read ON property_floorplan_revisions'
                   ' AS RESTRICTIVE FOR SELECT USING (%s)', readable);
    EXECUTE format('CREATE POLICY property_floorplan_revisions_lead_insert ON property_floorplan_revisions'
                   ' AS RESTRICTIVE FOR INSERT WITH CHECK (%s)', writable);
    EXECUTE format('CREATE POLICY property_floorplan_revisions_lead_update ON property_floorplan_revisions'
                   ' AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)', writable, writable);
    EXECUTE format('CREATE POLICY property_floorplan_revisions_lead_delete ON property_floorplan_revisions'
                   ' AS RESTRICTIVE FOR DELETE USING (%s)', writable);
END $$;

-- 4. negotiation_events (0140 section 4): a lead-only call's events need the
-- lead's client editable, like the call itself. Same names, same read policy;
-- only the client the write check is asked about changes, from the call's
-- client to "the call's client, else (no contact either) its lead's client".
DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    writable text;
BEGIN
    writable := wide || ' OR EXISTS (SELECT 1 FROM public.live_call_sessions p'
                        ' WHERE p.id = negotiation_events.call_session_id'
                        ' AND app_linked_access(COALESCE(p.client_id, CASE WHEN p.contact_id IS NULL THEN'
                        ' (SELECT l.seller_client_id FROM public.leads l WHERE l.id = p.lead_id) END),'
                        ' p.contact_id, true))';
    DROP POLICY IF EXISTS negotiation_events_linked_insert ON negotiation_events;
    DROP POLICY IF EXISTS negotiation_events_linked_update ON negotiation_events;
    DROP POLICY IF EXISTS negotiation_events_linked_delete ON negotiation_events;
    EXECUTE format('CREATE POLICY negotiation_events_linked_insert ON negotiation_events'
                   ' AS RESTRICTIVE FOR INSERT WITH CHECK (%s)', writable);
    EXECUTE format('CREATE POLICY negotiation_events_linked_update ON negotiation_events'
                   ' AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)', writable, writable);
    EXECUTE format('CREATE POLICY negotiation_events_linked_delete ON negotiation_events'
                   ' AS RESTRICTIVE FOR DELETE USING (%s)', writable);
END $$;

COMMENT ON POLICY client_portals_lead_read ON client_portals IS
    '0151: a portal link on a lead is visible only when the lead is (leads_linked_read, 0143). The public portal reads as a brokerage-scope pipeline.';
COMMENT ON POLICY contract_documents_lead_read ON contract_documents IS
    '0151: a contract document on a lead is visible only when the lead is (0143).';
COMMENT ON POLICY property_floorplans_lead_read ON property_floorplans IS
    '0151: a floor plan on a lead is visible only when the lead is (0143); listing-only plans are unchanged.';
COMMENT ON POLICY property_floorplan_revisions_lead_read ON property_floorplan_revisions IS
    '0151: a floor-plan revision is visible only when its floor plan is.';
COMMENT ON POLICY reconstruction_jobs_lead_read ON reconstruction_jobs IS
    '0151: a 3D build on a lead is visible only when the lead is (0143); listing-only builds are unchanged.';
COMMENT ON POLICY video_studio_jobs_lead_read ON video_studio_jobs IS
    '0151: a video job whose source names a lead is visible only when the lead is (0143).';
COMMENT ON POLICY live_call_sessions_lead_read ON live_call_sessions IS
    '0151: a call linked only to a lead (no client, no contact) is visible only when the lead is; otherwise it follows its client/contact (0140).';
