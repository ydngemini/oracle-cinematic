-- 0027_real_estate_intelligence_platform.sql
-- Tenant-isolated autonomous real-estate intelligence substrate.
--
-- Additive and idempotent.  This migration intentionally stores evidence,
-- approvals, and model/version metadata beside every operational artifact so a
-- result can be reproduced and a high-risk action cannot be executed merely by
-- changing an API payload.

BEGIN;

-- Upgrade the existing integration cache for canonical request hashes,
-- stale-while-revalidate, and measurable cache savings.
ALTER TABLE di_cache
    ADD COLUMN IF NOT EXISTS source_name text,
    ADD COLUMN IF NOT EXISTS request_hash char(64),
    ADD COLUMN IF NOT EXISTS stale_until timestamptz,
    ADD COLUMN IF NOT EXISTS hit_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_hit_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_di_cache_source_freshness
    ON di_cache(source_name, expires_at, stale_until);

-- -------------------------------------------------------------------------
-- Licensed/public source registry and immutable source observations.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_licenses (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_key            text NOT NULL,
    source_name           text NOT NULL,
    source_url            text,
    license_name          text NOT NULL DEFAULT 'public-record',
    license_url           text,
    property_level_allowed boolean NOT NULL DEFAULT true,
    outreach_use_allowed  boolean NOT NULL DEFAULT false,
    retention_days        integer NOT NULL DEFAULT 730 CHECK (retention_days BETWEEN 1 AND 3650),
    terms_version         text,
    terms_effective_at    date,
    reviewed_by           text,
    reviewed_at           timestamptz,
    active                boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_key)
);

CREATE TABLE IF NOT EXISTS source_records (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_license_id   uuid NOT NULL REFERENCES source_licenses(id),
    source_key          text NOT NULL,
    record_key          text NOT NULL,
    property_key        text,
    jurisdiction        text,
    observed_at         timestamptz NOT NULL,
    retrieved_at        timestamptz NOT NULL DEFAULT now(),
    effective_version   text,
    request_hash        char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    payload_hash        char(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    raw_payload         jsonb NOT NULL,
    expires_at          timestamptz,
    purged_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_key, record_key, observed_at)
);
ALTER TABLE source_records ADD COLUMN IF NOT EXISTS purged_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_source_records_property
    ON source_records(tenant_id, property_key, observed_at DESC)
    WHERE property_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_source_records_retention
    ON source_records(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS property_signals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    property_key        text NOT NULL,
    signal_type         text NOT NULL,
    signal_value        jsonb NOT NULL,
    evidence_status     text NOT NULL DEFAULT 'observed'
                            CHECK (evidence_status IN ('observed','inferred')),
    source_record_id    uuid REFERENCES source_records(id),
    model_version       text,
    confidence          numeric(5,4) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    observed_at         timestamptz NOT NULL,
    expires_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_property_signal_provenance CHECK (
        (evidence_status = 'observed' AND source_record_id IS NOT NULL)
        OR
        (evidence_status = 'inferred' AND source_record_id IS NOT NULL
                                      AND model_version IS NOT NULL
                                      AND confidence IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_property_signals_lookup
    ON property_signals(tenant_id, property_key, signal_type, observed_at DESC);

-- -------------------------------------------------------------------------
-- Durable jobs, PostgreSQL leases, attempts, and harvest state.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automation_jobs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_type            text NOT NULL,
    queue_name          text NOT NULL DEFAULT 'default',
    state               text NOT NULL DEFAULT 'queued'
                            CHECK (state IN ('draft','awaiting_approval','queued','leased',
                                             'running','succeeded','failed','cancelled','dead_letter')),
    risk_class          text NOT NULL DEFAULT 'read_only'
                            CHECK (risk_class IN ('read_only','outreach','live_call','calendar_write',
                                                  'financial','bidding_message','legal_document','role_override')),
    payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
    result              jsonb,
    priority            smallint NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    progress            numeric(5,2) NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    status_message      text,
    scheduled_at        timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    completed_at        timestamptz,
    attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts        integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    next_retry_at       timestamptz,
    lease_owner         text,
    lease_token         uuid,
    lease_expires_at    timestamptz,
    idempotency_key     text NOT NULL,
    approval_id         uuid,
    last_error_code     text,
    last_error          text,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT chk_job_lease CHECK (
        (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR
        (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_automation_jobs_claim
    ON automation_jobs(queue_name, priority, scheduled_at, created_at)
    WHERE state IN ('queued','failed');
CREATE INDEX IF NOT EXISTS idx_automation_jobs_expired_lease
    ON automation_jobs(lease_expires_at)
    WHERE state IN ('leased','running');
CREATE INDEX IF NOT EXISTS idx_automation_jobs_tenant_status
    ON automation_jobs(tenant_id, state, created_at DESC);

CREATE TABLE IF NOT EXISTS automation_job_attempts (
    id                  bigserial PRIMARY KEY,
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id              uuid NOT NULL REFERENCES automation_jobs(id) ON DELETE CASCADE,
    attempt_number      integer NOT NULL,
    worker_id           text NOT NULL,
    lease_token         uuid NOT NULL,
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    outcome             text CHECK (outcome IS NULL OR outcome IN ('succeeded','failed','lease_lost','cancelled')),
    error_code          text,
    error_detail        text,
    metrics             jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS harvest_sources (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_key            text NOT NULL,
    display_name          text NOT NULL,
    jurisdiction          text NOT NULL,
    adapter               text NOT NULL,
    schedule_seconds      integer NOT NULL DEFAULT 86400 CHECK (schedule_seconds >= 300),
    enabled               boolean NOT NULL DEFAULT true,
    cursor_value          text,
    cursor_observed_at    timestamptz,
    last_started_at       timestamptz,
    last_succeeded_at     timestamptz,
    last_record_observed_at timestamptz,
    coverage              jsonb NOT NULL DEFAULT '{}'::jsonb,
    cache_hits            bigint NOT NULL DEFAULT 0 CHECK (cache_hits >= 0),
    cache_misses          bigint NOT NULL DEFAULT 0 CHECK (cache_misses >= 0),
    failure_count         integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    circuit_state         text NOT NULL DEFAULT 'closed'
                              CHECK (circuit_state IN ('closed','open','half_open')),
    circuit_open_until    timestamptz,
    last_error            text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_key)
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id           uuid NOT NULL REFERENCES harvest_sources(id),
    job_id              uuid REFERENCES automation_jobs(id),
    cursor_start        text,
    cursor_end          text,
    state               text NOT NULL DEFAULT 'running'
                            CHECK (state IN ('running','succeeded','failed','cancelled')),
    requests            integer NOT NULL DEFAULT 0,
    fetched             integer NOT NULL DEFAULT 0,
    normalized          integer NOT NULL DEFAULT 0,
    aggregated          integer NOT NULL DEFAULT 0,
    inserted            integer NOT NULL DEFAULT 0,
    cache_hits          integer NOT NULL DEFAULT 0,
    malformed           integer NOT NULL DEFAULT 0,
    retries             integer NOT NULL DEFAULT 0,
    started_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    error_summary       text,
    metrics             jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_harvest_runs_source
    ON harvest_runs(tenant_id, source_id, started_at DESC);

-- -------------------------------------------------------------------------
-- Approvals and commands.  High-risk jobs reference an immutable approval.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS action_approvals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_type         text NOT NULL,
    risk_class          text NOT NULL CHECK (risk_class IN
                            ('outreach','live_call','calendar_write','financial',
                             'bidding_message','legal_document','role_override')),
    target_type         text NOT NULL,
    target_id           text NOT NULL,
    payload_hash        char(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    draft_payload       jsonb NOT NULL,
    status              text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','approved','rejected','revoked','expired')),
    requested_by        text NOT NULL,
    decided_by          text,
    reason              text,
    requested_at        timestamptz NOT NULL DEFAULT now(),
    decided_at          timestamptz,
    expires_at          timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_approval_decision CHECK (
        (status = 'pending' AND decided_by IS NULL AND decided_at IS NULL)
        OR
        (status <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_action_approvals_queue
    ON action_approvals(tenant_id, status, requested_at DESC);

CREATE TABLE IF NOT EXISTS protected_override_events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    approval_id         uuid NOT NULL REFERENCES action_approvals(id),
    override_type       text NOT NULL,
    target_type         text NOT NULL,
    target_id           text NOT NULL,
    prior_value         jsonb NOT NULL,
    new_value           jsonb NOT NULL,
    reason              text NOT NULL CHECK (length(reason) BETWEEN 8 AND 500),
    performed_by        text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_anomaly_alerts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    anomaly_type        text NOT NULL,
    severity            text NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    fingerprint         char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    actor_id            text,
    source_ip           inet,
    route               text,
    evidence            jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_anomalies_recent
    ON audit_anomaly_alerts(tenant_id,created_at DESC,severity);

ALTER TABLE automation_jobs
    DROP CONSTRAINT IF EXISTS automation_jobs_approval_id_fkey;
ALTER TABLE automation_jobs
    ADD CONSTRAINT automation_jobs_approval_id_fkey
    FOREIGN KEY (approval_id) REFERENCES action_approvals(id);

CREATE TABLE IF NOT EXISTS command_executions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    command_type        text NOT NULL CHECK (command_type IN ('EMAIL','CALL','CALENDAR')),
    classification      text NOT NULL,
    risk_class          text NOT NULL,
    target              jsonb NOT NULL,
    draft               jsonb NOT NULL,
    state               text NOT NULL DEFAULT 'awaiting_approval'
                            CHECK (state IN ('draft','awaiting_approval','approved','queued',
                                             'executing','succeeded','failed','cancelled',
                                             'reconciliation_required')),
    approval_id         uuid REFERENCES action_approvals(id),
    job_id              uuid REFERENCES automation_jobs(id),
    idempotency_key     text NOT NULL,
    provider            text,
    provider_reference  text,
    provider_submitted_at timestamptz,
    last_error          text,
    reconciliation_reason text,
    scheduled_at        timestamptz,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS provider_credentials (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider              text NOT NULL CHECK (provider IN ('google','twilio','ses','runpod','mls')),
    account_label         text NOT NULL,
    token_ciphertext      bytea NOT NULL,
    refresh_ciphertext    bytea,
    scopes                text[] NOT NULL DEFAULT '{}',
    expires_at            timestamptz,
    last_validated_at     timestamptz,
    disabled_at           timestamptz,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider, account_label)
);
COMMENT ON COLUMN provider_credentials.token_ciphertext IS
    'pgcrypto ciphertext only; plaintext OAuth/provider credentials are never persisted.';

CREATE TABLE IF NOT EXISTS live_call_sessions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    command_id            uuid REFERENCES command_executions(id),
    client_id             uuid REFERENCES clients(id),
    lead_id               uuid REFERENCES leads(id),
    consent_recorded      boolean NOT NULL DEFAULT false,
    consent_basis         text,
    started_at            timestamptz,
    ended_at              timestamptz,
    transcript_status     text NOT NULL DEFAULT 'pending'
                                CHECK (transcript_status IN ('pending','active','complete','failed','deleted')),
    provider_call_id      text,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_call_anchor CHECK (client_id IS NOT NULL OR lead_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS negotiation_events (
    id                    bigserial PRIMARY KEY,
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_session_id       uuid NOT NULL REFERENCES live_call_sessions(id) ON DELETE CASCADE,
    event_type            text NOT NULL CHECK (event_type IN
                              ('transcript','counter_offer','mao_update','threshold','objection_draft','consent')),
    transcript_excerpt    text,
    counter_offer         numeric(14,2),
    arv                   numeric(14,2),
    rehab                 numeric(14,2),
    mao                   numeric(14,2),
    threshold             text CHECK (threshold IS NULL OR threshold IN ('green','amber','red')),
    payload               jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version         text NOT NULL,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_call_session_command
    ON live_call_sessions(command_id) WHERE command_id IS NOT NULL;

-- -------------------------------------------------------------------------
-- Model registry, consented style training, and validation history.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_registry (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                  text NOT NULL,
    version               text NOT NULL,
    model_kind            text NOT NULL CHECK (model_kind IN
                              ('base','state_lora','agent_style_lora','scoring','forecast','vision')),
    scope_type            text NOT NULL CHECK (scope_type IN ('tenant','state','agent')),
    scope_key             text NOT NULL,
    base_model            text NOT NULL,
    artifact_uri          text NOT NULL,
    artifact_sha256       char(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    model_card            jsonb NOT NULL,
    compatibility         jsonb NOT NULL DEFAULT '{}'::jsonb,
    minimum_gpu_mb        integer CHECK (minimum_gpu_mb IS NULL OR minimum_gpu_mb > 0),
    status                text NOT NULL DEFAULT 'candidate'
                              CHECK (status IN ('candidate','validated','canary','active','fallback','retired','rejected')),
    rollback_model_id     uuid REFERENCES model_registry(id),
    registered_by         text NOT NULL,
    activated_by          text,
    activated_at          timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name, version, scope_type, scope_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_active_scope
    ON model_registry(tenant_id, model_kind, scope_type, scope_key)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS model_evaluations (
    id                    bigserial PRIMARY KEY,
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    model_id              uuid NOT NULL REFERENCES model_registry(id) ON DELETE CASCADE,
    evaluation_set        text NOT NULL,
    metrics               jsonb NOT NULL,
    calibration           jsonb NOT NULL DEFAULT '{}'::jsonb,
    leakage_reviewed      boolean NOT NULL DEFAULT false,
    geographic_bias_reviewed boolean NOT NULL DEFAULT false,
    passed                boolean NOT NULL,
    evaluator             text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS style_training_examples (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id              text NOT NULL,
    consented_at          timestamptz NOT NULL,
    consent_version       text NOT NULL,
    redacted_input        text NOT NULL,
    redacted_output       text NOT NULL,
    pii_scan              jsonb NOT NULL,
    dataset_split         text NOT NULL CHECK (dataset_split IN ('train','evaluation')),
    example_sha256        char(64) NOT NULL CHECK (example_sha256 ~ '^[0-9a-f]{64}$'),
    revoked_at            timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, agent_id, example_sha256)
);

CREATE TABLE IF NOT EXISTS model_training_runs (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id              text,
    state_code            char(2),
    job_id                uuid REFERENCES automation_jobs(id),
    provider              text NOT NULL DEFAULT 'runpod',
    base_model            text NOT NULL,
    dataset_manifest      jsonb NOT NULL,
    status                text NOT NULL DEFAULT 'queued'
                              CHECK (status IN ('queued','running','evaluating','succeeded','failed','cancelled')),
    artifact_sha256       char(64),
    model_card            jsonb,
    rollback_model_id     uuid REFERENCES model_registry(id),
    started_at            timestamptz,
    completed_at          timestamptz,
    error                 text,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------------------
-- Persisted intelligence, entity graph, title, zoning, and inferences.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intelligence_scores (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    property_key          text NOT NULL,
    analysis_type         text NOT NULL,
    evidence_status       text NOT NULL CHECK (evidence_status IN ('observed','inferred','mixed')),
    observation_date      date NOT NULL,
    confidence            numeric(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    model_version         text NOT NULL,
    source_record_ids     uuid[] NOT NULL,
    result                jsonb NOT NULL,
    trace                 jsonb,
    professional_review_status text NOT NULL DEFAULT 'not_required'
                                  CHECK (professional_review_status IN
                                         ('not_required','required','approved','rejected')),
    reviewed_by           text,
    reviewed_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_intelligence_sources CHECK (cardinality(source_record_ids) > 0)
);
CREATE INDEX IF NOT EXISTS idx_intelligence_scores_property
    ON intelligence_scores(tenant_id, property_key, analysis_type, observation_date DESC);

CREATE TABLE IF NOT EXISTS entity_nodes (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type           text NOT NULL CHECK (entity_type IN
                              ('property','person_of_record','acquisition_entity','address','officer','deed','public_filing')),
    canonical_key         text NOT NULL,
    display_label         text NOT NULL,
    attributes            jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_record_id      uuid NOT NULL REFERENCES source_records(id),
    observed_at           timestamptz NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, entity_type, canonical_key)
);

CREATE TABLE IF NOT EXISTS entity_links (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    from_node_id          uuid NOT NULL REFERENCES entity_nodes(id) ON DELETE CASCADE,
    to_node_id            uuid NOT NULL REFERENCES entity_nodes(id) ON DELETE CASCADE,
    relationship          text NOT NULL,
    attributes            jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_record_id      uuid NOT NULL REFERENCES source_records(id),
    confidence            numeric(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    match_status          text NOT NULL CHECK (match_status IN ('exact','probable','unresolved')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, from_node_id, to_node_id, relationship, source_record_id),
    CONSTRAINT chk_entity_no_self_link CHECK (from_node_id <> to_node_id)
);

CREATE TABLE IF NOT EXISTS title_findings (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    property_key          text NOT NULL,
    finding_type          text NOT NULL,
    record_id             text,
    amount                numeric(14,2),
    recorded_at           date,
    released_at           date,
    match_status          text NOT NULL CHECK (match_status IN
                              ('matched','possible_match','unresolved','released')),
    chain_gap             boolean NOT NULL DEFAULT false,
    source_record_id      uuid NOT NULL REFERENCES source_records(id),
    notes                 text,
    review_status         text NOT NULL DEFAULT 'required'
                              CHECK (review_status IN ('required','confirmed','dismissed')),
    reviewed_by           text,
    reviewed_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_title_findings_property
    ON title_findings(tenant_id, property_key, review_status, created_at DESC);

CREATE TABLE IF NOT EXISTS zoning_analyses (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    property_key          text NOT NULL,
    source_record_ids     uuid[] NOT NULL,
    zoning_district       text NOT NULL,
    effective_version     text NOT NULL,
    lot_area_sqft         numeric(14,2) NOT NULL,
    building_area_sqft    numeric(14,2) NOT NULL,
    current_far           numeric(10,4),
    max_far               numeric(10,4),
    remaining_buildable_sqft numeric(14,2),
    lot_coverage          numeric(8,4),
    permitted_uses        text[] NOT NULL DEFAULT '{}',
    dimensional_limits    jsonb NOT NULL DEFAULT '{}'::jsonb,
    comparable_land_sales jsonb NOT NULL DEFAULT '[]'::jsonb,
    result                jsonb NOT NULL,
    model_version         text NOT NULL,
    review_status         text NOT NULL DEFAULT 'required'
                              CHECK (review_status IN ('required','approved','rejected')),
    reviewed_by           text,
    reviewed_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_zoning_sources CHECK (cardinality(source_record_ids) > 0)
);

CREATE TABLE IF NOT EXISTS property_characteristic_inferences (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    property_key          text NOT NULL,
    characteristic       text NOT NULL,
    inferred_value        jsonb NOT NULL,
    confidence            numeric(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    model_version         text NOT NULL,
    source_record_ids     uuid[] NOT NULL,
    method                text NOT NULL CHECK (method IN ('statistical','photo_estimate','geospatial')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_inference_sources CHECK (cardinality(source_record_ids) > 0)
);

CREATE TABLE IF NOT EXISTS spatial_tour_variants (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id               uuid REFERENCES leads(id) ON DELETE CASCADE,
    listing_id            uuid REFERENCES listings(id) ON DELETE CASCADE,
    property_key          text NOT NULL,
    source_record_ids     uuid[] NOT NULL,
    source_media_ids      uuid[] NOT NULL,
    variant_name          text NOT NULL,
    style                 text NOT NULL,
    rehab_scope           jsonb NOT NULL,
    model_version         text NOT NULL,
    state                 text NOT NULL DEFAULT 'queued'
                              CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    disclosure            text NOT NULL,
    manifest              jsonb,
    job_id                uuid REFERENCES automation_jobs(id),
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_spatial_variant_anchor CHECK (lead_id IS NOT NULL OR listing_id IS NOT NULL),
    CONSTRAINT chk_spatial_variant_sources CHECK (
        cardinality(source_record_ids) > 0 AND cardinality(source_media_ids) > 0
    )
);

-- -------------------------------------------------------------------------
-- Brokerage transaction operations, buyer book, and marketplace disposition.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_parties (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    transaction_id        uuid NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    party_role            text NOT NULL CHECK (party_role IN
                              ('seller','buyer','assignor','assignee','agent','broker','attorney','title','lender','joint_venture')),
    client_id             uuid REFERENCES clients(id),
    display_name          text NOT NULL,
    contact_ciphertext    bytea,
    verified_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS team_memberships (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_name             text NOT NULL,
    title                 text,
    member_role           text NOT NULL DEFAULT 'agent'
                              CHECK (member_role IN ('agent','team_lead','broker')),
    status                text NOT NULL DEFAULT 'pending_broker_approval'
                              CHECK (status IN ('invited','pending_broker_approval','active','suspended','rejected')),
    training_status       text NOT NULL DEFAULT 'not_started'
                              CHECK (training_status IN ('not_started','examples_ready','training','awaiting_validation','validated','active','failed')),
    approval_id           uuid REFERENCES action_approvals(id),
    approved_by           text,
    approved_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,user_id)
);

CREATE TABLE IF NOT EXISTS agent_licenses (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state_code            char(2) NOT NULL,
    license_number        text NOT NULL,
    license_type          text NOT NULL DEFAULT 'salesperson',
    expires_on            date,
    verification_status   text NOT NULL DEFAULT 'pending'
                              CHECK (verification_status IN ('pending','verified','rejected','expired')),
    verified_by           text,
    verified_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,user_id,state_code,license_number)
);

CREATE TABLE IF NOT EXISTS agent_ai_settings (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    approved_tone         text NOT NULL DEFAULT 'neutral'
                              CHECK (approved_tone IN ('concise','warm','formal','neutral','direct')),
    autonomous_research   boolean NOT NULL DEFAULT true,
    autonomous_drafting   boolean NOT NULL DEFAULT true,
    outreach_requires_approval boolean NOT NULL DEFAULT true CHECK (outreach_requires_approval),
    calls_require_approval boolean NOT NULL DEFAULT true CHECK (calls_require_approval),
    legal_requires_approval boolean NOT NULL DEFAULT true CHECK (legal_requires_approval),
    style_training_opt_in boolean NOT NULL DEFAULT false,
    preferences           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,user_id)
);

CREATE TABLE IF NOT EXISTS transaction_milestones (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    transaction_id        uuid NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    milestone_type        text NOT NULL,
    title                 text NOT NULL,
    status                text NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','at_risk','complete','waived','cancelled')),
    due_at                timestamptz,
    completed_at          timestamptz,
    assigned_to           text,
    metadata              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transaction_id, milestone_type)
);

CREATE TABLE IF NOT EXISTS buyer_profiles (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id             uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    states                char(2)[] NOT NULL DEFAULT '{}',
    counties              text[] NOT NULL DEFAULT '{}',
    property_types        text[] NOT NULL DEFAULT '{}',
    min_price             numeric(14,2),
    max_price             numeric(14,2),
    min_beds              smallint,
    min_sqft              integer,
    max_rehab             numeric(14,2),
    strategies            text[] NOT NULL DEFAULT '{}',
    verification_status   text NOT NULL DEFAULT 'unverified'
                              CHECK (verification_status IN ('unverified','identity_verified','funds_verified')),
    acquisition_history_verified boolean NOT NULL DEFAULT false,
    explicit_preferences  jsonb NOT NULL DEFAULT '{}'::jsonb,
    active                boolean NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, client_id)
);

CREATE TABLE IF NOT EXISTS buyer_requests (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    buyer_profile_id      uuid NOT NULL REFERENCES buyer_profiles(id) ON DELETE CASCADE,
    request_name          text NOT NULL,
    criteria              jsonb NOT NULL,
    status                text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','paused','filled','expired')),
    expires_at            timestamptz,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS marketplace_publications (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id               uuid NOT NULL REFERENCES leads(id),
    transaction_id        uuid REFERENCES transactions(id),
    contract_document_id  uuid,
    state                 text NOT NULL DEFAULT 'draft'
                              CHECK (state IN ('draft','approved','published','under_offer','assigned','withdrawn','expired')),
    visibility            text NOT NULL DEFAULT 'platform'
                              CHECK (visibility IN ('tenant','platform')),
    truthful_summary      jsonb NOT NULL,
    asking_price          numeric(14,2),
    published_at          timestamptz,
    approved_by           text,
    approval_id           uuid REFERENCES action_approvals(id),
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, lead_id)
);

CREATE TABLE IF NOT EXISTS marketplace_matches (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    publication_id        uuid NOT NULL REFERENCES marketplace_publications(id) ON DELETE CASCADE,
    buyer_request_id      uuid NOT NULL REFERENCES buyer_requests(id) ON DELETE CASCADE,
    match_score           numeric(5,4) NOT NULL CHECK (match_score BETWEEN 0 AND 1),
    criteria_trace        jsonb NOT NULL,
    acquisition_history_verified boolean NOT NULL DEFAULT false,
    state                 text NOT NULL DEFAULT 'candidate'
                              CHECK (state IN ('candidate','shortlisted','contact_approved','contacted','passed','offer')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publication_id, buyer_request_id)
);

-- -------------------------------------------------------------------------
-- Approved contract templates, generalized generated documents, and vault.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_templates (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    template_key          text NOT NULL,
    version               text NOT NULL,
    document_type         text NOT NULL CHECK (document_type IN
                              ('assignment','seller_purchase','buyer_purchase','joint_venture','redline')),
    jurisdiction          text NOT NULL,
    body_template         text NOT NULL,
    required_fields       text[] NOT NULL,
    template_sha256       char(64) NOT NULL CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
    status                text NOT NULL DEFAULT 'draft'
                              CHECK (status IN ('draft','approved','retired','rejected')),
    attorney_reviewed_by  text,
    attorney_reviewed_at  timestamptz,
    approval_notes        text,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, template_key, version),
    CONSTRAINT chk_contract_template_approval CHECK (
        status <> 'approved'
        OR (attorney_reviewed_by IS NOT NULL AND attorney_reviewed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS contract_documents (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    transaction_id        uuid REFERENCES transactions(id),
    lead_id               uuid REFERENCES leads(id),
    document_type         text NOT NULL CHECK (document_type IN
                              ('assignment','seller_purchase','buyer_purchase','joint_venture','redline')),
    template_key          text NOT NULL,
    template_version      text NOT NULL,
    input_hash            char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    content_ciphertext    bytea NOT NULL,
    status                text NOT NULL DEFAULT 'draft'
                              CHECK (status IN ('draft','review_required','approved','signed','void')),
    attorney_review_required boolean NOT NULL DEFAULT true,
    reviewed_by           text,
    reviewed_at           timestamptz,
    approval_id           uuid REFERENCES action_approvals(id),
    s3_bucket             text,
    s3_key                text,
    artifact_sha256       char(64),
    encryption            text CHECK (encryption IS NULL OR encryption IN ('AES256','aws:kms')),
    metadata              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_contract_anchor CHECK (transaction_id IS NOT NULL OR lead_id IS NOT NULL),
    CONSTRAINT chk_contract_review CHECK (
        status NOT IN ('approved','signed') OR reviewed_by IS NOT NULL
    )
);

ALTER TABLE marketplace_publications
    DROP CONSTRAINT IF EXISTS marketplace_publications_contract_document_id_fkey;
ALTER TABLE marketplace_publications
    ADD CONSTRAINT marketplace_publications_contract_document_id_fkey
    FOREIGN KEY (contract_document_id) REFERENCES contract_documents(id);

-- Existing hashed portal links become scoped dossier links without weakening
-- the SECURITY DEFINER hash-only token resolver from migration 0008.
ALTER TABLE client_portals
    ADD COLUMN IF NOT EXISTS link_kind text NOT NULL DEFAULT 'seller'
        CHECK (link_kind IN ('seller','joint_venture')),
    ADD COLUMN IF NOT EXISTS asset_scope jsonb NOT NULL DEFAULT '{"summary":true}'::jsonb,
    ADD COLUMN IF NOT EXISTS watermark_text text,
    ADD COLUMN IF NOT EXISTS issued_to_label text;

-- Extend the exact-digest resolver with immutable scope and watermark claims.
-- It remains the only unauthenticated cross-tenant lookup and still accepts
-- only the SHA-256 digest, never the plaintext bearer token.
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
            tenant_id,lead_id,portal_id,actor_role,interaction_type,payload
        )
        SELECT h.tenant_id,h.lead_id,h.id,
               CASE WHEN h.link_kind='joint_venture' THEN 'buyer' ELSE 'seller' END,
               'portal_view',
               jsonb_build_object('link_kind',h.link_kind,'asset_scope',h.asset_scope)
          FROM hit h
    )
    SELECT h.id,h.tenant_id,h.lead_id,h.access_expires_at,h.link_kind,
           h.asset_scope,h.watermark_text,h.issued_to_label
      FROM hit h;
END $$;
REVOKE ALL ON FUNCTION resolve_portal_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_portal_token(text) TO oracle_app;

-- -------------------------------------------------------------------------
-- updated_at triggers.
-- -------------------------------------------------------------------------
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'source_licenses','automation_jobs','harvest_sources','command_executions',
        'provider_credentials','model_registry','transaction_milestones',
        'team_memberships','agent_licenses','agent_ai_settings',
        'buyer_profiles','buyer_requests','marketplace_publications',
        'marketplace_matches','contract_templates','contract_documents',
        'spatial_tour_variants'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname = 'trg_' || table_name || '_updated'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
                'trg_' || table_name || '_updated', table_name
            );
        END IF;
    END LOOP;
END $$;

-- Published platform inventory is readable across brokerages, while every
-- mutation remains confined to the owning tenant.  Drafts never cross the wall.
DROP POLICY IF EXISTS marketplace_publications_tenant_isolation ON marketplace_publications;
DROP POLICY IF EXISTS marketplace_publications_read ON marketplace_publications;
DROP POLICY IF EXISTS marketplace_publications_insert ON marketplace_publications;
DROP POLICY IF EXISTS marketplace_publications_update ON marketplace_publications;
DROP POLICY IF EXISTS marketplace_publications_delete ON marketplace_publications;
CREATE POLICY marketplace_publications_read ON marketplace_publications
    FOR SELECT USING (
        app_is_platform_admin()
        OR tenant_id = app_current_tenant()
        OR (visibility='platform' AND state IN ('published','under_offer'))
    );
CREATE POLICY marketplace_publications_insert ON marketplace_publications
    FOR INSERT WITH CHECK (
        app_is_platform_admin() OR tenant_id = app_current_tenant()
    );
CREATE POLICY marketplace_publications_update ON marketplace_publications
    FOR UPDATE USING (
        app_is_platform_admin() OR tenant_id = app_current_tenant()
    ) WITH CHECK (
        app_is_platform_admin() OR tenant_id = app_current_tenant()
    );
CREATE POLICY marketplace_publications_delete ON marketplace_publications
    FOR DELETE USING (
        app_is_platform_admin() OR tenant_id = app_current_tenant()
    );

-- -------------------------------------------------------------------------
-- Canonical tenant isolation.  Every operational table is FORCE RLS.
-- -------------------------------------------------------------------------
DO $$
DECLARE table_name text;
DECLARE policy_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'source_licenses','source_records','property_signals','automation_jobs',
        'automation_job_attempts','harvest_sources','harvest_runs','action_approvals',
        'protected_override_events','audit_anomaly_alerts','command_executions',
        'provider_credentials','live_call_sessions',
        'negotiation_events','model_registry','model_evaluations',
        'style_training_examples','model_training_runs','intelligence_scores',
        'entity_nodes','entity_links','title_findings','zoning_analyses',
        'property_characteristic_inferences','spatial_tour_variants','transaction_parties',
        'transaction_milestones','team_memberships','agent_licenses',
        'agent_ai_settings','buyer_profiles','buyer_requests',
        'marketplace_publications','marketplace_matches','contract_templates',
        'contract_documents'
    ] LOOP
        policy_name := table_name || '_tenant_isolation';
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', policy_name, table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING '
            '(app_is_platform_admin() OR tenant_id = app_current_tenant()) '
            'WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant())',
            policy_name, table_name
        );
    END LOOP;
END $$;

-- Evidence and telemetry ledgers are append-only to the application role.
REVOKE UPDATE, DELETE, TRUNCATE ON source_records FROM oracle_app;
REVOKE UPDATE, DELETE, TRUNCATE ON automation_job_attempts FROM oracle_app;
REVOKE UPDATE, DELETE, TRUNCATE ON model_evaluations FROM oracle_app;
REVOKE UPDATE, DELETE, TRUNCATE ON negotiation_events FROM oracle_app;
REVOKE UPDATE, DELETE, TRUNCATE ON protected_override_events FROM oracle_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_anomaly_alerts FROM oracle_app;

-- Retention is the sole sanctioned mutation of the append-only evidence and
-- negotiation ledgers. It preserves hashes, timestamps, source identity, and
-- numerical audit facts while removing expired raw payloads/transcript text.
-- SECURITY DEFINER is required because the application role is intentionally
-- denied UPDATE/DELETE above; the platform-admin GUC check prevents tenant
-- agents from invoking this cross-tenant maintenance path.
CREATE OR REPLACE FUNCTION purge_expired_platform_data(
    p_raw_source_days integer,
    p_transcript_days integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    source_count integer := 0;
    transcript_count integer := 0;
    cache_count integer := 0;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'platform administrator context required'
            USING ERRCODE = '42501';
    END IF;
    IF p_raw_source_days NOT BETWEEN 1 AND 3650
       OR p_transcript_days NOT BETWEEN 1 AND 3650 THEN
        RAISE EXCEPTION 'retention days must be between 1 and 3650'
            USING ERRCODE = '22023';
    END IF;

    UPDATE source_records
       SET raw_payload = jsonb_build_object(
               'retention_status', 'purged',
               'payload_sha256', payload_hash
           ),
           purged_at = now()
     WHERE purged_at IS NULL
       AND (
           (expires_at IS NOT NULL AND expires_at <= now())
           OR
           (expires_at IS NULL
            AND retrieved_at < now() - make_interval(days => p_raw_source_days))
       );
    GET DIAGNOSTICS source_count = ROW_COUNT;

    UPDATE negotiation_events
       SET transcript_excerpt = NULL,
           payload = (payload - 'transcript' - 'text' - 'utterance')
                     || '{"retention_status":"transcript_purged"}'::jsonb
     WHERE event_type = 'transcript'
       AND transcript_excerpt IS NOT NULL
       AND created_at < now() - make_interval(days => p_transcript_days);
    GET DIAGNOSTICS transcript_count = ROW_COUNT;

    DELETE FROM di_cache
     WHERE COALESCE(stale_until, expires_at) IS NOT NULL
       AND COALESCE(stale_until, expires_at) < now();
    GET DIAGNOSTICS cache_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'source_payloads_purged', source_count,
        'transcripts_purged', transcript_count,
        'cache_rows_deleted', cache_count
    );
END;
$$;
REVOKE ALL ON FUNCTION purge_expired_platform_data(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_expired_platform_data(integer, integer) TO oracle_app;

COMMIT;
