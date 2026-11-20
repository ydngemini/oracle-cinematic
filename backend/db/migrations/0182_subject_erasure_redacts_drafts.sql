-- ---------------------------------------------------------------------------
-- 0182 — a data-subject deletion redacts the person's outbound drafts and
-- approvals and keeps the record that an action happened (ledger L17-38,
-- external audit 29.8 follow-up; owner decision 2026-11-20).
--
-- Before: a deletion request deleted the person's clients and contacts, and
-- their drafts (command_executions), the approvals of those drafts
-- (action_approvals) and the execute jobs that carry a copy of the approved
-- draft (automation_jobs, job_type 'command:execute') stayed in place with
-- the message, the recipient's address or number and every name in them,
-- merely unlinked (ON DELETE SET NULL). Unlinked, the 0142 policies treat
-- them as pool drafts: every agent of the brokerage could read them.
--
-- Now privacy_erase_subject_rows also accepts those three tables, for the
-- same running dsr_delete operation, tenant and legal-hold checks as before,
-- and for them it UPDATEs instead of deleting:
--
--   replaced by the marker {"redacted": "data_subject_erasure", "at": <time>}
--     command_executions.target, .draft
--     action_approvals.draft_payload
--     automation_jobs.payload  (keeps command_id and command_type beside the
--                               marker: the worker reads them first)
--     automation_jobs.result   (keeps provider, reference and status)
--   replaced by a fixed sentence when not NULL (free text may quote the
--   address or the message)
--     command_executions.last_error, .reconciliation_reason
--     action_approvals.reason
--     automation_jobs.status_message, .last_error
--   replaced by 'redacted'
--     action_approvals.target_id, unless target_type = 'command' (then it is
--     the command's own id, part of the record)
--   kept (the action skeleton)
--     ids, tenant_id, command_type / action_type / job_type, classification,
--     risk_class, target_type, state / status, approval_id, job_id,
--     idempotency_key, provider, provider_reference, provider_submitted_at,
--     scheduled_at, created_by, requested_by, decided_by, payload_hash, every
--     timestamp, reconcile_attempts / reconcile_checked_at, attempt counts.
--     payload_hash is kept: it is the integrity anchor of what was approved,
--     and because the redacted payload no longer matches it, no job can ever
--     be built from a redacted approval.
--
-- A draft that was never sent cannot be sent from a redacted copy, so it is
-- cancelled, like an offboarding (privacy_lifecycle._PENDING): a command in
-- draft / awaiting_approval / approved / queued becomes 'cancelled'; a
-- pending approval becomes 'revoked' by the person who asked for the
-- deletion; an execute job in draft / awaiting_approval / queued / failed
-- becomes 'cancelled'. Sent, failed, executing and reconciliation_required
-- commands keep their state. client_id / contact_id are re-derived by the
-- 0142/0179 trigger from the redacted target, so they become NULL.
--
-- updated_at moves (set_updated_at trigger): it is the row's last-change
-- time, and the redaction is a change. A row already carrying the marker is
-- not touched again (a re-application after a restore counts only rows the
-- restore brought back).
--
-- The erasure_ledger row for these tables says 'anonymized' (the CHECK has
-- no 'redacted'), with detail {"mode": "redacted", "unsent_cancelled": n}.
--
-- No function SET clause for app.* (managed PostgreSQL, 0134): set_config in
-- the body, as before. REVOKE/GRANT re-applied below.
--
-- Rollback: re-run 0122's privacy_erase_subject_rows (and its REVOKE/GRANT).
-- Rows already redacted stay redacted.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION privacy_erase_subject_rows(p_operation uuid, p_table text, p_ids uuid[])
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant    uuid;
    v_subject   text;
    v_requester text;
    v_rows      bigint;
    v_unsent    bigint := 0;
    v_marker    jsonb;
    v_cancelled CONSTANT text := 'Cancelled: the person it was for was deleted by a data-subject request. Nothing was sent.';
    v_redacted  CONSTANT text := 'Redacted: the person it was for was deleted by a data-subject request.';
BEGIN
    SELECT tenant_id, subject_ref, requested_by INTO v_tenant, v_subject, v_requester FROM privacy_operations
     WHERE id = p_operation AND kind = 'dsr_delete' AND state = 'running';
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'no running deletion request %', p_operation USING ERRCODE = '42501';
    END IF;
    IF v_tenant IS DISTINCT FROM app_current_tenant() AND NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'operation belongs to another tenant' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM legal_holds WHERE tenant_id = v_tenant AND released_at IS NULL
                AND (scope = 'tenant' OR (scope = 'contact' AND subject_ref = v_subject))) THEN
        RAISE EXCEPTION 'subject is under legal hold' USING ERRCODE = 'P0001', HINT = 'legal_hold';
    END IF;

    -- Redacted, not deleted: the brokerage's record that an action happened.
    IF p_table IN ('command_executions', 'action_approvals', 'automation_jobs') THEN
        PERFORM set_config('app.privacy_operation', p_operation::text, true);
        v_marker := jsonb_build_object('redacted', 'data_subject_erasure', 'at', now());
        IF p_table = 'command_executions' THEN
            SELECT count(*) FILTER (WHERE s.state IN ('draft','awaiting_approval','approved','queued'))
              INTO v_unsent
              FROM (SELECT state FROM command_executions
                     WHERE tenant_id = v_tenant AND id = ANY (p_ids)
                       AND (draft ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure'
                       FOR UPDATE) s;
            UPDATE command_executions
               SET target = v_marker,
                   draft = v_marker,
                   state = CASE WHEN state IN ('draft','awaiting_approval','approved','queued')
                                THEN 'cancelled' ELSE state END,
                   last_error = CASE WHEN state IN ('draft','awaiting_approval','approved','queued')
                                     THEN v_cancelled
                                     WHEN last_error IS NOT NULL THEN v_redacted END,
                   reconciliation_reason = CASE WHEN reconciliation_reason IS NOT NULL THEN v_redacted END
             WHERE tenant_id = v_tenant AND id = ANY (p_ids)
               AND (draft ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure';
            GET DIAGNOSTICS v_rows = ROW_COUNT;
        ELSIF p_table = 'action_approvals' THEN
            SELECT count(*) FILTER (WHERE s.status = 'pending')
              INTO v_unsent
              FROM (SELECT status FROM action_approvals
                     WHERE tenant_id = v_tenant AND id = ANY (p_ids)
                       AND (draft_payload ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure'
                       FOR UPDATE) s;
            UPDATE action_approvals
               SET draft_payload = v_marker,
                   target_id = CASE WHEN target_type = 'command' THEN target_id ELSE 'redacted' END,
                   status = CASE WHEN status = 'pending' THEN 'revoked' ELSE status END,
                   decided_by = CASE WHEN status = 'pending' THEN v_requester ELSE decided_by END,
                   decided_at = CASE WHEN status = 'pending' THEN now() ELSE decided_at END,
                   reason = CASE WHEN status = 'pending' THEN v_cancelled
                                 WHEN reason IS NOT NULL THEN v_redacted END
             WHERE tenant_id = v_tenant AND id = ANY (p_ids)
               AND (draft_payload ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure';
            GET DIAGNOSTICS v_rows = ROW_COUNT;
        ELSE
            -- Only the execute jobs of commands (a copy of the approved draft).
            SELECT count(*) FILTER (WHERE s.state IN ('draft','awaiting_approval','queued','failed'))
              INTO v_unsent
              FROM (SELECT state FROM automation_jobs
                     WHERE tenant_id = v_tenant AND id = ANY (p_ids) AND job_type = 'command:execute'
                       AND (payload ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure'
                       FOR UPDATE) s;
            UPDATE automation_jobs
               SET payload = v_marker || jsonb_strip_nulls(jsonb_build_object(
                                 'command_id', payload -> 'command_id',
                                 'command_type', payload -> 'command_type')),
                   result = CASE WHEN result IS NULL THEN NULL
                                 ELSE v_marker || jsonb_strip_nulls(jsonb_build_object(
                                     'provider', result -> 'provider',
                                     'reference', result -> 'reference',
                                     'status', result -> 'status')) END,
                   state = CASE WHEN state IN ('draft','awaiting_approval','queued','failed')
                                THEN 'cancelled' ELSE state END,
                   next_retry_at = CASE WHEN state IN ('draft','awaiting_approval','queued','failed')
                                        THEN NULL ELSE next_retry_at END,
                   status_message = CASE WHEN state IN ('draft','awaiting_approval','queued','failed')
                                         THEN v_cancelled
                                         WHEN status_message IS NOT NULL THEN v_redacted END,
                   last_error = CASE WHEN last_error IS NOT NULL THEN v_redacted END
             WHERE tenant_id = v_tenant AND id = ANY (p_ids) AND job_type = 'command:execute'
               AND (payload ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure';
            GET DIAGNOSTICS v_rows = ROW_COUNT;
        END IF;
        INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected, detail)
        VALUES (p_operation, v_tenant, 'subject', p_table, 'anonymized', v_rows,
                jsonb_build_object('mode', 'redacted', 'unsent_cancelled', v_unsent));
        RETURN v_rows;
    END IF;

    IF NOT (p_table = ANY (ARRAY[
        'sms_messages','inbound_voice_calls','outreach_consent','outreach_suppression',
        'outreach_attempt_log','email_outbox','interaction_logs','client_notes',
        'client_tasks','client_activities','client_tags','buyer_profiles',
        'contact_nurture_jobs','contact_property_relationships','contact_intake_sessions',
        'intake_handoff_tasks','smart_plan_step_runs','smart_plan_enrollments',
        'agent_call_intents','lead_intake_events','clients','agent_contacts'])) THEN
        RAISE EXCEPTION 'table % is not erasable per subject', p_table USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.privacy_operation', p_operation::text, true);
    EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1 AND id = ANY ($2)', p_table)
      USING v_tenant, p_ids;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected)
    VALUES (p_operation, v_tenant, 'subject', p_table, 'deleted', v_rows);
    RETURN v_rows;
END $$;
REVOKE ALL ON FUNCTION privacy_erase_subject_rows(uuid, text, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION privacy_erase_subject_rows(uuid, text, uuid[]) TO oracle_app;
COMMENT ON FUNCTION privacy_erase_subject_rows(uuid, text, uuid[]) IS
    '0122/0182: one person''s rows for a running dsr_delete operation. Deletes from the allow-listed tables; '
    'redacts command_executions / action_approvals / command:execute automation_jobs to the action skeleton '
    '(content replaced by {"redacted": "data_subject_erasure", "at": ...}; unsent ones cancelled).';
