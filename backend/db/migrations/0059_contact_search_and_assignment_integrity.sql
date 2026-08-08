-- Complete, privacy-preserving CRM search and assignment integrity.
-- Names remain encrypted; only tenant-keyed HMAC word-prefix tokens are stored.

BEGIN;

ALTER TABLE agent_contacts
    ADD COLUMN IF NOT EXISTS name_search_tokens text[] NOT NULL DEFAULT ARRAY[]::text[];

ALTER TABLE agent_contacts
    DROP CONSTRAINT IF EXISTS agent_contacts_name_search_tokens_chk;
ALTER TABLE agent_contacts
    ADD CONSTRAINT agent_contacts_name_search_tokens_chk CHECK (
        array_position(name_search_tokens, NULL) IS NULL
        AND array_to_string(name_search_tokens, '') ~ '^([0-9a-f]{64})*$'
    );

CREATE INDEX IF NOT EXISTS idx_agent_contacts_name_search
    ON agent_contacts USING gin (name_search_tokens)
    WHERE deleted_at IS NULL;

-- Assignments are brokerage-local and can only target active users. The trigger
-- provides a database boundary for non-API writers while allowing null during
-- controlled imports.
CREATE OR REPLACE FUNCTION validate_agent_contact_assignee()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.assigned_agent_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM users u
         WHERE u.tenant_id=NEW.tenant_id
           AND lower(u.agent_id)=lower(NEW.assigned_agent_id)
           AND u.is_active=true
    ) THEN
        RAISE EXCEPTION 'assigned_agent_id must identify an active tenant user'
            USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_contacts_validate_assignee ON agent_contacts;
CREATE TRIGGER trg_agent_contacts_validate_assignee
    BEFORE INSERT OR UPDATE OF tenant_id,assigned_agent_id ON agent_contacts
    FOR EACH ROW EXECUTE FUNCTION validate_agent_contact_assignee();

COMMIT;
