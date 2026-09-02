-- 0096 — attribute portal activity to the person, not just the parcel
--
-- resolve_portal_token() has always written a portal_view row, and writes it
-- well: atomically with the access bookkeeping, with actor_role derived from
-- link_kind, keyed to the lead. What it does not write is client_id.
--
-- That was invisible until 0095, because nothing read behavioural rows. Now
-- intent_states does, and it reads them per person — so every portal visit ever
-- recorded sits in the table attached to a parcel and attached to nobody. The
-- signal was being captured and then not counted.
--
-- WHY ATTRIBUTE AT WRITE TIME rather than joining at read time. The read-time
-- version is `WHERE il.client_id = $1 OR l.seller_client_id = $1` across a
-- join — precisely the shape that cannot use either single-column index, and
-- 0086/0089 are this codebase's record of what that costs once the table grows.
-- Stamping the column on insert keeps the read a plain indexed equality.
--
-- The cost is honest and small: a portal opened before its lead was linked to a
-- client stays unattributed. The backfill fixes every such row that exists
-- today, and it is idempotent, so a lead linked later can be swept the same way.
--
-- THIS DEFINITION IS 0027'S, not 0008'S. 0027 widened the return type with
-- link_kind, asset_scope, watermark_text and issued_to_label — all of which
-- open_portal_session() reads — and added the joint_venture actor_role rule.
-- Rebuilding from the 0008 text would have silently reverted all of it; the
-- only reason that did not ship is that Postgres refuses to change a function's
-- return type in place.

DROP FUNCTION IF EXISTS resolve_portal_token(text);
CREATE FUNCTION resolve_portal_token(p_token_hash text)
RETURNS TABLE (
    portal_id uuid,
    tenant_id uuid,
    lead_id uuid,
    access_expires_at timestamptz,
    link_kind text,
    asset_scope jsonb,
    watermark_text text,
    issued_to_label text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    RETURN QUERY
    WITH hit AS (
        UPDATE client_portals cp
           SET access_count = cp.access_count + 1,
               last_accessed_at = now()
         WHERE cp.token_hash = p_token_hash
           AND cp.revoked_at IS NULL
           AND cp.access_expires_at > now()
        RETURNING cp.id, cp.tenant_id, cp.lead_id, cp.access_expires_at,
                  cp.link_kind, cp.asset_scope, cp.watermark_text,
                  cp.issued_to_label
    ),
    logged AS (
        INSERT INTO interaction_logs (
            tenant_id,lead_id,client_id,portal_id,actor_role,interaction_type,payload
        )
        -- LEFT JOIN, not JOIN. A lead with no client link must still record the
        -- visit: losing the row entirely is a worse failure than losing its
        -- attribution, and an inner join here would do exactly that silently.
        SELECT h.tenant_id,h.lead_id,l.seller_client_id,h.id,
               CASE WHEN h.link_kind='joint_venture' THEN 'buyer' ELSE 'seller' END,
               'portal_view',
               jsonb_build_object('link_kind',h.link_kind,'asset_scope',h.asset_scope)
          FROM hit h
          LEFT JOIN leads l ON l.id = h.lead_id
    )
    SELECT h.id,h.tenant_id,h.lead_id,h.access_expires_at,h.link_kind,
           h.asset_scope,h.watermark_text,h.issued_to_label
      FROM hit h;
END $$;

-- 0003 revoked PUBLIC from every function in this schema, and a DROP + CREATE
-- starts from nothing. Without this grant the portal stops resolving links
-- outright. Four migrations exist because this step was forgotten before
-- (0085, 0088, 0091, 0092).
REVOKE ALL ON FUNCTION resolve_portal_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_portal_token(text) TO oracle_app;

-- Backfill, bounded to rows that are lead-anchored, unattributed, and whose
-- lead names a client. Re-running it is a no-op.
UPDATE interaction_logs il
   SET client_id = l.seller_client_id
  FROM leads l
 WHERE il.lead_id = l.id
   AND il.client_id IS NULL
   AND l.seller_client_id IS NOT NULL;
