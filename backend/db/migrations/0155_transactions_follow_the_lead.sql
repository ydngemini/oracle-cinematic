-- ---------------------------------------------------------------------------
-- 0155 — a deal with no client follows its lead, and a deal's offers,
-- milestones, parties and closing checklist follow the deal (prompt 03, owner
-- decision D-2026-10-13-01, ledger L03-10).
--
-- What was still open after 0151 (L03-03, L03-09):
--   * transactions: 0131 made a deal follow its client (plus "the agent who
--     opened it"). A deal with NO client — every deal opened from a lead or a
--     listing without a client — had only the tenant wall, so any agent in the
--     brokerage could read a colleague's deal on their private client's house:
--     purchase price, earnest money, deadlines, notes.
--   * transaction_offers, transaction_milestones, compliance_checklist_items:
--     only the tenant wall, for EVERY deal — including a deal on a colleague's
--     private client. The API reads them after the deal itself (portfolio_api,
--     ai_tools_read, routes_compliance), so the routes were guarded; the rows
--     were not (readable by id or by transaction_id from any tenant session).
--   * transaction_parties: 0131 narrowed a party who IS a client to that
--     client; every other party (attorney, title, lender, the other side) and
--     its encrypted contact details were brokerage-wide.
--
-- The rule (RESTRICTIVE policies, ANDed with each table's tenant wall; the
-- 0143 / 0151 pattern):
--   transactions (`transactions_lead_read/_insert/_update/_delete`)
--     read   a deal with a client is decided by 0131 and is not narrowed again
--            here; a deal with no client is visible when its lead is (an
--            EXISTS on leads under the caller's own RLS, so 0143 decides), or
--            to the agent who opened it (0131's arm, kept);
--     write  the lead's client is editable (or the lead has none), or the
--            caller opened the deal;
--     a deal with no client and no lead reads exactly as before (the
--     brokerage pool: nothing to follow).
--   offers, milestones, checklist items, parties (`<t>_deal_read/...`)
--     read   the deal is visible (an EXISTS on transactions under the
--            caller's own RLS: 0131 + this file decide);
--     write  the deal is editable: opened by the caller, or its client is
--            editable (0131), or — no client — its lead's client is editable.
--   Platform admin, the broker owner and brokerage-scope pipelines
--   (app.record_scope = 'brokerage') short-circuit, as everywhere since 0131.
--   transaction_parties keeps 0131's client policies besides.
--
-- Not changed, deliberately (L03-10 row): contract_documents and
-- marketplace_publications keep following their lead (0151) / the listings
-- owner question (L03-07) and are not narrowed by their transaction_id;
-- lead_pipeline_counts is an aggregate.
--
-- Performance: inline, never a function (0131). For an agent the short-circuit
-- arms are InitPlans; on transactions `client_id IS NOT NULL` and
-- `lead_id IS NULL` decide most rows before the EXISTS, which is a leads
-- primary-key probe (then leads_linked_read, 0143). A child row's EXISTS is a
-- transactions primary-key probe. Deals are small per brokerage.
--
-- Previous release on this schema: a deal with a client, or with no lead,
-- reads as before; a colleague's no-client deal on their private client's
-- house answers 404 where it used to answer the row, and so do its offers,
-- milestones, parties and checklist. A write through a view-only share
-- touches 0 rows (portfolio_api reports the version conflict it already
-- reported for a view-only client's deal since 0131). No column, no data
-- change.
--
-- Rollback: DROP POLICY transactions_lead_read/_insert/_update/_delete ON
-- transactions; DROP POLICY <t>_deal_read/_insert/_update/_delete ON <t> for
-- transaction_offers, transaction_milestones, compliance_checklist_items and
-- transaction_parties.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    wide constant text := $q$(SELECT app_is_platform_admin())
       OR (SELECT app_current_role()) = 'broker_owner'
       OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'$q$;
    -- The client of a lead `l` is editable (or there is none). As in 0151.
    lead_client_edit constant text :=
        '(l.seller_client_id IS NULL OR EXISTS (SELECT 1 FROM public.clients c'
        ' WHERE c.id = l.seller_client_id AND app_client_access(c.assignee_user_id, c.id, true)))';
    -- A deal `t` the caller may change: they opened it, or its client is
    -- editable, or (no client) its lead's client is editable.
    deal_edit text;
    readable text;
    writable text;
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['transactions', 'transaction_offers', 'transaction_milestones',
                             'compliance_checklist_items', 'transaction_parties'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '0155: expected table % is missing', t;
        END IF;
    END LOOP;

    -- 1. transactions with no client follow their lead.
    writable := wide
        || ' OR transactions.client_id IS NOT NULL'
        || ' OR transactions.lead_id IS NULL'
        || ' OR lower(transactions.created_by) = lower(app_current_agent())'
        || ' OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = transactions.lead_id AND '
        || lead_client_edit || ')';
    -- (transactions_lead_read is written out below the block.)
    DROP POLICY IF EXISTS transactions_lead_insert ON transactions;
    DROP POLICY IF EXISTS transactions_lead_update ON transactions;
    DROP POLICY IF EXISTS transactions_lead_delete ON transactions;
    EXECUTE format('CREATE POLICY transactions_lead_insert ON transactions AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                   writable);
    EXECUTE format('CREATE POLICY transactions_lead_update ON transactions AS RESTRICTIVE FOR UPDATE'
                   ' USING (%s) WITH CHECK (%s)', writable, writable);
    EXECUTE format('CREATE POLICY transactions_lead_delete ON transactions AS RESTRICTIVE FOR DELETE USING (%s)',
                   writable);

    -- 2. A deal's children follow the deal.
    deal_edit := 'lower(t.created_by) = lower(app_current_agent())'
        || ' OR (t.client_id IS NOT NULL AND EXISTS (SELECT 1 FROM public.clients c'
        || ' WHERE c.id = t.client_id AND app_client_access(c.assignee_user_id, c.id, true)))'
        || ' OR (t.client_id IS NULL AND (t.lead_id IS NULL OR EXISTS (SELECT 1 FROM public.leads l'
        || ' WHERE l.id = t.lead_id AND ' || lead_client_edit || ')))';
    FOREACH t IN ARRAY ARRAY['transaction_offers', 'transaction_milestones',
                             'compliance_checklist_items', 'transaction_parties'] LOOP
        readable := format('%s OR EXISTS (SELECT 1 FROM public.transactions t WHERE t.id = %I.transaction_id)',
                           wide, t);
        writable := format('%s OR EXISTS (SELECT 1 FROM public.transactions t WHERE t.id = %I.transaction_id'
                           ' AND (%s))', wide, t, deal_edit);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_deal_read', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_deal_insert', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_deal_update', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_deal_delete', t);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR SELECT USING (%s)',
                       t || '_deal_read', t, readable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR INSERT WITH CHECK (%s)',
                       t || '_deal_insert', t, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR UPDATE USING (%s) WITH CHECK (%s)',
                       t || '_deal_update', t, writable, writable);
        EXECUTE format('CREATE POLICY %I ON %I AS RESTRICTIVE FOR DELETE USING (%s)',
                       t || '_deal_delete', t, writable);
    END LOOP;
END $$;

-- 3. The read rule on transactions, written out so it reads plainly and
-- run_migrations --reconcile can probe this file by name (the 0151 pattern).
-- The same text as section 1's `readable`.
DROP POLICY IF EXISTS transactions_lead_read ON transactions;
CREATE POLICY transactions_lead_read ON transactions AS RESTRICTIVE FOR SELECT USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR transactions.client_id IS NOT NULL
    OR transactions.lead_id IS NULL
    OR lower(transactions.created_by) = lower(app_current_agent())
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = transactions.lead_id));

COMMENT ON POLICY transactions_lead_read ON transactions IS
    '0155: a deal with no client is visible when its lead is (0143), or to the agent who opened it; a deal with a client follows the client (0131).';
COMMENT ON POLICY transaction_offers_deal_read ON transaction_offers IS
    '0155: an offer is visible only when its deal is.';
COMMENT ON POLICY transaction_milestones_deal_read ON transaction_milestones IS
    '0155: a milestone is visible only when its deal is.';
COMMENT ON POLICY compliance_checklist_items_deal_read ON compliance_checklist_items IS
    '0155: a closing-checklist item is visible only when its deal is.';
COMMENT ON POLICY transaction_parties_deal_read ON transaction_parties IS
    '0155: a party is visible only when its deal is (and, if the party is a client, that client: 0131).';
