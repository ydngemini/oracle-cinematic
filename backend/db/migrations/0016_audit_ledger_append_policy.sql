-- ---------------------------------------------------------------------------
-- 0016 — Fix audit_ledger insert policy for the global append-only chain.
--
-- The audit_ledger is a GLOBAL, hash-chained, append-only log. It must capture
-- events for EVERY tenant, plus pre-auth and platform-level events whose
-- tenant_id is NULL (tenant_id/user_id were made nullable in 0014).
--
-- The original 0005 policy:
--     CREATE POLICY audit_ledger_insert ... WITH CHECK (tenant_id = app_current_tenant());
-- broke this in two ways once the application started routing inserts through
-- the non-owner oracle_app_login role (which FORCE ROW LEVEL SECURITY binds):
--
--   1. Every INSERT failed. The app writes on a pooled connection with no
--      app.current_tenant GUC set, so app_current_tenant() is NULL and
--      `tenant_id = NULL` evaluates to NULL (= policy failure). Inserts silently
--      fell back to per-host SQLite and the PG ledger stayed empty.
--   2. NULL-tenant events (LOGIN before auth, platform_admin actions) can never
--      satisfy `tenant_id = app_current_tenant()` at all.
--
-- Relaxing the WRITE check to (true) does NOT weaken tamper-resistance:
--   * Append-only is enforced at the GRANT level in 0005 — oracle_app holds
--     only INSERT + SELECT; UPDATE / DELETE / TRUNCATE are explicitly REVOKEd.
--   * Integrity is enforced cryptographically by the SHA-256 hash chain
--     (verify_chain / verify_audit_chain), not by row-level write scoping.
--
-- READ isolation is unchanged: audit_ledger_select still restricts non-admins
-- to their own tenant (app_is_platform_admin() OR tenant_id = app_current_tenant()).
-- The application reads the global chain head under a platform_admin SET LOCAL
-- context (see audit_ledger.AuditLedger.record), which the SELECT policy permits.
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS audit_ledger_insert ON audit_ledger;

CREATE POLICY audit_ledger_insert ON audit_ledger
    FOR INSERT
    WITH CHECK (true);

COMMENT ON POLICY audit_ledger_insert ON audit_ledger IS
    'Append-only global audit chain. Write scoping is intentionally open; '
    'tamper-resistance comes from the INSERT-only GRANT (no UPDATE/DELETE) and '
    'the SHA-256 hash chain. Read isolation is enforced by audit_ledger_select.';

-- ---------------------------------------------------------------------------
-- Monotonic insertion-order column for the hash chain.
--
-- The chain head (record()) and chain walk (verify_chain()) previously ordered
-- by (created_at, event_id). created_at is an application-supplied timestamp and
-- event_id is a random uuid — NEITHER reflects true insertion order. Under
-- concurrency, a row inserted later can carry an earlier created_at (timestamps
-- are stamped before the row reaches the DB), so "ORDER BY created_at DESC LIMIT
-- 1" can return a stale head and two writers fork the chain off the same
-- predecessor — even while holding the chain's advisory lock. An identity column
-- increments strictly in insertion order, giving record()/verify_chain() a
-- correct, monotonic ordering key. (seq is intentionally NOT part of the hashed
-- block — it is assigned by the DB after the hash is computed.)
--
-- Backfill ordering matters on an EXISTING volume. The pre-0016 chain links were
-- built with the old head selection ORDER BY (created_at, event_id) DESC, so the
-- chain must be *walked* in that same order to verify. A bare
-- `ADD COLUMN seq ... GENERATED ALWAYS AS IDENTITY` backfills existing rows in
-- physical-scan order, which can diverge from (created_at, event_id) order at the
-- exact concurrency boundaries above — making verify_chain (which now walks
-- ORDER BY seq) report a false tamper on an intact chain. So we add seq as a
-- plain column, backfill it in (created_at, event_id) order, THEN attach the
-- identity for all future inserts. Fresh installs (empty table) take the same
-- path harmlessly.
-- ---------------------------------------------------------------------------
ALTER TABLE audit_ledger
    ADD COLUMN IF NOT EXISTS seq bigint;

-- 1. Backfill existing rows in the historical chain order.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM audit_ledger WHERE seq IS NULL) THEN
        WITH ordered AS (
            SELECT event_id,
                   row_number() OVER (ORDER BY created_at, event_id) AS rn
              FROM audit_ledger
        )
        UPDATE audit_ledger a
           SET seq = o.rn
          FROM ordered o
         WHERE a.event_id = o.event_id
           AND a.seq IS NULL;
    END IF;
END $$;

-- 2. Attach the always-generated identity, continuing strictly after the highest
--    backfilled value so new inserts stay monotonic. Idempotent via is_identity.
DO $$
DECLARE next_seq bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'audit_ledger' AND column_name = 'seq'
           AND is_identity = 'YES'
    ) THEN
        SELECT COALESCE(max(seq), 0) + 1 INTO next_seq FROM audit_ledger;
        ALTER TABLE audit_ledger ALTER COLUMN seq SET NOT NULL;
        EXECUTE format(
            'ALTER TABLE audit_ledger '
            'ALTER COLUMN seq ADD GENERATED ALWAYS AS IDENTITY (START WITH %s)',
            next_seq
        );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_audit_ledger_seq ON audit_ledger (seq);
