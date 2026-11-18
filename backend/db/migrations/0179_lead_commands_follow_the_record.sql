-- ---------------------------------------------------------------------------
-- 0179 — an outbound command about a lead follows that lead's record (external
-- audit 26.3).
--
-- 0142 linked a draft (command_executions.target, action_approvals'
-- draft_payload->target) to its client and contact only. An SMS or CALL may
-- name only a lead_id; such a draft linked nothing, so the 0142 policies saw
-- it as a pool draft and the execution-time recheck (_requester_may_contact)
-- passed it unconditionally — a colleague's private lead could be texted.
--
-- 0143 already decides who may see a lead: whoever may see its seller client
-- (leads.seller_client_id; NULL = a public/pool lead, visible to every agent).
-- So a draft that names a lead and no client now links that lead's seller
-- client, and every 0142 rule (read, write, worker recheck) applies as is.
-- When a draft names both, the client named is linked; the application
-- refuses a lead that belongs to a different client (commands_api).
--
-- Rollback: re-run 0142's app_link_draft_target (client/contact only).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app_link_draft_target()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_target jsonb;
    v_client text;
    v_contact text;
    v_lead text;
    v_prev text;
    uuid_re constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF TG_TABLE_NAME = 'command_executions' THEN
        v_target := NEW.target;
    ELSE
        v_target := NEW.draft_payload -> 'target';
    END IF;
    IF jsonb_typeof(v_target) = 'object' THEN
        v_client := lower(v_target ->> 'client_id');
        v_contact := lower(v_target ->> 'contact_id');
        v_lead := lower(v_target ->> 'lead_id');
    END IF;
    NEW.client_id := NULL;
    NEW.contact_id := NULL;
    IF (v_client ~ uuid_re) OR (v_contact ~ uuid_re) OR (v_lead ~ uuid_re) THEN
        -- Whether the record EXISTS, not whether the caller may see it: the
        -- policies decide visibility from the link this sets.
        v_prev := COALESCE(current_setting('app.record_scope', true), '');
        PERFORM set_config('app.record_scope', 'brokerage', true);
        IF v_client ~ uuid_re THEN
            SELECT c.id INTO NEW.client_id FROM public.clients c
             WHERE c.id = v_client::uuid AND c.tenant_id = NEW.tenant_id;
        ELSIF v_lead ~ uuid_re THEN
            -- 0179: a lead-only draft follows the lead's seller client (0143).
            SELECT l.seller_client_id INTO NEW.client_id FROM public.leads l
             WHERE l.id = v_lead::uuid AND l.tenant_id = NEW.tenant_id;
        END IF;
        IF v_contact ~ uuid_re THEN
            SELECT ac.id INTO NEW.contact_id FROM public.agent_contacts ac
             WHERE ac.id = v_contact::uuid AND ac.tenant_id = NEW.tenant_id;
        END IF;
        PERFORM set_config('app.record_scope', v_prev, true);
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app_link_draft_target() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_link_draft_target() TO oracle_app;
COMMENT ON FUNCTION app_link_draft_target() IS
    '0142/0179: sets client_id/contact_id from the draft target; a lead-only target links the lead''s seller client. Returns no data to the caller.';

-- Re-link existing lead-only drafts through the trigger, without touching
-- updated_at (reconciliation reads it for executing rows).
ALTER TABLE command_executions DISABLE TRIGGER trg_command_executions_updated;
UPDATE command_executions SET client_id = NULL
 WHERE jsonb_typeof(target) = 'object' AND target ? 'lead_id' AND NOT target ? 'client_id';
ALTER TABLE command_executions ENABLE TRIGGER trg_command_executions_updated;
UPDATE action_approvals SET client_id = NULL
 WHERE jsonb_typeof(draft_payload -> 'target') = 'object'
   AND (draft_payload -> 'target') ? 'lead_id' AND NOT (draft_payload -> 'target') ? 'client_id';
