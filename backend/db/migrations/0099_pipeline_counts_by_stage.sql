-- 0099 — the pipeline rollup learns about stages.
--
-- `get_team_pipeline` (the AI tool behind "what does my pipeline look like")
-- ran `SELECT dossier_status, count(*) FROM leads WHERE tenant_id=$1 GROUP BY
-- dossier_status`. That is the thing migration 0038 already established must
-- never be done directly: on a tenant holding the harvested corpus it is an
-- 8.6M-row aggregate. Measured before this migration: **15.3 s**, against a
-- purpose-built index that the planner *did* choose — the index was never the
-- problem, the row count was. asyncpg's 30 s command_timeout then surfaced it
-- as a bare TimeoutError, so the chat turn died with no reason attached.
--
-- 0038 solved this shape for `state` with a trigger-maintained rollup. This
-- migration refines that rollup's GRAIN rather than adding a second table:
-- (tenant_id, state) becomes (tenant_id, state, dossier_status). Every
-- existing consumer aggregates with `sum(row_count)` (server.py:787,
-- ai_tools_read.py:1530), and a sum over a finer grain is the same number, so
-- they are correct without being touched.
--
-- What CANNOT live here: "expiring within 14 days" is a function of now(), so
-- no row change fires a trigger when it becomes true. That half stays a live
-- query — and it is already indexed by idx_leads_tenant_contract_window,
-- measured at 0.7 ms. Fast + fast, instead of one 15 s scan.

BEGIN;

ALTER TABLE lead_pipeline_counts
    ADD COLUMN IF NOT EXISTS dossier_status text NOT NULL DEFAULT '';

-- Both source columns are NOT NULL on `leads`, so the widened key needs no
-- coalesce and cannot silently drop rows into a NULL bucket.
ALTER TABLE lead_pipeline_counts DROP CONSTRAINT IF EXISTS lead_pipeline_counts_pkey;
ALTER TABLE lead_pipeline_counts
    ADD CONSTRAINT lead_pipeline_counts_pkey PRIMARY KEY (tenant_id, state, dossier_status);

CREATE OR REPLACE FUNCTION maintain_lead_pipeline_counts()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO lead_pipeline_counts (tenant_id, state, dossier_status, row_count, updated_at)
        VALUES (NEW.tenant_id, NEW.state, NEW.dossier_status, 1, now())
        ON CONFLICT (tenant_id, state, dossier_status) DO UPDATE
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
          AND dossier_status = OLD.dossier_status
          AND row_count > 0;
        DELETE FROM lead_pipeline_counts
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND dossier_status = OLD.dossier_status
          AND row_count = 0;
        RETURN OLD;
    END IF;

    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.state IS DISTINCT FROM OLD.state
       OR NEW.dossier_status IS DISTINCT FROM OLD.dossier_status THEN
        UPDATE lead_pipeline_counts
        SET row_count = row_count - 1,
            updated_at = now()
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND dossier_status = OLD.dossier_status
          AND row_count > 0;
        DELETE FROM lead_pipeline_counts
        WHERE tenant_id = OLD.tenant_id
          AND state = OLD.state
          AND dossier_status = OLD.dossier_status
          AND row_count = 0;

        INSERT INTO lead_pipeline_counts (tenant_id, state, dossier_status, row_count, updated_at)
        VALUES (NEW.tenant_id, NEW.state, NEW.dossier_status, 1, now())
        ON CONFLICT (tenant_id, state, dossier_status) DO UPDATE
        SET row_count = lead_pipeline_counts.row_count + 1,
            updated_at = now();
    END IF;
    RETURN NEW;
END;
$$;

-- Same lock ordering as 0038, and for the same reason: CREATE TRIGGER takes a
-- write-conflicting lock held to COMMIT, so installing it before the rebuild
-- makes concurrent lead writers queue rather than land in the gap between the
-- snapshot and the trigger becoming visible.
DROP TRIGGER IF EXISTS trg_leads_pipeline_counts ON leads;
CREATE TRIGGER trg_leads_pipeline_counts
AFTER INSERT OR DELETE OR UPDATE OF tenant_id, state, dossier_status ON leads
FOR EACH ROW EXECUTE FUNCTION maintain_lead_pipeline_counts();

-- Rebuild at the new grain. The old rows carry dossier_status='' from the
-- column default and are not merely wrong but unmergeable, so they go.
DELETE FROM lead_pipeline_counts;
INSERT INTO lead_pipeline_counts (tenant_id, state, dossier_status, row_count, updated_at)
SELECT tenant_id, state, dossier_status, COUNT(*), now()
FROM leads
GROUP BY tenant_id, state, dossier_status;

ALTER TABLE lead_pipeline_counts ALTER COLUMN dossier_status DROP DEFAULT;

COMMIT;
