-- ==========================================================================
-- 0052_public_market_research.sql
--
-- Shared, source-attributed aggregate housing metrics. These are market-level
-- research observations, never individual listings and never CRM contacts.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS public_market_metrics (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key         text NOT NULL,
    metric_key         text NOT NULL,
    state_code         text NOT NULL CHECK (state_code ~ '^[A-Z]{2}$'),
    geography_name     text NOT NULL,
    geography_type     text NOT NULL DEFAULT 'state',
    period_end         date NOT NULL,
    value              numeric NOT NULL,
    unit               text NOT NULL,
    source_url         text NOT NULL,
    dataset_sha256     text NOT NULL CHECK (dataset_sha256 ~ '^[a-f0-9]{64}$'),
    dataset_updated_at timestamptz,
    retrieved_at       timestamptz NOT NULL DEFAULT now(),
    metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_public_market_metric
        UNIQUE (source_key, metric_key, state_code, period_end)
);

DROP TRIGGER IF EXISTS trg_public_market_metrics_updated ON public_market_metrics;
CREATE TRIGGER trg_public_market_metrics_updated
BEFORE UPDATE ON public_market_metrics
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_public_market_metrics_state_latest
    ON public_market_metrics (state_code, metric_key, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_public_market_metrics_source_latest
    ON public_market_metrics (source_key, period_end DESC);

ALTER TABLE public_market_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE public_market_metrics FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS public_market_metrics_read ON public_market_metrics;
CREATE POLICY public_market_metrics_read ON public_market_metrics
    FOR SELECT
    USING (app_current_role() IN ('agent','broker_owner','platform_admin'));

DROP POLICY IF EXISTS public_market_metrics_write ON public_market_metrics;
CREATE POLICY public_market_metrics_write ON public_market_metrics
    FOR ALL
    USING (app_is_platform_admin())
    WITH CHECK (app_is_platform_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON public_market_metrics TO oracle_app;
