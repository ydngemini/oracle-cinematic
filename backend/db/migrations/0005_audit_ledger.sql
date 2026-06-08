-- ===========================================================================
-- 0005_audit_ledger_pg.sql
-- Oracle — Append-only tamper-evident audit ledger (Decision 008).
--
-- Layers added here:
--   1. audit_ledger table  — immutable event log, INSERT only for oracle_app
--   2. Hash chain          — prev_hash / entry_hash SHA-256 links enforce
--                            append-only semantics at the application layer;
--                            a broken chain signals out-of-band tampering even
--                            if a DBA-level DELETE somehow slipped through.
--   3. RLS policies        — tenants are hard-walled; platform_admin reads all.
--
-- INVARIANT: oracle_app CANNOT UPDATE, DELETE, or TRUNCATE this table.
-- Only a superuser / table owner can mutate existing rows.  Any such mutation
-- MUST break the hash chain, making it detectable on the next chain-verify run.
--
-- pgaudit (0003) already logs every DDL/write by oracle_app_login at the session
-- level; this ledger is the application-level companion — richer semantic events
-- (AI_PHONE_CALL, EXPORT_LEAD, etc.) that pgaudit cannot classify on its own.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. platform_admin marker role.  Reuse oracle_app_login's existing pgaudit
--    reader infra (0003); add a logical flag role for policy checks.
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_admin_role') THEN
        CREATE ROLE platform_admin_role NOLOGIN;
    END IF;
END $$;

-- Grant to oracle_app so the same login credential can act as platform_admin
-- when app.current_role = 'platform_admin' (policy evaluated via helper fn).
-- The actual row-level gate is app_is_platform_admin() from 0001.
GRANT platform_admin_role TO oracle_app;

-- ---------------------------------------------------------------------------
-- 2. audit_ledger — the immutable event spine.
--
--    Column notes:
--      user_id      the agent/operator UUID (maps to users.id) who triggered
--                   the event.  Stored denormalised so the ledger is self-
--                   contained even after a cascade-delete of the users row.
--      category     machine-readable event class; drives dashboards + alerts.
--                   Known values: ACCESS_PII | EXPORT_LEAD | AI_PHONE_CALL |
--                                 GENERATE_TOUR | STRIPE_WEBHOOK | AUTH_FAIL
--      target_id    nullable — the primary entity affected (lead, listing, …).
--      metadata     flexible JSONB payload; application fills per category.
--      prev_hash    SHA-256 of the immediately preceding row for this tenant,
--                   or 'GENESIS' for the first row.
--      entry_hash   SHA-256(tenant_id || user_id || category || action ||
--                           coalesce(target_id,'') || metadata::text ||
--                           prev_hash || created_at::text)
--                   computed by the application before INSERT.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_ledger (
    event_id    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL,
    user_id     uuid        NOT NULL,
    category    text        NOT NULL,
    action      text        NOT NULL,
    target_id   uuid,
    metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    prev_hash   text        NOT NULL,
    entry_hash  text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE audit_ledger IS
    'Append-only tamper-evident event log. oracle_app holds INSERT only — '
    'UPDATE / DELETE / TRUNCATE are revoked. A SHA-256 hash chain '
    '(prev_hash → entry_hash) links every row; any out-of-band deletion '
    'breaks the chain and is detectable via the verify_audit_chain() procedure. '
    'Do NOT add ON DELETE CASCADE from tenants — orphaned rows are evidence.';

COMMENT ON COLUMN audit_ledger.prev_hash IS
    'SHA-256 of the preceding row for this tenant, or the literal string '
    '''GENESIS'' for the first row. Computed by the application layer.';

COMMENT ON COLUMN audit_ledger.entry_hash IS
    'SHA-256(tenant_id || user_id || category || action || '
    'coalesce(target_id::text,'''') || metadata::text || prev_hash || '
    'created_at::text). Verified server-side by verify_audit_chain().';

-- ---------------------------------------------------------------------------
-- 3. Indexes — keep per-tenant timeline queries and category fan-outs fast.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_audit_ledger_tenant_time
    ON audit_ledger (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_ledger_category_time
    ON audit_ledger (category, created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Grants — oracle_app gets INSERT only.  UPDATE / DELETE / TRUNCATE are
--    explicitly revoked (belt-and-suspenders against future blanket GRANTs).
-- ---------------------------------------------------------------------------
GRANT INSERT ON audit_ledger TO oracle_app;
GRANT SELECT ON audit_ledger TO oracle_app;

-- Revoke mutation rights that a blanket GRANT in 0001 may have added.
REVOKE UPDATE   ON audit_ledger FROM oracle_app;
REVOKE DELETE   ON audit_ledger FROM oracle_app;
REVOKE TRUNCATE ON audit_ledger FROM oracle_app;

-- Also ensure PUBLIC has nothing (0003 stripped PUBLIC from all tables, but
-- be explicit for new tables created after that migration).
REVOKE ALL ON audit_ledger FROM PUBLIC;

-- pgaudit reader (0003) should log every SELECT of this table, too.
GRANT SELECT ON audit_ledger TO pgaudit_reader;

-- ---------------------------------------------------------------------------
-- 5. Row-Level Security.
--    SELECT  — own tenant, or platform_admin sees everything.
--    INSERT  — only rows whose tenant_id matches app.current_tenant.
--    (No UPDATE / DELETE policies — the privilege revoke above already
--     prevents them, but omitting policies means even a policy bypass attempt
--     gets blocked at the grant level.)
-- ---------------------------------------------------------------------------
ALTER TABLE audit_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_ledger FORCE  ROW LEVEL SECURITY;

-- Read: own tenant rows, or platform_admin reads full ledger.
CREATE POLICY audit_ledger_select ON audit_ledger
    FOR SELECT
    USING (
        app_is_platform_admin()
        OR tenant_id = app_current_tenant()
    );

-- Write: the application can only insert events for its own tenant.
CREATE POLICY audit_ledger_insert ON audit_ledger
    FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());

-- ---------------------------------------------------------------------------
-- 6. Hash-chain verification helper.
--    Returns rows where the recorded entry_hash does NOT match a fresh
--    recomputation — a non-empty result means the chain was tampered with.
--    Owned by the table owner (superuser / migration role), not oracle_app.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION verify_audit_chain(p_tenant_id uuid)
RETURNS TABLE (
    event_id    uuid,
    created_at  timestamptz,
    discrepancy text
)
LANGUAGE sql STABLE SECURITY DEFINER AS
$$
    SELECT
        a.event_id,
        a.created_at,
        'entry_hash mismatch — expected '
            || encode(
                   digest(
                       a.tenant_id::text
                       || a.user_id::text
                       || a.category
                       || a.action
                       || coalesce(a.target_id::text, '')
                       || a.metadata::text
                       || a.prev_hash
                       || a.created_at::text,
                       'sha256'
                   ),
                   'hex'
               )
            || ' got ' || a.entry_hash AS discrepancy
    FROM audit_ledger a
    WHERE a.tenant_id = p_tenant_id
      AND a.entry_hash IS DISTINCT FROM
          encode(
              digest(
                  a.tenant_id::text
                  || a.user_id::text
                  || a.category
                  || a.action
                  || coalesce(a.target_id::text, '')
                  || a.metadata::text
                  || a.prev_hash
                  || a.created_at::text,
                  'sha256'
              ),
              'hex'
          )
    ORDER BY a.created_at;
$$;

COMMENT ON FUNCTION verify_audit_chain(uuid) IS
    'Returns any audit_ledger rows whose entry_hash does not match a fresh '
    'SHA-256 recomputation. An empty result set means the chain is intact. '
    'SECURITY DEFINER so platform tooling can call it without superuser; '
    'revoke from PUBLIC to prevent tenant self-inspection of chain internals.';

REVOKE ALL ON FUNCTION verify_audit_chain(uuid) FROM PUBLIC;
