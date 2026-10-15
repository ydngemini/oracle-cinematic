-- 0154: sealed text, slice 2 (audit A01 / ISO R01, ledger L17-22). EXPAND step only.
--
-- Same scheme and rollout as 0152 (L17-21): pgp_sym_encrypt(text, per-tenant
-- HKDF key, 's2k-mode=1'; crypto.py, 0153) into a new bytea column, the old
-- column left empty on a sealed row, every reader opening both shapes through
-- backend/sealed_text.open_rows. Each field below is read by id or by its
-- parent record and never searched, filtered or indexed by content (grep
-- census in L17-22):
--     email_outbox.subject              -> subject_ciphertext      (sealed: '')
--     email_outbox.body_text            -> body_text_ciphertext    (sealed: '')
--     interaction_logs.subject          -> subject_ciphertext      (sealed: NULL)
--     interaction_logs.payload content  -> content_ciphertext      (the keys
--         body, text, transcript, summary, extraction move into the sealed JSON
--         object; the behavioural keys stay in payload, unsealed)
--     ai_tool_operations.result         -> result_ciphertext       (sealed: NULL)
--     beliefs.source_quote              -> source_quote_ciphertext (sealed: NULL)
--     user_profiles.profile_summary     -> profile_summary_ciphertext (sealed: '')
-- email_outbox.body_html has no writer and no reader in the app; a sealed row
-- must keep it NULL, so a future writer has to seal it too.
--
-- The key never reaches the database at rest, so this file cannot encrypt the
-- existing rows: backend/backfill_sealed_text.py (operator) and the periodic
-- sealed_text_seal task (internal-only, also in recovery mode) do that in
-- bounded, restartable batches after the new code serves.
--
-- Deploy order (migrations apply BEFORE the new code serves): additive only.
-- The previous release keeps writing plaintext with a NULL ciphertext, which
-- every CHECK below accepts, and keeps reading its plaintext columns. NO
-- constraint here refuses plaintext; that is the later CONTRACT step.
--
-- interaction_logs is append-only to oracle_app (0008 revokes UPDATE), so its
-- old rows are sealed through seal_interaction_logs, a SECURITY DEFINER
-- function that can only move a row's own text between its plaintext and
-- sealed shapes (platform context of the row's own tenant, at most 5,000 rows
-- a call, SKIP LOCKED). The other four tables are already UPDATE-able by the
-- app, and the backfill updates them directly under RLS.
--
-- Retention: no retention or purge function touches these columns (erasure
-- deletes whole rows: privacy_erase_subject_rows, tenant erasure), so none
-- needs to learn the sealed column.
--
-- Rollback (code needs plaintext back): first
--   python backfill_sealed_text.py --unseal
-- then the old release reads its plaintext columns again; the new columns
-- may stay (NULL everywhere) or be dropped.

ALTER TABLE email_outbox       ADD COLUMN IF NOT EXISTS subject_ciphertext         bytea;
ALTER TABLE email_outbox       ADD COLUMN IF NOT EXISTS body_text_ciphertext       bytea;
ALTER TABLE interaction_logs   ADD COLUMN IF NOT EXISTS subject_ciphertext         bytea;
ALTER TABLE interaction_logs   ADD COLUMN IF NOT EXISTS content_ciphertext         bytea;
ALTER TABLE ai_tool_operations ADD COLUMN IF NOT EXISTS result_ciphertext          bytea;
ALTER TABLE beliefs            ADD COLUMN IF NOT EXISTS source_quote_ciphertext    bytea;
ALTER TABLE user_profiles      ADD COLUMN IF NOT EXISTS profile_summary_ciphertext bytea;

COMMENT ON COLUMN email_outbox.subject_ciphertext IS
    'pgp_sym_encrypt(subject, per-tenant HKDF key; crypto.py). When set, subject is ''''. '
    'NULL = a pre-0154 row the backfill has not sealed yet.';
COMMENT ON COLUMN email_outbox.body_text_ciphertext IS
    'pgp_sym_encrypt(body_text, per-tenant HKDF key; crypto.py). When set, body_text is '''' '
    'and body_html is NULL. NULL = a pre-0154 row the backfill has not sealed yet.';
COMMENT ON COLUMN interaction_logs.subject_ciphertext IS
    'pgp_sym_encrypt(subject, per-tenant HKDF key; crypto.py). When set, subject is NULL.';
COMMENT ON COLUMN interaction_logs.content_ciphertext IS
    'pgp_sym_encrypt(JSON object of the payload keys body/text/transcript/summary/extraction, '
    'per-tenant HKDF key; crypto.py). When set, payload holds none of those keys.';
COMMENT ON COLUMN ai_tool_operations.result_ciphertext IS
    'pgp_sym_encrypt(result jsonb as text, per-tenant HKDF key; crypto.py). When set, result is NULL.';
COMMENT ON COLUMN beliefs.source_quote_ciphertext IS
    'pgp_sym_encrypt(source_quote, per-tenant HKDF key; crypto.py). When set, source_quote is NULL.';
COMMENT ON COLUMN user_profiles.profile_summary_ciphertext IS
    'pgp_sym_encrypt(profile_summary, per-tenant HKDF key of tenant_id; crypto.py). When set, '
    'profile_summary is ''''.';

-- The new columns are NULL on every existing row, so each CHECK validates
-- trivially; the scan runs under the runner's lock_timeout (5 s).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'email_outbox_sealed_subject_empty') THEN
        ALTER TABLE email_outbox ADD CONSTRAINT email_outbox_sealed_subject_empty
            CHECK (subject_ciphertext IS NULL OR subject = '');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'email_outbox_sealed_body_empty') THEN
        ALTER TABLE email_outbox ADD CONSTRAINT email_outbox_sealed_body_empty
            CHECK (body_text_ciphertext IS NULL OR (body_text = '' AND body_html IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interaction_logs_sealed_subject_empty') THEN
        ALTER TABLE interaction_logs ADD CONSTRAINT interaction_logs_sealed_subject_empty
            CHECK (subject_ciphertext IS NULL OR COALESCE(subject, '') = '');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interaction_logs_sealed_content_empty') THEN
        ALTER TABLE interaction_logs ADD CONSTRAINT interaction_logs_sealed_content_empty
            CHECK (content_ciphertext IS NULL
                   OR NOT (payload ?| ARRAY['body','text','transcript','summary','extraction']));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tool_operations_sealed_result_empty') THEN
        ALTER TABLE ai_tool_operations ADD CONSTRAINT ai_tool_operations_sealed_result_empty
            CHECK (result_ciphertext IS NULL OR result IS NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'beliefs_sealed_quote_empty') THEN
        ALTER TABLE beliefs ADD CONSTRAINT beliefs_sealed_quote_empty
            CHECK (source_quote_ciphertext IS NULL OR COALESCE(source_quote, '') = '');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_profiles_sealed_summary_empty') THEN
        ALTER TABLE user_profiles ADD CONSTRAINT user_profiles_sealed_summary_empty
            CHECK (profile_summary_ciphertext IS NULL OR COALESCE(profile_summary, '') = '');
    END IF;
END $$;

-- ── one-way storage move for the append-only interaction log ───────────────
-- Moves a row's subject and its content keys into (or, for the rollback, out
-- of) the sealed columns. It cannot write new text: every value it stores is
-- the row's own, encrypted or decrypted under the caller's key.
CREATE OR REPLACE FUNCTION seal_interaction_logs(
    p_tenant uuid, p_key text, p_limit integer, p_unseal boolean DEFAULT false
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
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
        UPDATE interaction_logs
           SET subject = COALESCE(pgp_sym_decrypt(subject_ciphertext, p_key), subject),
               subject_ciphertext = NULL,
               payload = payload || COALESCE(pgp_sym_decrypt(content_ciphertext, p_key)::jsonb, '{}'::jsonb),
               content_ciphertext = NULL
         WHERE id IN (SELECT id FROM interaction_logs
                       WHERE tenant_id = p_tenant
                         AND (subject_ciphertext IS NOT NULL OR content_ciphertext IS NOT NULL)
                       ORDER BY id LIMIT p_limit
                       FOR UPDATE SKIP LOCKED);
    ELSE
        UPDATE interaction_logs
           SET subject_ciphertext = COALESCE(subject_ciphertext,
                                             pgp_sym_encrypt(NULLIF(subject, ''), p_key, 's2k-mode=1')),
               subject = CASE WHEN COALESCE(subject, '') = '' THEN subject END,
               content_ciphertext = COALESCE(content_ciphertext, CASE WHEN payload ?| v_keys THEN
                   pgp_sym_encrypt((SELECT jsonb_object_agg(e.key, e.value)
                                      FROM jsonb_each(payload) e
                                     WHERE e.key = ANY (v_keys))::text, p_key, 's2k-mode=1') END),
               payload = payload - v_keys
         WHERE id IN (SELECT id FROM interaction_logs
                       WHERE tenant_id = p_tenant
                         AND ((subject_ciphertext IS NULL AND COALESCE(subject, '') <> '')
                              OR (content_ciphertext IS NULL AND payload ?| v_keys))
                       ORDER BY id LIMIT p_limit
                       FOR UPDATE SKIP LOCKED);
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$$;

REVOKE ALL ON FUNCTION seal_interaction_logs(uuid, text, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION seal_interaction_logs(uuid, text, integer, boolean) TO oracle_app;
