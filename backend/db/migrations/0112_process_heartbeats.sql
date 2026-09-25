-- 0112 — process heartbeats: a worker you can see is alive.
--
-- The release smoke test only ever checked the API. The worker has no HTTP
-- route, and nothing it does is guaranteed to be visible within a release
-- window: job leases heartbeat only WHILE a job runs, and the periodic
-- scheduler ticks hourly by default. An idle healthy worker and a crashed one
-- were indistinguishable, so a release whose worker died on boot passed its
-- smoke test and stopped running every background job — MLS syncs, mission
-- ticks, the usage drain — until somebody noticed the absence.
--
-- Each background-work process now upserts one row here every 30 s, carrying
-- the git SHA it was built from. That answers two questions at once: is a
-- worker alive, and has it actually rolled over to the release being checked.
-- The second matters as much as the first: a worker still running the PREVIOUS
-- image after a deploy is a release where the API and the jobs disagree about
-- the schema.
--
-- Platform-level, not tenant data — no tenant_id, no RLS, like mls_sync_status.
-- Additive only (migration_safety classifies it so): a previous release simply
-- never writes here.

CREATE TABLE IF NOT EXISTS process_heartbeats (
    process_id   text        PRIMARY KEY,
    role         text        NOT NULL,
    git_sha      text        NOT NULL,
    hostname     text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_process_heartbeats_role_seen
    ON process_heartbeats (role, last_seen_at DESC);

COMMENT ON TABLE process_heartbeats IS
    'One row per background-work process, refreshed every ~30 s. Read by '
    'GET /health/workers and the release smoke test. Rows older than a day '
    'are pruned by the heartbeat loop itself.';

-- Explicit, because 0003 revokes PUBLIC from everything and a table the app
-- role cannot write is a heartbeat that silently never beats.
GRANT SELECT, INSERT, UPDATE, DELETE ON process_heartbeats TO oracle_app;
