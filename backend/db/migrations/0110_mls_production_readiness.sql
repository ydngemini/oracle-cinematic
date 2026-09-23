-- 0110 — MLS production readiness: feed health, freshness, provenance, entitlement.
--
-- Four problems this closes.
--
-- 1. Provenance was inferred from a naming convention and failed OPEN. Any
--    dataset slug that was not one of three known developer names became
--    `licensed_property_listing` automatically. The live configuration is
--    `actris_ref` — a Bridge REFERENCE dataset, frozen sample inventory, which
--    the adapter's own docstring describes as having every ModificationTimestamp
--    frozen — and its 52,622 rows are stamped as licensed inventory today.
--    Classification now comes from mls_licensing.py, which fails closed, and is
--    stored in a real column instead of being buried in JSONB where nothing can
--    enforce or index it.
--
-- 2. Freshness could not be measured. `source_modified_at` lived inside the
--    `features` JSONB and was only reachable through a text-cast expression
--    index, so a delta cursor was a string comparison against a JSON field.
--
-- 3. mls_sync_status recorded that a sync happened, not whether the feed is
--    healthy: no attempted-vs-succeeded distinction, no cursor, no backfill
--    progress, no error class. An operator could not answer "is MLS working?"
--    without reading application logs.
--
-- 4. Listings are platform-wide with no entitlement boundary at all, so one
--    brokerage's licensed feed is visible to every tenant.
--
-- On why entitlement is NOT row-level security here: oracle_mls_listings
-- carries two property-matching indexes built on regexp_replace() expressions.
-- Migration 0102 and a production incident established that a non-leakproof
-- expression index is silently unusable under FORCE RLS — the planner simply
-- stops choosing it. Adding RLS to this table would therefore kill address and
-- parcel matching without any error. Entitlement is enforced at the FEED grain
-- instead: a tenant is entitled to a set of mls_id values, and every query
-- narrows to that set server-side.

BEGIN;

-- ── Canonical provenance and freshness, promoted out of JSONB ────────────
ALTER TABLE oracle_mls_listings
    ADD COLUMN IF NOT EXISTS license_classification text NOT NULL
        DEFAULT 'developer_listing_dataset'
        CHECK (license_classification IN ('developer_listing_dataset', 'licensed_property_listing')),
    ADD COLUMN IF NOT EXISTS source_modified_at timestamptz,
    ADD COLUMN IF NOT EXISTS source_status text;

-- Backfill from where these used to live.
UPDATE oracle_mls_listings
   SET source_modified_at = NULLIF(features->>'source_modified_at', '')::timestamptz
 WHERE source_modified_at IS NULL
   AND features ? 'source_modified_at';

UPDATE oracle_mls_listings
   SET source_status = NULLIF(features->>'source_status', '')
 WHERE source_status IS NULL
   AND features ? 'source_status';

-- Correct the mislabelled rows. Everything currently stored was ingested
-- under the fail-open rule; the only dataset ever configured is a reference
-- dataset, so nothing in this table is licensed inventory. An operator who
-- connects a genuinely licensed feed will have its rows written as licensed
-- by the ingestion path, and may reclassify historical rows deliberately.
UPDATE oracle_mls_listings
   SET license_classification = 'developer_listing_dataset'
 WHERE license_classification <> 'developer_listing_dataset'
   AND COALESCE(features->'provenance'->>'provider_id', '') IN ('actris', 'bridge_dev');

-- Searches are always narrowed by entitlement and almost always by
-- classification, so both lead.
CREATE INDEX IF NOT EXISTS idx_oml_feed_class_status
    ON oracle_mls_listings (mls_id, license_classification, status);

CREATE INDEX IF NOT EXISTS idx_oml_feed_modified
    ON oracle_mls_listings (mls_id, source_modified_at DESC NULLS LAST);

-- ── Feed identity and health ─────────────────────────────────────────────
-- Extending mls_sync_status rather than adding a second feed table: it is
-- already keyed on mls_id, which is the right grain, and two tables claiming
-- to know whether a feed is healthy would disagree.
ALTER TABLE mls_sync_status
    ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS dataset text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS license_classification text NOT NULL
        DEFAULT 'developer_listing_dataset'
        CHECK (license_classification IN ('developer_listing_dataset', 'licensed_property_listing')),
    ADD COLUMN IF NOT EXISTS license_reason text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS agreement_ref text,
    ADD COLUMN IF NOT EXISTS health text NOT NULL DEFAULT 'NOT_CONFIGURED'
        CHECK (health IN ('NOT_CONFIGURED', 'CONFIGURED', 'BACKFILLING', 'READY',
                          'STALE', 'DEGRADED', 'ERROR', 'AUTH_ERROR', 'RATE_LIMITED')),
    -- Attempted and succeeded are separate on purpose: a feed that has been
    -- retrying every ten minutes for a day is not the same as one that has not
    -- run, and last_sync_at alone cannot tell them apart.
    ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_success_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error text,
    ADD COLUMN IF NOT EXISTS last_error_at timestamptz,
    -- Classified, because the three that matter need different operator
    -- responses: auth will never succeed on retry, rate_limit wants backoff,
    -- transport wants a retry. An unclassified error string cannot be
    -- alerted on without regex-matching provider prose.
    ADD COLUMN IF NOT EXISTS last_error_class text
        CHECK (last_error_class IS NULL OR last_error_class IN
               ('auth', 'rate_limit', 'transport', 'provider', 'data')),
    ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    -- The durable delta cursor. Advanced only after a page is fully accepted,
    -- so a partial failure cannot make later records look synchronised.
    ADD COLUMN IF NOT EXISTS cursor_modified_at timestamptz,
    ADD COLUMN IF NOT EXISTS cursor_key text,
    -- Backfill progress, so a restart resumes instead of starting over and a
    -- half-finished walk is never mistaken for a complete one.
    ADD COLUMN IF NOT EXISTS backfill_complete boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS backfill_cursor_key text,
    ADD COLUMN IF NOT EXISTS backfill_records integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS stale_after_minutes integer NOT NULL DEFAULT 1440
        CHECK (stale_after_minutes > 0),
    ADD COLUMN IF NOT EXISTS last_run jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_mls_sync_health ON mls_sync_status (health);

-- ── Entitlement ──────────────────────────────────────────────────────────
-- Which feeds a brokerage may see. Absent row means not entitled: one
-- brokerage's licensed data must not become every tenant's data just because
-- the listings table is shared.
CREATE TABLE IF NOT EXISTS mls_feed_entitlements (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mls_id      text NOT NULL,
    granted_by  text NOT NULL,
    granted_at  timestamptz NOT NULL DEFAULT now(),
    note        text,
    PRIMARY KEY (tenant_id, mls_id)
);

CREATE INDEX IF NOT EXISTS idx_mls_entitlements_tenant
    ON mls_feed_entitlements (tenant_id);

ALTER TABLE mls_feed_entitlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE mls_feed_entitlements FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mls_feed_entitlements_tenant_isolation ON mls_feed_entitlements;
CREATE POLICY mls_feed_entitlements_tenant_isolation ON mls_feed_entitlements
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Granting entitlement is an operator act, not a tenant self-service one.
REVOKE ALL ON mls_feed_entitlements FROM PUBLIC;
REVOKE ALL ON mls_feed_entitlements FROM oracle_app;
GRANT SELECT ON mls_feed_entitlements TO oracle_app;
GRANT INSERT, DELETE ON mls_feed_entitlements TO oracle_app;

COMMIT;
