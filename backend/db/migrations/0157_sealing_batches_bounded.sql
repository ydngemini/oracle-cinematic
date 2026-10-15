-- ---------------------------------------------------------------------------
-- 0157 — sealing batches really are bounded (audit A01 follow-up, prompt 17,
-- ledger L17-24).
--
-- seal_negotiation_transcripts and seal_user_interactions (0152, re-made in
-- 0153) and seal_interaction_logs (0154) promise "at most p_limit (<= 5,000)
-- rows a call" and pick them as
--     UPDATE t ... WHERE id IN (SELECT id FROM t WHERE ... ORDER BY id
--                               LIMIT p_limit FOR UPDATE SKIP LOCKED)
-- On small tables PostgreSQL plans that as a nested-loop semi join with the
-- locking subquery on the inner side, and re-runs it for every outer row:
-- each re-run skips the rows it has just locked and returns the next ones, so
-- the LIMIT bounds each run, not the statement. Measured (L17-24): the same
-- pattern in backfill_client_notes sealed 3 notes in a "batch of 1"
-- (test_client_note_sealing_live, order-dependent on table statistics).
-- Consequence: a call may change more rows than asked — a longer transaction
-- and a broken per-run bound, never a wrong value.
--
-- Fix: pick the rows in a MATERIALIZED CTE (a CTE with a locking clause is
-- evaluated exactly once), then UPDATE ... WHERE id IN (SELECT id FROM
-- picked). Bodies otherwise identical to the definitions they replace (taken
-- from pg_get_functiondef on a database built from every migration);
-- signatures unchanged, so the 0152/0154 EXECUTE grants stay.
--
-- Rollback: re-run the function definitions from 0153 (the two) and 0154
-- (seal_interaction_logs).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.seal_negotiation_transcripts(p_tenant uuid, p_key text, p_limit integer, p_unseal boolean DEFAULT false)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $fn$
DECLARE
    v_count integer := 0;
BEGIN
    IF NOT app_is_platform_admin() OR p_tenant IS DISTINCT FROM app_current_tenant() THEN
        RAISE EXCEPTION 'sealing runs only in the platform context of the row''s own tenant'
            USING ERRCODE = '42501';
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 5000 OR COALESCE(p_key, '') = '' THEN
        RAISE EXCEPTION 'a key and a batch size of 1..5000 are required' USING ERRCODE = '22023';
    END IF;
    IF p_unseal THEN
        WITH picked AS MATERIALIZED (
            SELECT id FROM negotiation_events
            WHERE tenant_id = p_tenant AND transcript_ciphertext IS NOT NULL
            ORDER BY id LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
        UPDATE negotiation_events
           SET transcript_excerpt = pgp_sym_decrypt(transcript_ciphertext, p_key),
               transcript_ciphertext = NULL
         WHERE id IN (SELECT id FROM picked);
    ELSE
        WITH picked AS MATERIALIZED (
            SELECT id FROM negotiation_events
            WHERE tenant_id = p_tenant AND transcript_ciphertext IS NULL
            AND COALESCE(transcript_excerpt, '') <> ''
            ORDER BY id LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
        UPDATE negotiation_events
           SET transcript_ciphertext = pgp_sym_encrypt(transcript_excerpt, p_key, 's2k-mode=1'),
               transcript_excerpt = NULL
         WHERE id IN (SELECT id FROM picked);
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$fn$;

CREATE OR REPLACE FUNCTION public.seal_user_interactions(p_tenant text, p_key text, p_limit integer, p_unseal boolean DEFAULT false)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $fn$
DECLARE
    v_count integer := 0;
BEGIN
    IF NOT app_is_platform_admin()
       OR lower(p_tenant) IS DISTINCT FROM lower(app_current_tenant()::text) THEN
        RAISE EXCEPTION 'sealing runs only in the platform context of the row''s own tenant'
            USING ERRCODE = '42501';
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 5000 OR COALESCE(p_key, '') = '' THEN
        RAISE EXCEPTION 'a key and a batch size of 1..5000 are required' USING ERRCODE = '22023';
    END IF;
    IF p_unseal THEN
        WITH picked AS MATERIALIZED (
            SELECT id FROM user_interactions
            WHERE tenant_id = p_tenant AND content_ciphertext IS NOT NULL
            ORDER BY id LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
        UPDATE user_interactions
           SET content = pgp_sym_decrypt(content_ciphertext, p_key),
               content_ciphertext = NULL
         WHERE id IN (SELECT id FROM picked);
    ELSE
        WITH picked AS MATERIALIZED (
            SELECT id FROM user_interactions
            WHERE tenant_id = p_tenant AND content_ciphertext IS NULL AND content <> ''
            ORDER BY id LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
        UPDATE user_interactions
           SET content_ciphertext = pgp_sym_encrypt(content, p_key, 's2k-mode=1'),
               content = ''
         WHERE id IN (SELECT id FROM picked);
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$fn$;

CREATE OR REPLACE FUNCTION public.seal_interaction_logs(p_tenant uuid, p_key text, p_limit integer, p_unseal boolean DEFAULT false)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $fn$
DECLARE
    v_keys  CONSTANT text[] := ARRAY['body','text','transcript','summary','extraction'];
    v_count integer := 0;
BEGIN
    IF NOT app_is_platform_admin() OR p_tenant IS DISTINCT FROM app_current_tenant() THEN
        RAISE EXCEPTION 'sealing runs only in the platform context of the row''s own tenant'
            USING ERRCODE = '42501';
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 5000 OR COALESCE(p_key, '') = '' THEN
        RAISE EXCEPTION 'a key and a batch size of 1..5000 are required' USING ERRCODE = '22023';
    END IF;
    IF p_unseal THEN
        WITH picked AS MATERIALIZED (
            SELECT id FROM interaction_logs
            WHERE tenant_id = p_tenant
            AND (subject_ciphertext IS NOT NULL OR content_ciphertext IS NOT NULL)
            ORDER BY id LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
        UPDATE interaction_logs
           SET subject = COALESCE(pgp_sym_decrypt(subject_ciphertext, p_key), subject),
               subject_ciphertext = NULL,
               payload = payload || COALESCE(pgp_sym_decrypt(content_ciphertext, p_key)::jsonb, '{}'::jsonb),
               content_ciphertext = NULL
         WHERE id IN (SELECT id FROM picked);
    ELSE
        WITH picked AS MATERIALIZED (
            SELECT id FROM interaction_logs
            WHERE tenant_id = p_tenant
            AND ((subject_ciphertext IS NULL AND COALESCE(subject, '') <> '')
            OR (content_ciphertext IS NULL AND payload ?| v_keys))
            ORDER BY id LIMIT p_limit
            FOR UPDATE SKIP LOCKED)
        UPDATE interaction_logs
           SET subject_ciphertext = COALESCE(subject_ciphertext,
                                             pgp_sym_encrypt(NULLIF(subject, ''), p_key, 's2k-mode=1')),
               subject = CASE WHEN COALESCE(subject, '') = '' THEN subject END,
               content_ciphertext = COALESCE(content_ciphertext, CASE WHEN payload ?| v_keys THEN
                   pgp_sym_encrypt((SELECT jsonb_object_agg(e.key, e.value)
                                      FROM jsonb_each(payload) e
                                     WHERE e.key = ANY (v_keys))::text, p_key, 's2k-mode=1') END),
               payload = payload - v_keys
         WHERE id IN (SELECT id FROM picked);
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$fn$;

