-- 0019_client_enterprise.sql — Neoh enterprise client-management substrate.
-- Additive + idempotent. Extends `clients` with lifecycle/scoring/assignment
-- and adds first-class tags, authored notes, tasks/reminders, a unified activity
-- timeline, and saved segments. All tenant tables follow the 0001 RLS posture:
--   USING/CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant())
-- plus FORCE so table owners can't bypass (matches 0018). Triggers reuse the
-- existing set_updated_at() function.
--
-- Apply manually (compose initdb only runs on first boot):
--   docker exec -i oracle-db-1 psql -U postgres -d oracle < 0019_client_enterprise.sql

BEGIN;

-- ── 1. Extend clients with enterprise fields ────────────────────────────────
ALTER TABLE clients ADD COLUMN IF NOT EXISTS stage             text NOT NULL DEFAULT 'lead';
ALTER TABLE clients ADD COLUMN IF NOT EXISTS lead_score        smallint NOT NULL DEFAULT 0;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS assignee_id       text;            -- agent_id (TEXT, matches users debt)
ALTER TABLE clients ADD COLUMN IF NOT EXISTS company           text;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS last_contacted_at timestamptz;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_client_stage') THEN
        ALTER TABLE clients ADD CONSTRAINT chk_client_stage CHECK (stage IN
            ('lead', 'active', 'nurture', 'under_contract', 'closed', 'lost'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_client_score') THEN
        ALTER TABLE clients ADD CONSTRAINT chk_client_score CHECK (lead_score BETWEEN 0 AND 100);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_clients_stage    ON clients(tenant_id, stage)      WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_clients_assignee ON clients(tenant_id, assignee_id) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_clients_score    ON clients(tenant_id, lead_score DESC);

-- ── 2. client_tags — first-class tags (one row per tag) ─────────────────────
CREATE TABLE IF NOT EXISTS client_tags (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id   uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    tag         text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_tags ON client_tags(tenant_id, client_id, lower(tag));
CREATE INDEX IF NOT EXISTS idx_client_tags_tag ON client_tags(tenant_id, lower(tag));

-- ── 3. client_notes — timestamped, authored notes (pinnable) ────────────────
CREATE TABLE IF NOT EXISTS client_notes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id   uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    body        text NOT NULL,
    author_id   text,
    pinned      boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_client_notes_client ON client_notes(client_id, pinned DESC, created_at DESC);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_client_notes_updated') THEN
        CREATE TRIGGER trg_client_notes_updated
            BEFORE UPDATE ON client_notes
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- ── 4. client_tasks — follow-ups / reminders ────────────────────────────────
CREATE TABLE IF NOT EXISTS client_tasks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id   uuid REFERENCES clients(id) ON DELETE CASCADE,   -- nullable: standalone tasks ok
    title       text NOT NULL,
    details     text,
    due_at      timestamptz,
    status      text NOT NULL DEFAULT 'open',
    priority    text NOT NULL DEFAULT 'normal',
    assignee_id text,
    created_by  text,
    completed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_task_status   CHECK (status   IN ('open', 'done', 'snoozed', 'cancelled')),
    CONSTRAINT chk_task_priority CHECK (priority IN ('low', 'normal', 'high', 'urgent'))
);
CREATE INDEX IF NOT EXISTS idx_client_tasks_client ON client_tasks(client_id, due_at);
CREATE INDEX IF NOT EXISTS idx_client_tasks_queue  ON client_tasks(tenant_id, status, due_at) WHERE status = 'open';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_client_tasks_updated') THEN
        CREATE TRIGGER trg_client_tasks_updated
            BEFORE UPDATE ON client_tasks
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- ── 5. client_activities — unified, append-only timeline feed ───────────────
CREATE TABLE IF NOT EXISTS client_activities (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id   uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    kind        text NOT NULL,            -- created|stage_change|score_change|note|task|message|showing|tag|system
    summary     text NOT NULL,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    actor       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_client_activities_client ON client_activities(client_id, created_at DESC);

-- ── 6. client_segments — saved views / smart filters ────────────────────────
CREATE TABLE IF NOT EXISTS client_segments (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_id    text,
    name        text NOT NULL,
    definition  jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {type,stage,tags[],score_min,q,sort}
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_client_segments_tenant ON client_segments(tenant_id, name);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_client_segments_updated') THEN
        CREATE TRIGGER trg_client_segments_updated
            BEFORE UPDATE ON client_segments
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- ── 7. RLS — enable + FORCE + canonical tenant-isolation policy ─────────────
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['client_tags','client_notes','client_tasks','client_activities','client_segments']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant_isolation', t);
        EXECUTE format(
            'CREATE POLICY %I ON %I '
            'USING (app_is_platform_admin() OR tenant_id = app_current_tenant()) '
            'WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant())',
            t || '_tenant_isolation', t);
    END LOOP;
END $$;

COMMIT;
