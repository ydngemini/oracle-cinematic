-- 0128: job attempts actually finalize.
--
-- finish_automation_job_attempt (0047) is called by the durable workers in the
-- PLATFORM context, but its UPDATE also required
-- `tenant_id = app_current_tenant()` — the worker's own (platform) tenant,
-- never the job's. So it matched no row, returned false for every attempt, and
-- automation_job_attempts kept finished_at/outcome/error_code NULL forever: on
-- staging every attempt of every job (found 2026-10-06, while tracing a call
-- whose first attempt's error was therefore unrecoverable). Duplicating half
-- of an RLS predicate hides rows from exactly the caller it was meant for.
--
-- The function is already platform-admin-only; (job_id, attempt_number)
-- identifies the attempt. Body otherwise unchanged; grants are kept by
-- CREATE OR REPLACE and restated.

BEGIN;

CREATE OR REPLACE FUNCTION finish_automation_job_attempt(
    p_job_id uuid,
    p_attempt_number integer,
    p_outcome text,
    p_error_code text DEFAULT NULL,
    p_error_detail text DEFAULT NULL
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    changed boolean := false;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'platform administrator context required'
            USING ERRCODE = '42501';
    END IF;
    IF p_attempt_number < 1 THEN
        RAISE EXCEPTION 'attempt number must be positive' USING ERRCODE = '22023';
    END IF;
    IF p_outcome NOT IN ('succeeded', 'failed', 'lease_lost', 'cancelled') THEN
        RAISE EXCEPTION 'invalid attempt outcome' USING ERRCODE = '22023';
    END IF;

    UPDATE automation_job_attempts
       SET finished_at = now(),
           outcome = p_outcome,
           error_code = NULLIF(left(COALESCE(p_error_code, ''), 120), ''),
           error_detail = NULLIF(left(COALESCE(p_error_detail, ''), 2000), '')
     WHERE job_id = p_job_id
       AND attempt_number = p_attempt_number
       AND outcome IS NULL
     RETURNING true INTO changed;

    RETURN COALESCE(changed, false);
END;
$$;

REVOKE ALL ON FUNCTION finish_automation_job_attempt(uuid, integer, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finish_automation_job_attempt(uuid, integer, text, text, text) TO oracle_app;

COMMIT;
