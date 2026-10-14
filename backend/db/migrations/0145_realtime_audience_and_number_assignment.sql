-- ---------------------------------------------------------------------------
-- 0145 — two small, independent additions (both additive; the previous
-- release runs on this schema unchanged).
--
-- 1. app_record_audience(): who may receive a live frame about a client
--    and/or contact (CONTRACT C3.6, ledger L03-08).
--
--    Since 0131/0134/0140 an agent cannot read a colleague's client, contact,
--    call or text, but the WebSocket fan-out still sent some frames about them
--    to every socket of the brokerage (NEGOTIATION_TELEMETRY with the counter
--    offer and ARV, VOICE_TELEMETRY with the transcript line). ws_hub now asks
--    this function ONCE per frame for the members entitled to it and delivers
--    only to their sockets.
--
--    Returns NULL when every member may see the row (both links empty or in
--    the brokerage pool — exactly the frames that went to everyone before),
--    else the agent ids (users.agent_id) of the active members who may:
--    broker owners, and agents who pass app_linked_access() (0140) for read —
--    the client's assignee or a holder of an active share (view or edit), and
--    the same for the contact, both links required. A link that names no row
--    the caller can read gives an empty list (fail closed).
--
--    It states for EVERY member what app_linked_access(p_client, p_contact,
--    false) states for the caller; test_realtime_record_scope_live.py holds
--    the two equal for every actor (owner, assignee, view/edit share, revoked
--    and expired share, colleague, pool). Call it in the record's tenant
--    context with brokerage record scope (record_access.linked_audience): it
--    is SECURITY INVOKER and reads clients, agent_contacts, record_shares and
--    users under the caller's RLS; members are taken from the record's own
--    tenant, so even a platform-admin caller never lists another brokerage.
--
-- 2. A campaign number assignment goes through the provider side-effect
--    ledger (provider_purchases, 0123) as kind '10dlc_number_assignment'
--    (ledger L10-10, audit A07 follow-up). assign_campaign_number submitted
--    the number to the carrier on every POST, so racing or repeated clicks
--    could each reach Telnyx. Only the kind CHECK widens: no new table or
--    column, so no new grant and no privacy-map entry (provider_purchases is
--    classified; the detail holds the brokerage's own business number and the
--    provider campaign id, as for kind 'phone_number').
--
-- Nothing here needs superuser: no SET clause naming an app.* parameter, the
-- function is owned by the migrating role, and the grant is explicit (0003
-- revokes PUBLIC from every function).
--
-- Rollback (application code first, since it calls / writes these):
--   DROP FUNCTION app_record_audience(uuid, uuid);
--   an operator moves any '10dlc_number_assignment' rows out of the way, then
--   re-creates the 0139 check: kind IN ('phone_number','hosted_messaging_order',
--   '10dlc_brand','10dlc_campaign').
-- ---------------------------------------------------------------------------

-- 1. The audience of a live frame ------------------------------------------
CREATE OR REPLACE FUNCTION app_record_audience(p_client uuid, p_contact uuid)
    RETURNS text[]
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, public AS
$$
WITH c AS (SELECT r.id, r.tenant_id, r.assignee_user_id FROM public.clients r WHERE r.id = p_client),
     k AS (SELECT r.id, r.tenant_id, r.assigned_user_id FROM public.agent_contacts r WHERE r.id = p_contact),
     rec AS (
        SELECT (p_client IS NULL OR EXISTS (SELECT 1 FROM c))
           AND (p_contact IS NULL OR EXISTS (SELECT 1 FROM k)) AS found,
               COALESCE((SELECT c.tenant_id FROM c), (SELECT k.tenant_id FROM k)) AS tenant_id,
               (p_client IS NULL OR (SELECT c.assignee_user_id IS NULL FROM c))
           AND (p_contact IS NULL OR (SELECT k.assigned_user_id IS NULL FROM k)) AS pooled)
SELECT CASE
         WHEN NOT rec.found THEN '{}'::text[]
         WHEN rec.pooled THEN NULL
         ELSE COALESCE((
            SELECT array_agg(u.agent_id ORDER BY u.agent_id)
              FROM public.users u
             WHERE u.tenant_id = rec.tenant_id
               AND u.is_active
               AND (u.role IN ('broker_owner', 'platform_admin')
                    OR ((p_client IS NULL OR EXISTS (
                            SELECT 1 FROM c
                             WHERE c.assignee_user_id IS NULL
                                OR c.assignee_user_id = u.id
                                OR EXISTS (SELECT 1 FROM public.record_shares s
                                            WHERE s.grantee_user_id = u.id
                                              AND s.resource_kind = 'client'
                                              AND s.resource_id = c.id
                                              AND s.revoked_at IS NULL
                                              AND (s.expires_at IS NULL OR s.expires_at > now()))))
                        AND (p_contact IS NULL OR EXISTS (
                            SELECT 1 FROM k
                             WHERE k.assigned_user_id IS NULL
                                OR k.assigned_user_id = u.id
                                OR EXISTS (SELECT 1 FROM public.record_shares s
                                            WHERE s.grantee_user_id = u.id
                                              AND s.resource_kind = 'contact'
                                              AND s.resource_id = k.id
                                              AND s.revoked_at IS NULL
                                              AND (s.expires_at IS NULL OR s.expires_at > now()))))))
         ), '{}'::text[])
       END
  FROM rec
$$;
REVOKE ALL ON FUNCTION app_record_audience(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_record_audience(uuid, uuid) TO oracle_app;
COMMENT ON FUNCTION app_record_audience(uuid, uuid) IS
    '0145 (C3.6): agent ids of the active members who may read a row linked to this client and/or contact (app_linked_access for read, per member); NULL = every member (unlinked or pool); empty = nobody found. For ws_hub fan-out, once per frame.';

-- 2. Campaign number assignment joins the provider ledger -------------------
ALTER TABLE provider_purchases DROP CONSTRAINT IF EXISTS provider_purchases_kind_check;
ALTER TABLE provider_purchases
    ADD CONSTRAINT provider_purchases_kind_check
    CHECK (kind IN ('phone_number','hosted_messaging_order','10dlc_brand','10dlc_campaign',
                    '10dlc_number_assignment'));
