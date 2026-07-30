-- ==========================================================================
-- 0038_pipeline_browse_performance.sql
--
-- The property pipeline can contain more than one million source records.
-- Browsing it previously sorted the full leads table and ran COUNT(*) on every
-- request, which is prohibitively slow on the production burstable database.
--
-- Keep exact per-tenant/per-state counts transactionally and add the ranking
-- indexes used by the default and state-filtered browse paths.  Complex search
-- filters continue to use an exact COUNT(*) against leads.
-- ==========================================================================

DROP INDEX IF EXISTS idx_leads_pipeline_rank;
CREATE INDEX idx_leads_pipeline_rank
    ON leads (motivation_score DESC, created_at DESC, id ASC);

DROP INDEX IF EXISTS idx_leads_pipeline_state_rank;
CREATE INDEX idx_leads_pipeline_state_rank
    ON leads (state, motivation_score DESC, created_at DESC, id ASC);

DROP INDEX IF EXISTS idx_leads_pipeline_tenant_rank;
CREATE INDEX idx_leads_pipeline_tenant_rank
    ON leads (tenant_id, motivation_score DESC, created_at DESC, id ASC);

DROP INDEX IF EXISTS idx_leads_pipeline_tenant_state_rank;
CREATE INDEX idx_leads_pipeline_tenant_state_rank
    ON leads (tenant_id, state, motivation_score DESC, created_at DESC, id ASC);

CREATE TABLE IF NOT EXISTS lead_pipeline_counts (
    tenant_id uuid   NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    state     text   NOT NULL,
    row_count bigint NOT NULL CHECK (row_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, state)
);

CREATE OR REPLACE FUNCTION maintain_lead_pipeline_counts()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO lead_pipeline_counts (tenant_id, state, row_count, updated_at)
        VALUES (NEW.tenant_id, NEW.state, 1, now())
        ON CONFLICT (tenant_id, state) DO UPDATE
        SET row_count = lead_pipeline_counts.row_count + 1,
            updated_at = now();
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        UPDATE lead_pipeline_counts
        SET row_count = row_count - 1,
            updated_at = now()
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND row_count > 0;
        DELETE FROM lead_pipeline_counts
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND row_count = 0;
        RETURN OLD;
    END IF;

    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.state IS DISTINCT FROM OLD.state THEN
        UPDATE lead_pipeline_counts
        SET row_count = row_count - 1,
            updated_at = now()
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND row_count > 0;
        DELETE FROM lead_pipeline_counts
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND row_count = 0;

        INSERT INTO lead_pipeline_counts (tenant_id, state, row_count, updated_at)
        VALUES (NEW.tenant_id, NEW.state, 1, now())
        ON CONFLICT (tenant_id, state) DO UPDATE
        SET row_count = lead_pipeline_counts.row_count + 1,
            updated_at = now();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_leads_pipeline_counts ON leads;
CREATE TRIGGER trg_leads_pipeline_counts
AFTER INSERT OR DELETE OR UPDATE OF tenant_id, state ON leads
FOR EACH ROW EXECUTE FUNCTION maintain_lead_pipeline_counts();

-- CREATE TRIGGER takes a write-conflicting table lock that is held until this
-- migration transaction commits. Install it before the seed scan: concurrent
-- lead writers wait, then resume after commit with the trigger visible. This
-- keeps the one-time exact snapshot from missing writes in the seed/trigger
-- gap while avoiding a long explicit ACCESS EXCLUSIVE lock.
INSERT INTO lead_pipeline_counts (tenant_id, state, row_count, updated_at)
SELECT tenant_id, state, COUNT(*), now()
FROM leads
GROUP BY tenant_id, state
ON CONFLICT (tenant_id, state) DO UPDATE
SET row_count = EXCLUDED.row_count,
    updated_at = EXCLUDED.updated_at;

ALTER TABLE lead_pipeline_counts ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_pipeline_counts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lead_pipeline_counts_tenant_isolation ON lead_pipeline_counts;
CREATE POLICY lead_pipeline_counts_tenant_isolation ON lead_pipeline_counts
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON lead_pipeline_counts TO oracle_app;
