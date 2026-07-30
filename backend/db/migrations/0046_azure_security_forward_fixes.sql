-- Forward-only repair for Azure databases that already recorded migrations
-- 0036 and 0043 before their security corrections landed. Never rely on edits
-- to an applied migration filename: run_migrations.py intentionally skips it.

BEGIN;

-- Backfill the original uploader as the attachment owner, then enforce an
-- explicit per-agent boundary in addition to the tenant boundary.
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

DROP POLICY IF EXISTS ai_record_attachments_tenant_isolation
    ON ai_record_attachments;
CREATE POLICY ai_record_attachments_tenant_isolation
    ON ai_record_attachments
    USING (
        tenant_id = app_current_tenant()
        AND owner_agent_id = app_current_agent()
    )
    WITH CHECK (
        tenant_id = app_current_tenant()
        AND owner_agent_id = app_current_agent()
    );

-- The join table must not become an alternate route to another agent's bytes.
DROP POLICY IF EXISTS ai_chat_message_attachments_tenant_isolation
    ON ai_chat_message_attachments;
CREATE POLICY ai_chat_message_attachments_tenant_isolation
    ON ai_chat_message_attachments
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant()
        AND EXISTS (
            SELECT 1
              FROM ai_chat_messages message
             WHERE message.id = message_id
               AND message.user_id = app_current_agent()
        )
        AND EXISTS (
            SELECT 1
              FROM ai_record_attachments attachment
             WHERE attachment.id = attachment_id
               AND attachment.owner_agent_id = app_current_agent()
        )
    ))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant()
        AND EXISTS (
            SELECT 1
              FROM ai_chat_messages message
             WHERE message.id = message_id
               AND message.user_id = app_current_agent()
        )
        AND EXISTS (
            SELECT 1
              FROM ai_record_attachments attachment
             WHERE attachment.id = attachment_id
               AND attachment.owner_agent_id = app_current_agent()
        )
    ));

-- 0043 briefly shipped a destructive ACS-only constraint. Restore all runtime
-- providers, including Twilio, without deleting any surviving tenant rows.
ALTER TABLE provider_credentials
    DROP CONSTRAINT IF EXISTS provider_credentials_provider_check;
ALTER TABLE provider_credentials
    ADD CONSTRAINT provider_credentials_provider_check
    CHECK (provider IN ('google', 'twilio', 'acs', 'ses', 'runpod', 'mls'));

COMMIT;
