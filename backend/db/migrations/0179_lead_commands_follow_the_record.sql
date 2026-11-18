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
-- When a draft names both, the client named is linked, and a lead that
-- belongs to a different client is refused (here and in commands_api). An id
-- in any form PostgreSQL reads as a uuid (no hyphens, braces) links like the
-- canonical form; the application also stores ids canonically.
--
-- Rollback: re-run 0142's app_link_draft_target (client/contact only).
-- ---------------------------------------------------------------------------

-- NULL for anything that is not a uuid (no error): ids in the target are free
-- JSON until the application validates them.
CREATE OR REPLACE FUNCTION app_try_uuid(p text) RETURNS uuid
    LANGUAGE plpgsql IMMUTABLE
    SET search_path = pg_catalog AS
$$
BEGIN
    RETURN p::uuid;
EXCEPTION WHEN invalid_text_representation THEN
    RETURN NULL;
END
$$;
REVOKE ALL ON FUNCTION app_try_uuid(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_try_uuid(text) TO oracle_app;

CREATE OR REPLACE FUNCTION app_link_draft_target()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public AS
$$
DECLARE
    v_target jsonb;
    v_client uuid;
    v_contact uuid;
    v_lead uuid;
    v_seller uuid;
    v_prev text;
BEGIN
    IF TG_TABLE_NAME = 'command_executions' THEN
        v_target := NEW.target;
    ELSE
        v_target := NEW.draft_payload -> 'target';
    END IF;
    IF jsonb_typeof(v_target) = 'object' THEN
        v_client := app_try_uuid(v_target ->> 'client_id');
        v_contact := app_try_uuid(v_target ->> 'contact_id');
        v_lead := app_try_uuid(v_target ->> 'lead_id');
    END IF;
    NEW.client_id := NULL;
    NEW.contact_id := NULL;
    IF v_client IS NOT NULL OR v_contact IS NOT NULL OR v_lead IS NOT NULL THEN
        -- Whether the record EXISTS, not whether the caller may see it: the
        -- policies decide visibility from the link this sets.
        v_prev := COALESCE(current_setting('app.record_scope', true), '');
        PERFORM set_config('app.record_scope', 'brokerage', true);
        IF v_client IS NOT NULL THEN
            SELECT c.id INTO NEW.client_id FROM public.clients c
             WHERE c.id = v_client AND c.tenant_id = NEW.tenant_id;
        END IF;
        IF v_lead IS NOT NULL THEN
            -- 0179: a draft about a lead follows the lead's seller client
            -- (0143) unless a client of this brokerage is named; a named
            -- client must be that lead's own.
            SELECT l.seller_client_id INTO v_seller FROM public.leads l
             WHERE l.id = v_lead AND l.tenant_id = NEW.tenant_id;
            IF NEW.client_id IS NULL THEN
                NEW.client_id := v_seller;
            ELSIF v_seller IS NOT NULL AND v_seller <> NEW.client_id THEN
                PERFORM set_config('app.record_scope', v_prev, true);
                RAISE EXCEPTION 'draft names a lead of a different client'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        IF v_contact IS NOT NULL THEN
            SELECT ac.id INTO NEW.contact_id FROM public.agent_contacts ac
             WHERE ac.id = v_contact AND ac.tenant_id = NEW.tenant_id;
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
 WHERE jsonb_typeof(target) = 'object' AND target ? 'lead_id';
ALTER TABLE command_executions ENABLE TRIGGER trg_command_executions_updated;
UPDATE action_approvals SET client_id = NULL
 WHERE jsonb_typeof(draft_payload -> 'target') = 'object'
   AND (draft_payload -> 'target') ? 'lead_id';
