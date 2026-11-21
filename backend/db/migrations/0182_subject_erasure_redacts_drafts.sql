-- ---------------------------------------------------------------------------
-- 0182 — a data-subject deletion redacts the person's outbound drafts and
-- approvals, and every copy of them this database keeps, and keeps the
-- record that an action happened (ledger L17-38, external audit 29.8
-- follow-up; owner decision 2026-11-20).
--
-- Before: a deletion request deleted the person's clients and contacts, and
-- their drafts (command_executions), the approvals of those drafts
-- (action_approvals), the execute jobs carrying a copy of the approved draft
-- (automation_jobs, job_type 'command:execute', with their attempts' error
-- text) and the AI decision traces holding the whole draft (ai_decision_traces,
-- 0074) stayed in place with the message, the recipient's address or number
-- and every name in them, merely unlinked (ON DELETE SET NULL). Unlinked, the
-- 0142 policies treat drafts as pool drafts: every agent could read them.
--
-- Now privacy_erase_subject_rows also accepts command_executions,
-- action_approvals and automation_jobs, for the same running dsr_delete
-- operation, tenant and legal-hold checks as before, and for them it UPDATEs
-- instead of deleting:
--
--   replaced by the marker {"redacted": "data_subject_erasure", "at": <time>}
--     command_executions.target, .draft
--     action_approvals.draft_payload
--     automation_jobs.payload  (keeps command_id and command_type beside the
--                               marker: the worker reads them first)
--     automation_jobs.result   (keeps provider, reference and status)
--     ai_decision_traces.proposal, .final (final stays NULL when it was NULL)
--       of the redacted approvals; the trace is also withdrawn from the
--       training corpus (revoked_at, which dataset assembly filters on)
--   replaced by a fixed sentence when not NULL (free text may quote the
--   address or the message)
--     command_executions.last_error, .reconciliation_reason
--     action_approvals.reason
--     automation_jobs.status_message, .last_error
--     automation_job_attempts.error_detail (e.g. an SMTP refusal naming the address)
--   replaced by 'redacted'
--     action_approvals.target_id, unless target_type = 'command' (then it is
--     the command's own id). Approvals that are not a command's carry no
--     draft_payload.target, so they are never linked to a person and this
--     branch does not run in practice.
--   kept (the action skeleton)
--     ids, tenant_id, command_type / action_type / job_type, classification,
--     risk_class, target_type, state / status, approval_id, job_id,
--     idempotency_key, provider, provider_reference, provider_submitted_at,
--     scheduled_at, created_by, requested_by, decided_by, payload_hash, every
--     timestamp, reconcile_attempts / reconcile_checked_at, attempt counts and
--     error codes; for a trace: agent_id, surface, action_type, risk_class,
--     model_version, source_table / source_id, signal, the two digests,
--     decided_at, latency and outcome_*.
--     payload_hash and the trace digests are kept: they are the integrity
--     anchors of what was decided, and because the redacted payload no longer
--     matches payload_hash, no job can ever be built from a redacted approval.
--
-- A command's execute jobs are found by the command's id inside the job
-- payload under the command rows' locks, so a job queued between discovery and
-- the deletion (a release in that window) is redacted and cancelled too.
--
-- A draft that was never sent cannot be sent from a redacted copy, so it is
-- cancelled, like an offboarding (privacy_lifecycle._PENDING): a command in
-- draft / awaiting_approval / approved / queued becomes 'cancelled'; a
-- pending approval becomes 'revoked' by the person who asked for the
-- deletion; an execute job in draft / awaiting_approval / queued / failed
-- becomes 'cancelled' (a leased or running job is left to its worker).
-- Sent, failed, executing and reconciliation_required commands keep their
-- state. client_id / contact_id are re-derived by the 0142/0179 trigger from
-- the redacted target, so they become NULL.
--
-- ai_decision_traces is append-only by trigger for EVERY role (0074), so an
-- UPDATE of its proposal was impossible. Deleting and re-inserting a trace
-- would null the links that point at it (mission_actions.decision_trace_id,
-- 0100; the outcome attribution in 0098: ON DELETE SET NULL). Instead the
-- trigger now permits exactly one rewrite, when ALL of these hold:
--   * proposal becomes exactly {"redacted": "data_subject_erasure", "at": <a
--     timestamp string>} — no other key — and final likewise when it was not
--     NULL (a NULL final stays NULL); every other immutable column unchanged;
--   * the writer (current_user) owns the table, read from pg_catalog.pg_class
--     — i.e. owner-privileged code such as this SECURITY DEFINER function,
--     never oracle_app;
--   * the transaction carries a privacy operation (app.privacy_operation,
--     which privacy_erase_subject_rows sets before it writes).
-- The trigger function pins search_path = pg_catalog, pg_temp and qualifies
-- every catalog object, so a session that may create temp objects cannot
-- shadow pg_class (or a function) to pass the owner check. Everything else is
-- refused as before.
--
-- updated_at moves (set_updated_at trigger): it is the row's last-change
-- time, and the redaction is a change. A row already carrying the marker is
-- not touched again (a re-application after a restore counts only rows the
-- restore brought back).
--
-- The erasure_ledger row for these tables says 'anonymized' (the CHECK has
-- no 'redacted'), with detail {"mode": "redacted", ...counts by state}; the
-- request's receipt is built from those counts.
--
-- Not reached here: audit_ledger rows of approval decisions carry the
-- approver's reason in metadata.reason, but audit_ledger is hash-chained
-- (0148) and is never rewritten; the providers' own records; delivered
-- messages.
--
-- privacy_redact_execute_jobs is an internal helper: no EXECUTE for
-- oracle_app (PUBLIC revoked), called only from the SECURITY DEFINER
-- function as its owner. No function SET clause for app.* (managed
-- PostgreSQL, 0134). REVOKE/GRANT re-applied below.
--
-- Rollback: re-run 0122's privacy_erase_subject_rows (and its REVOKE/GRANT)
-- and 0074's ai_decision_traces_immutable; DROP FUNCTION
-- privacy_redact_execute_jobs(uuid, uuid[], uuid[], jsonb). Rows already
-- redacted stay redacted.
-- ---------------------------------------------------------------------------

-- ── the one rewrite an AI decision trace accepts ───────────────────────────
CREATE OR REPLACE FUNCTION ai_decision_traces_immutable()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_erasure boolean := false;
    v_marker  CONSTANT jsonb := '{"redacted": "data_subject_erasure"}'::jsonb;
BEGIN
    -- 0182: a data-subject erasure replaces the content with the exact marker.
    -- Only the table owner (owner-privileged code, never the app role), only
    -- inside a privacy operation. Nested IFs: the operators below are applied
    -- only to JSON objects. Operators and functions never resolve from
    -- pg_temp, and pg_catalog comes first, so nothing here can be shadowed.
    IF current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner)
                         FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)
       AND COALESCE(pg_catalog.current_setting('app.privacy_operation', true), '') <> ''
       AND pg_catalog.jsonb_typeof(NEW.proposal) = 'object'
       AND (NEW.final IS NULL) = (OLD.final IS NULL)
    THEN
        IF NEW.final IS NULL OR pg_catalog.jsonb_typeof(NEW.final) = 'object' THEN
            v_erasure := COALESCE(
                (NEW.proposal - 'at'::text) = v_marker
                AND pg_catalog.jsonb_typeof(NEW.proposal -> 'at') = 'string'
                AND (NEW.proposal ->> 'at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
                AND (NEW.final IS NULL OR (
                    (NEW.final - 'at'::text) = v_marker
                    AND pg_catalog.jsonb_typeof(NEW.final -> 'at') = 'string'
                    AND (NEW.final ->> 'at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T')),
                false);
        END IF;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.surface IS DISTINCT FROM OLD.surface
       OR NEW.action_type IS DISTINCT FROM OLD.action_type
       OR NEW.source_table IS DISTINCT FROM OLD.source_table
       OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR (NOT v_erasure AND NEW.proposal IS DISTINCT FROM OLD.proposal)
       OR NEW.proposal_sha256 IS DISTINCT FROM OLD.proposal_sha256
       OR (NOT v_erasure AND NEW.final IS DISTINCT FROM OLD.final)
       OR NEW.final_sha256 IS DISTINCT FROM OLD.final_sha256
       OR NEW.signal IS DISTINCT FROM OLD.signal
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
    THEN
        RAISE EXCEPTION
            'ai_decision_traces is append-only; only outcome_* and revoked_at may change';
    END IF;
    RETURN NEW;
END;
$$;

-- ── execute jobs (a copy of the approved draft) and their attempts ─────────
-- By job id and/or by the command id inside the payload. Returns
-- {"jobs": redacted, "jobs_cancelled": n, "attempt_errors": n}.
CREATE OR REPLACE FUNCTION privacy_redact_execute_jobs(p_tenant uuid, p_job_ids uuid[],
                                                       p_command_ids uuid[], p_marker jsonb)
RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
    v_all       uuid[];
    v_ids       uuid[];
    v_cancel    bigint;
    v_rows      bigint;
    v_attempts  bigint;
    v_cancelled CONSTANT text := 'Cancelled: the person it was for was deleted by a data-subject request. Nothing was sent.';
    v_redacted  CONSTANT text := 'Redacted: the person it was for was deleted by a data-subject request.';
BEGIN
    SELECT COALESCE(array_agg(s.id), '{}'),
           COALESCE(array_agg(s.id) FILTER (WHERE s.unredacted), '{}'),
           count(*) FILTER (WHERE s.unredacted AND s.state IN ('draft','awaiting_approval','queued','failed'))
      INTO v_all, v_ids, v_cancel
      FROM (SELECT id, state,
                   (payload ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure' AS unredacted
              FROM automation_jobs
             WHERE tenant_id = p_tenant AND job_type = 'command:execute'
               AND (id = ANY (COALESCE(p_job_ids, '{}'))
                    OR payload ->> 'command_id' = ANY (COALESCE(p_command_ids, '{}')::text[]))
               FOR UPDATE) s;
    UPDATE automation_jobs
       SET payload = p_marker || jsonb_strip_nulls(jsonb_build_object(
                         'command_id', payload -> 'command_id',
                         'command_type', payload -> 'command_type')),
           result = CASE WHEN result IS NULL THEN NULL
                         ELSE p_marker || jsonb_strip_nulls(jsonb_build_object(
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
     WHERE tenant_id = p_tenant AND id = ANY (v_ids);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    -- Every attempt of every matched job (a job redacted earlier may have run since).
    UPDATE automation_job_attempts
       SET error_detail = v_redacted
     WHERE tenant_id = p_tenant AND job_id = ANY (v_all)
       AND error_detail IS NOT NULL AND error_detail IS DISTINCT FROM v_redacted;
    GET DIAGNOSTICS v_attempts = ROW_COUNT;
    RETURN jsonb_build_object('jobs', v_rows, 'jobs_cancelled', v_cancel, 'attempt_errors', v_attempts);
END $$;
REVOKE ALL ON FUNCTION privacy_redact_execute_jobs(uuid, uuid[], uuid[], jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION privacy_erase_subject_rows(p_operation uuid, p_table text, p_ids uuid[])
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_tenant    uuid;
    v_subject   text;
    v_requester text;
    v_rows      bigint;
    v_marker    jsonb;
    v_detail    jsonb;
    v_jobs      jsonb;
    v_traces    bigint;
    v_counts    record;
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
            -- Lock the commands first: a release holds the command row while
            -- it queues a job, so after these locks no new job can appear.
            SELECT count(*) FILTER (WHERE s.state IN ('draft','awaiting_approval','approved','queued')) AS unsent,
                   count(*) FILTER (WHERE s.state = 'succeeded') AS sent,
                   count(*) FILTER (WHERE s.state = 'executing') AS in_flight,
                   count(*) FILTER (WHERE s.state = 'reconciliation_required') AS uncertain,
                   count(*) FILTER (WHERE s.state IN ('failed','cancelled')) AS not_sent
              INTO v_counts
              FROM (SELECT state FROM command_executions
                     WHERE tenant_id = v_tenant AND id = ANY (p_ids)
                       AND (draft ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure'
                       FOR UPDATE) s;
            -- Their execute jobs, including one queued after discovery.
            v_jobs := privacy_redact_execute_jobs(v_tenant, NULL, p_ids, v_marker);
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
            v_detail := jsonb_build_object('unsent_cancelled', v_counts.unsent, 'sent', v_counts.sent,
                                           'in_flight', v_counts.in_flight, 'uncertain', v_counts.uncertain,
                                           'not_sent', v_counts.not_sent, 'jobs', v_jobs);
        ELSIF p_table = 'action_approvals' THEN
            SELECT count(*) FILTER (WHERE s.status = 'pending') AS pending
              INTO v_counts
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
            -- The AI decision trace of each approval holds the whole draft.
            UPDATE ai_decision_traces
               SET proposal = v_marker,
                   final = CASE WHEN final IS NULL THEN NULL ELSE v_marker END,
                   revoked_at = COALESCE(revoked_at, now())
             WHERE tenant_id = v_tenant AND source_table = 'action_approvals' AND source_id = ANY (p_ids)
               AND (proposal ->> 'redacted') IS DISTINCT FROM 'data_subject_erasure';
            GET DIAGNOSTICS v_traces = ROW_COUNT;
            v_detail := jsonb_build_object('pending_revoked', v_counts.pending, 'decision_traces', v_traces);
        ELSE
            v_jobs := privacy_redact_execute_jobs(v_tenant, p_ids, NULL, v_marker);
            v_rows := (v_jobs ->> 'jobs')::bigint;
            v_detail := jsonb_build_object('jobs_cancelled', v_jobs -> 'jobs_cancelled',
                                           'attempt_errors', v_jobs -> 'attempt_errors');
        END IF;
        INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected, detail)
        VALUES (p_operation, v_tenant, 'subject', p_table, 'anonymized', v_rows,
                jsonb_build_object('mode', 'redacted') || v_detail);
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
    'redacts command_executions / action_approvals / command:execute automation_jobs (+ their attempts'' '
    'error text and the approvals'' ai_decision_traces) to the action skeleton '
    '(content replaced by {"redacted": "data_subject_erasure", "at": ...}; unsent ones cancelled).';
