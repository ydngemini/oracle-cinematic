-- 0086 — the two indexes behind every TimeoutError() in automation_jobs
--
-- Both dead-letter queues traced to one shape: a query whose predicate no index
-- could serve, run against a multi-million-row table, exceeding the pool's
-- command_timeout=30 (db/connection.py) and surfacing as a bare `TimeoutError()`
-- with no message. 67 crm:client_reconcile and 9 periodic:lead_payload_
-- normalization jobs died this way, the most recent minutes before this file.
--
--   1. crm:client_reconcile -> _property_candidates (client_ai_automation.py:554)
--      matches an owner by normalising the name inline:
--        regexp_replace(lower(COALESCE(owner_name,'')),'[^a-z0-9]','','g') = $1
--      No index can serve a function applied to a column, so this seq-scanned
--      all 8.59M public_property_records. Measured: EXPLAIN ANALYZE had not
--      returned after 120s — four times the timeout that kills the job. Every
--      client reconciliation ran it, which is why that queue is the loudest.
--
--      The index below stores the same normalised expression, so the planner
--      matches it verbatim. record_refreshed_at DESC is the second column so
--      the query's ORDER BY ... LIMIT 5 is answered from the index rather than
--      by sorting the matches.
--
--   2. periodic:lead_payload_normalization (harvesters/base.py:1079) asks
--      "which harvested leads still carry a stale payload envelope?". Measured
--      plan: Parallel Seq Scan, 8.4M rows examined, 2.7M buffers, 24.8s — and
--      `actual rows=0`. There is no work left; the job burns ~20GB of I/O every
--      cycle to rediscover that, and tips over 30s whenever the box is busy.
--
--      This is a backfill that has finished, so the right index is one that
--      holds exactly the outstanding work: empty today, instant to probe, and
--      it fills itself the moment genuinely stale rows appear.
--
-- Both are plain CREATE INDEX, matching 0076 — run_migrations.py wraps each
-- migration in a transaction and CONCURRENTLY cannot run inside one.

-- 1. Owner-name lookup for _property_candidates.
CREATE INDEX IF NOT EXISTS idx_public_property_owner_normalized
    ON public_property_records (
        (regexp_replace(lower(COALESCE(owner_name, '')), '[^a-z0-9]', '', 'g')),
        record_refreshed_at DESC
    );

-- 2. Outstanding lead-payload normalisation work.
--
-- The '3' literal is LEAD_PAYLOAD_SCHEMA_VERSION (harvesters/base.py:69). That
-- coupling is deliberate and it is the whole point: a partial index is what
-- makes "is there work?" free. It is also a trap, because bumping the constant
-- without adding the matching index would silently restore the 24.8s seq scan
-- with nothing failing loudly.
--
-- tests/test_lead_payload_quality.py asserts a migration exists carrying the
-- current constant, so that bump fails in CI instead of in production latency.
CREATE INDEX IF NOT EXISTS idx_leads_pending_payload_normalization
    ON leads (updated_at, id)
 WHERE (
           underwriting->>'source' LIKE 'firehose:%'
        OR underwriting->>'source' = 'md_sdat'
       )
   AND COALESCE(payload->>'schema_version', '') <> '3';
