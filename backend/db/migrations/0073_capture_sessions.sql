-- Tie a reconstruction to the media that produced it.
--
-- A finished splat is a property_media row with a provenance and a generator
-- (0071) and nothing else. Which photos went into it is unrecoverable: the
-- worker gathers whatever is attached to the property at the moment it runs, so
-- the input set is a function of time, not a record. Two consequences:
--
--   * a bad capture cannot be diagnosed — "was the hallway even in this?" has
--     no answer;
--   * a re-capture cannot supersede a bad one, because nothing says which splat
--     came from which attempt. They just accumulate, and the resolver picks by
--     sort order.
--
-- A session is one attempt: the media the agent gathered for it, the job it
-- produced, and the outcome. It is deliberately a *record* rather than a
-- container — media rows still belong to the property, and a photo can be used
-- by more than one attempt. `capture_session_id` on property_media marks which
-- attempt a row was captured FOR, not which attempts consumed it.
--
-- Depends on 0012 (property_media), 0023 (reconstruction_jobs).

BEGIN;

CREATE TABLE IF NOT EXISTS capture_sessions (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL,

    lead_id       uuid,
    listing_id    uuid,

    -- What the agent was capturing with, when they know. Free text: an honest
    -- "iPhone 15 Pro" from a picker beats a normalised enum nobody fills in.
    device        text,
    -- Which revision of the shooting guidance they were shown, so a change in
    -- the advice can be correlated with a change in capture quality.
    guidance_version text,

    -- Snapshot of what actually went in, recorded when the job is enqueued.
    -- Not a live count: the point is what this attempt used, and media attached
    -- afterwards belongs to a later attempt.
    photo_count   int         NOT NULL DEFAULT 0,
    video_count   int         NOT NULL DEFAULT 0,
    frame_count   int         NOT NULL DEFAULT 0,

    reconstruction_job_id uuid REFERENCES reconstruction_jobs(id) ON DELETE SET NULL,
    -- The splat this attempt produced, once it has one.
    result_media_id       uuid REFERENCES property_media(id) ON DELETE SET NULL,

    -- pending | running | succeeded | failed | superseded
    status        text        NOT NULL DEFAULT 'pending',
    failure_reason text,

    started_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,

    CONSTRAINT chk_capture_session_subject
        CHECK (lead_id IS NOT NULL OR listing_id IS NOT NULL),
    CONSTRAINT chk_capture_session_status
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'superseded'))
);

CREATE INDEX IF NOT EXISTS idx_capture_sessions_lead
    ON capture_sessions (lead_id, started_at DESC)
    WHERE lead_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_capture_sessions_listing
    ON capture_sessions (listing_id, started_at DESC)
    WHERE listing_id IS NOT NULL;

ALTER TABLE property_media
    ADD COLUMN IF NOT EXISTS capture_session_id uuid
        REFERENCES capture_sessions(id) ON DELETE SET NULL;

COMMENT ON COLUMN property_media.capture_session_id IS
    'The capture attempt this media was gathered for, when it was. NULL for '
    'media uploaded outside a session (ad-hoc photos, client-link uploads). '
    'Not a claim about which attempts consumed it.';

ALTER TABLE capture_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE capture_sessions FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'capture_sessions_tenant_isolation'
    ) THEN
        CREATE POLICY capture_sessions_tenant_isolation ON capture_sessions
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON capture_sessions TO oracle_app';
    END IF;
END $$;

COMMENT ON TABLE capture_sessions IS
    'One reconstruction attempt: the media it used, the job it produced and how '
    'it ended. Without this, "which photos produced this splat" is unanswerable '
    'and a re-capture cannot supersede a bad one.';

COMMIT;
