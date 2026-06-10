-- ===========================================================================
-- 0007_contract_lifecycle.sql
-- Oracle — Contract lifecycle + asset dossier state (client-management
-- roadmap, "Temporal Listing Safeguards"). Adds the assignment-window clock
-- to leads and the dossier state machine that the countdown tracker and
-- failure-to-close worker key off.
--
-- Design note vs. the naive approach: a BEFORE UPDATE trigger that flips
-- status to 'expired' only fires when a row is WRITTEN — a stagnant deal
-- nobody touches would never expire, which is exactly the deal the
-- intervention feature exists for. Instead:
--   * contract_expires_at is materialized by a sync trigger (it cannot be a
--     generated column: timestamptz + interval is STABLE, not IMMUTABLE)
--   * expiry is evaluated against now() at read time via the
--     lead_contract_status view
--   * expire_stale_contracts() lets the background worker bulk-transition
--     overdue rows in one statement (RLS still binds — caller's tenant only)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. Lifecycle columns. Off-market assignments typically run a 60/90-day
--    exclusive window; 90 is the default per the roadmap.
-- ---------------------------------------------------------------------------
ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS contract_execution_date timestamptz,
    ADD COLUMN IF NOT EXISTS contract_duration_days  int NOT NULL DEFAULT 90
        CONSTRAINT chk_contract_duration CHECK (contract_duration_days > 0),
    ADD COLUMN IF NOT EXISTS dossier_status text NOT NULL DEFAULT 'draft'
        CONSTRAINT chk_dossier_status CHECK (dossier_status IN
            ('draft', 'under_contract', 'marketing', 'assigned', 'closed', 'expired', 'dead')),
    ADD COLUMN IF NOT EXISTS contract_expires_at timestamptz;

-- Keep contract_expires_at in lockstep with its two source columns.
CREATE OR REPLACE FUNCTION sync_contract_expires_at()
RETURNS trigger AS $$
BEGIN
    NEW.contract_expires_at :=
        NEW.contract_execution_date + make_interval(days => NEW.contract_duration_days);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_leads_contract_expiry ON leads;
CREATE TRIGGER trg_leads_contract_expiry
    BEFORE INSERT OR UPDATE OF contract_execution_date, contract_duration_days ON leads
    FOR EACH ROW EXECUTE FUNCTION sync_contract_expires_at();

-- The failure-to-close worker scans by expiry; only contracted rows matter.
CREATE INDEX IF NOT EXISTS idx_leads_contract_expiry
    ON leads (contract_expires_at)
    WHERE contract_execution_date IS NOT NULL
      AND dossier_status IN ('under_contract', 'marketing');

-- ---------------------------------------------------------------------------
-- 2. Read-time countdown. security_invoker so the caller's RLS context
--    applies — the view never widens visibility beyond the leads policies.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW lead_contract_status
WITH (security_invoker = true) AS
SELECT
    l.id,
    l.tenant_id,
    l.parcel_id,
    l.dossier_status,
    l.contract_execution_date,
    l.contract_expires_at,
    GREATEST(0, CEIL(EXTRACT(EPOCH FROM (l.contract_expires_at - now())) / 86400))::int
        AS days_remaining,
    (l.contract_expires_at IS NOT NULL AND l.contract_expires_at < now())
        AS is_expired
FROM leads l
WHERE l.contract_execution_date IS NOT NULL;

GRANT SELECT ON lead_contract_status TO oracle_app;

-- ---------------------------------------------------------------------------
-- 3. Worker-side bulk transition. Returns the rows it expired so the caller
--    can emit intervention prompts ("no cash buyer matched — generate
--    marketing assets / reverse-lookup stream") per lead. SECURITY INVOKER
--    (the default): RLS confines the sweep to the caller's tenant.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION expire_stale_contracts()
RETURNS SETOF leads AS $$
    UPDATE leads
       SET dossier_status = 'expired'
     WHERE contract_execution_date IS NOT NULL
       AND contract_expires_at < now()
       AND dossier_status IN ('under_contract', 'marketing')
    RETURNING *;
$$ LANGUAGE sql;

GRANT EXECUTE ON FUNCTION expire_stale_contracts() TO oracle_app;
