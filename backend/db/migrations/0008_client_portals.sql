-- ===========================================================================
-- 0008_client_portals.sql
-- Oracle — Secure passwordless client portal links + interaction log
-- (Neoh client-management roadmap, "Single-Use Sovereign Portal").
--
-- SECURITY MODEL (deviations from the naive blueprint, on purpose):
--   * The bearer token is generated in the APPLICATION (secrets.token_urlsafe)
--     and only its SHA-256 hex digest is stored. The database never sees the
--     plaintext — not in a column, and not in pgaudit statement logs (0003
--     session-logs every write statement; a DB-side token factory would leak
--     plaintext tokens into the audit stream).
--   * No client_email column. The lead row already carries encrypted contact
--     PII (0006); the agent delivers the link out-of-band. PII not stored
--     twice is PII that cannot leak twice.
--   * Public link resolution cannot run under tenant RLS (an unauthenticated
--     homeowner has no tenant GUC — the policy would yield zero rows and
--     every click would 404). resolve_portal_token() is SECURITY DEFINER,
--     keyed strictly by token digest, and is the ONLY cross-tenant read path.
--   * interaction_logs is append-only: no UPDATE/DELETE policies exist and
--     both privileges are revoked from oracle_app. History cannot be edited.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Portal links — one row per issued link, hash-only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_portals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id           uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    token_hash        text NOT NULL UNIQUE,          -- sha256 hex of the bearer token
    access_expires_at timestamptz NOT NULL,
    revoked_at        timestamptz,
    last_accessed_at  timestamptz,
    access_count      int NOT NULL DEFAULT 0,
    created_by        text,                          -- issuing agent_id
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_client_portals_updated
    BEFORE UPDATE ON client_portals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_client_portals_tenant ON client_portals(tenant_id);
CREATE INDEX IF NOT EXISTS idx_client_portals_lead   ON client_portals(lead_id);
-- token_hash lookups ride the UNIQUE index.

-- ---------------------------------------------------------------------------
-- 2. Interaction log — unified, append-only client-touch history per lead.
--    Portal views, signed documents, voice notes, SMS — one stream.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interaction_logs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id          uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    portal_id        uuid REFERENCES client_portals(id) ON DELETE SET NULL,
    actor_role       text NOT NULL CHECK (actor_role IN
                         ('agent', 'seller', 'buyer', 'ai_system')),
    interaction_type text NOT NULL CHECK (interaction_type IN
                         ('sms', 'call_transcript', 'voice_note', 'portal_view',
                          'document_signed', 'status_change')),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_interaction_logs_lead
    ON interaction_logs(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_tenant ON interaction_logs(tenant_id);

-- ---------------------------------------------------------------------------
-- 3. RLS — ENABLE + FORCE per house style (0001). Tenant isolation via the
--    app_current_tenant() accessor, NOT a raw current_setting() literal.
-- ---------------------------------------------------------------------------
ALTER TABLE client_portals   ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_portals   FORCE  ROW LEVEL SECURITY;
ALTER TABLE interaction_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE interaction_logs FORCE  ROW LEVEL SECURITY;

CREATE POLICY client_portals_tenant_isolation ON client_portals
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Append-only: SELECT + INSERT policies only. With FORCE RLS and no
-- UPDATE/DELETE policy, those statements match zero rows even for the owner.
CREATE POLICY interaction_logs_read ON interaction_logs
    FOR SELECT
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant());

CREATE POLICY interaction_logs_insert ON interaction_logs
    FOR INSERT
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Belt and braces: strip the privileges too (0001's default privileges grant
-- all four to oracle_app on new tables).
REVOKE UPDATE, DELETE ON interaction_logs FROM oracle_app;

-- ---------------------------------------------------------------------------
-- 4. Public token resolution. SECURITY DEFINER (owner: migration superuser)
--    so the unauthenticated landing request can resolve a digest without a
--    tenant context. Strictly:
--      * lookup by exact digest only — no enumeration surface
--      * expiry + revocation enforced here, not in the caller
--      * access bookkeeping and the portal_view log row happen atomically
--      * pinned search_path (SECURITY DEFINER hygiene)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION resolve_portal_token(p_token_hash text)
RETURNS TABLE (portal_id uuid, tenant_id uuid, lead_id uuid, access_expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    RETURN QUERY
    WITH hit AS (
        UPDATE client_portals cp
           SET access_count     = cp.access_count + 1,
               last_accessed_at = now()
         WHERE cp.token_hash = p_token_hash
           AND cp.revoked_at IS NULL
           AND cp.access_expires_at > now()
        RETURNING cp.id, cp.tenant_id, cp.lead_id, cp.access_expires_at
    ),
    logged AS (
        INSERT INTO interaction_logs (tenant_id, lead_id, portal_id, actor_role, interaction_type, payload)
        SELECT h.tenant_id, h.lead_id, h.id, 'seller', 'portal_view', '{}'::jsonb
          FROM hit h
    )
    SELECT h.id, h.tenant_id, h.lead_id, h.access_expires_at FROM hit h;
END $$;

-- New functions default EXECUTE to PUBLIC — strip, then grant the app role.
REVOKE ALL ON FUNCTION resolve_portal_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_portal_token(text) TO oracle_app;

-- ---------------------------------------------------------------------------
-- 5. REMEDIATION (pre-existing defect found while verifying this migration):
--    0003 ran REVOKE ALL ON ALL FUNCTIONS FROM PUBLIC, and the 0001 RLS
--    accessor functions were never re-granted to oracle_app (only
--    app_has_listing_grant got one, in 0002). Locally this is masked because
--    start.sh connects as the postgres superuser — but for any role that
--    actually inherits oracle_app, EVERY policy evaluation fails with
--    "permission denied for function app_current_tenant" and the app cannot
--    read a single row. Same gap for the 0006 crypto helpers.
-- ---------------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION app_current_tenant()            TO oracle_app;
GRANT EXECUTE ON FUNCTION app_current_role()              TO oracle_app;
GRANT EXECUTE ON FUNCTION app_is_platform_admin()         TO oracle_app;
GRANT EXECUTE ON FUNCTION oracle_encrypt(text, text)      TO oracle_app;
GRANT EXECUTE ON FUNCTION oracle_decrypt(bytea, text)     TO oracle_app;
GRANT EXECUTE ON FUNCTION expire_stale_contracts()        TO oracle_app;  -- 0007 (already granted there; idempotent)
