-- 0097 — a refresh is not a visit
--
-- 0096 attributed the portal_view row to a person. This stops that row being
-- written seven times for one reading.
--
-- resolve_portal_token() inserts a portal_view on every successful resolution,
-- and every page load resolves the token — the session JWT is deliberately not
-- persisted by the dossier page, so a refresh, a reconnect, a phone waking up
-- and React's own double-invoked effect in development each mint a new session.
-- Measured on a real browser session: nine rows in ninety seconds, two of them
-- eighteen milliseconds apart, from one person reading one page once.
--
-- That is worse than capturing nothing. The observed intent score weights
-- repeats, so an uncollapsed refresh loop climbs on its own and reports a
-- homeowner who left a tab open as the most engaged seller in the book. A
-- confidently wrong signal costs more trust than an absent one.
--
-- The Python side already had this cooldown (client_portal.PORTAL_VIEW_COOLDOWN)
-- and it was doing nothing useful, because the path that actually fires on a
-- page load is this function, not that code. The guard belongs where the write
-- happens. The two intervals must stay equal; a test asserts the Python
-- constant matches the fifteen minutes hardcoded below.
--
-- ACCESS BOOKKEEPING IS NOT COLLAPSED. access_count and last_accessed_at still
-- increment on every resolution: those answer "has this link been used, and
-- when", which is a security question, and an agent checking whether a
-- withdrawn link is still being hit needs the true count. Only the behavioural
-- row — the one an intent model reads — is deduplicated.

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
        SELECT h.tenant_id,h.lead_id,l.seller_client_id,h.id,
               CASE WHEN h.link_kind='joint_venture' THEN 'buyer' ELSE 'seller' END,
               'portal_view',
               jsonb_build_object('link_kind',h.link_kind,'asset_scope',h.asset_scope)
          FROM hit h
          -- LEFT JOIN, not JOIN: a lead with no client link must still record
          -- the visit. Losing the row is a worse failure than losing its
          -- attribution, and an inner join would do that silently.
          LEFT JOIN leads l ON l.id = h.lead_id
         WHERE NOT EXISTS (
             SELECT 1 FROM interaction_logs il
              WHERE il.portal_id = h.id
                AND il.interaction_type = 'portal_view'
                AND il.created_at > now() - interval '15 minutes'
         )
    )
    SELECT h.id,h.tenant_id,h.lead_id,h.access_expires_at,h.link_kind,
           h.asset_scope,h.watermark_text,h.issued_to_label
      FROM hit h;
END $$;

-- 0003 revoked PUBLIC from every function here and DROP+CREATE starts from
-- nothing. Without this the portal stops resolving links outright.
REVOKE ALL ON FUNCTION resolve_portal_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_portal_token(text) TO oracle_app;

-- The NOT EXISTS probe runs on every portal open. Without an index it is a scan
-- of interaction_logs filtered by portal_id, and 0008's only index there is on
-- (lead_id, created_at). Partial, because portal_view is the sole type queried
-- this way and the predicate is a literal the planner can match.
CREATE INDEX IF NOT EXISTS idx_interaction_logs_portal_view_recent
    ON interaction_logs (portal_id, created_at DESC)
    WHERE interaction_type = 'portal_view';
