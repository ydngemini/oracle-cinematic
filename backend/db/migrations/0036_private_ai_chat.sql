-- 0036_private_ai_chat.sql
--
-- Durable, agent-private AI chat. Message bodies, attachment bytes, extracted
-- document text, and action snapshots are encrypted with the tenant key. RLS
-- remains the outer boundary; user_id adds the per-agent boundary within a
-- brokerage.

ALTER TABLE automation_jobs
    DROP CONSTRAINT IF EXISTS automation_jobs_risk_class_check;
ALTER TABLE automation_jobs
    ADD CONSTRAINT automation_jobs_risk_class_check
    CHECK (risk_class IN ('read_only','internal_edit','outreach','live_call','calendar_write',
                          'financial','bidding_message','legal_document','role_override'));

CREATE OR REPLACE FUNCTION app_current_agent() RETURNS text
    LANGUAGE sql STABLE AS
$$ SELECT COALESCE(NULLIF(current_setting('app.current_agent', true), ''), '') $$;

CREATE TABLE IF NOT EXISTS ai_chat_messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             text NOT NULL,
    role                text NOT NULL CHECK (role IN ('user','assistant','system')),
    content_ciphertext  bytea NOT NULL,
    status              text NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('pending','streaming','completed','failed')),
    request_id          uuid NOT NULL,
    context_type        text CHECK (context_type IS NULL OR context_type IN
                            ('client','lead','listing','contract')),
    context_id          uuid,
    model_id            text,
    error_code          text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, user_id, request_id, role),
    CHECK ((context_type IS NULL) = (context_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_agent_time
    ON ai_chat_messages (tenant_id, user_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS ai_record_attachments (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_agent_id             text NOT NULL DEFAULT app_current_agent(),
    record_type                text NOT NULL CHECK (record_type IN
                                   ('client','lead','listing','contract')),
    record_id                  uuid NOT NULL,
    filename                   text NOT NULL,
    media_type                 text NOT NULL CHECK (media_type IN
                                   ('application/pdf','image/jpeg','image/png','image/webp')),
    byte_size                  integer NOT NULL CHECK (byte_size > 0 AND byte_size <= 12582912),
    sha256                     char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes_ciphertext           bytea NOT NULL,
    extracted_text_ciphertext  bytea,
    scan_status                text NOT NULL CHECK (scan_status IN ('clean','unavailable_dev')),
    created_by                 text NOT NULL,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    deleted_at                 timestamptz
);

-- ``CREATE TABLE IF NOT EXISTS`` does not add columns on an already-migrated
-- database. Backfill from the authenticated uploader captured by the original
-- schema before making ownership mandatory.
ALTER TABLE ai_record_attachments
    ADD COLUMN IF NOT EXISTS owner_agent_id text;
UPDATE ai_record_attachments
   SET owner_agent_id = created_by
 WHERE owner_agent_id IS NULL OR owner_agent_id = '';
ALTER TABLE ai_record_attachments
    ALTER COLUMN owner_agent_id SET DEFAULT app_current_agent(),
    ALTER COLUMN owner_agent_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_record_attachments_owner_record
    ON ai_record_attachments
       (tenant_id, owner_agent_id, record_type, record_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS ai_chat_message_attachments (
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    message_id     uuid NOT NULL REFERENCES ai_chat_messages(id) ON DELETE CASCADE,
    attachment_id  uuid NOT NULL REFERENCES ai_record_attachments(id),
    PRIMARY KEY (message_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_message_attachments_tenant
    ON ai_chat_message_attachments (tenant_id, message_id);

CREATE TABLE IF NOT EXISTS ai_chat_actions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id               text NOT NULL,
    message_id            uuid NOT NULL REFERENCES ai_chat_messages(id) ON DELETE CASCADE,
    action_type           text NOT NULL,
    record_type           text NOT NULL CHECK (record_type IN ('client','lead','listing')),
    record_id             uuid NOT NULL,
    before_ciphertext     bytea NOT NULL,
    after_ciphertext      bytea NOT NULL,
    expected_updated_at   timestamptz,
    status                text NOT NULL DEFAULT 'applied'
                              CHECK (status IN ('applied','undone','conflict')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    undone_at             timestamptz,
    undo_expires_at       timestamptz NOT NULL DEFAULT (now() + interval '24 hours')
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_actions_agent_time
    ON ai_chat_actions (tenant_id, user_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_ai_chat_messages_updated ON ai_chat_messages;
CREATE TRIGGER trg_ai_chat_messages_updated
    BEFORE UPDATE ON ai_chat_messages
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE ai_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_chat_messages FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_record_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_record_attachments FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_chat_message_attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_chat_message_attachments FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_chat_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_chat_actions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ai_chat_messages_tenant_isolation ON ai_chat_messages;
CREATE POLICY ai_chat_messages_tenant_isolation ON ai_chat_messages
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()
    ))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()
    ));

DROP POLICY IF EXISTS ai_record_attachments_tenant_isolation ON ai_record_attachments;
CREATE POLICY ai_record_attachments_tenant_isolation ON ai_record_attachments
    USING (
        tenant_id = app_current_tenant()
        AND owner_agent_id = app_current_agent()
    )
    WITH CHECK (
        tenant_id = app_current_tenant()
        AND owner_agent_id = app_current_agent()
    );

DROP POLICY IF EXISTS ai_chat_message_attachments_tenant_isolation ON ai_chat_message_attachments;
CREATE POLICY ai_chat_message_attachments_tenant_isolation ON ai_chat_message_attachments
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant()
        AND EXISTS (
            SELECT 1 FROM ai_chat_messages message
             WHERE message.id = message_id AND message.user_id = app_current_agent()
        )
        AND EXISTS (
            SELECT 1 FROM ai_record_attachments attachment
             WHERE attachment.id = attachment_id
               AND attachment.owner_agent_id = app_current_agent()
        )
    ))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant()
        AND EXISTS (
            SELECT 1 FROM ai_chat_messages message
             WHERE message.id = message_id AND message.user_id = app_current_agent()
        )
        AND EXISTS (
            SELECT 1 FROM ai_record_attachments attachment
             WHERE attachment.id = attachment_id
               AND attachment.owner_agent_id = app_current_agent()
        )
    ));

DROP POLICY IF EXISTS ai_chat_actions_tenant_isolation ON ai_chat_actions;
CREATE POLICY ai_chat_actions_tenant_isolation ON ai_chat_actions
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()
    ))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()
    ));

GRANT SELECT, INSERT, UPDATE ON ai_chat_messages TO oracle_app;
GRANT SELECT, INSERT, UPDATE ON ai_record_attachments TO oracle_app;
GRANT SELECT, INSERT, DELETE ON ai_chat_message_attachments TO oracle_app;
GRANT SELECT, INSERT, UPDATE ON ai_chat_actions TO oracle_app;
