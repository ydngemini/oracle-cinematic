-- 0152: message bodies, call transcript excerpts and memory-core chat turns
-- are sealed at rest (audit A01 / ISO R01, ledger L17-21). EXPAND step only.
--
-- Three plaintext columns hold what a person wrote or said, and nothing reads
-- them except by id or by their parent record (no content search):
--     sms_messages.body                      -> body_ciphertext
--     negotiation_events.transcript_excerpt  -> transcript_ciphertext
--     user_interactions.content              -> content_ciphertext
-- The app (backend/sealed_text.py) now writes the text only to the new bytea
-- column — pgp_sym_encrypt under the per-tenant HKDF key, the scheme of
-- crypto.py, client_notes (0132) and ai_chat_messages — and leaves the old
-- column empty (NULL; '' for user_interactions.content, which is NOT NULL).
-- The key never reaches the database at rest, so this file cannot encrypt the
-- existing rows: backend/backfill_sealed_text.py does that in bounded,
-- restartable batches (an operator script; staging runs ORACLE_RECOVERY_MODE,
-- so no periodic job would). Until it has run, readers open both shapes.
--
-- Deploy order (migrations apply BEFORE the new code serves): everything here
-- is additive. The previous release keeps writing plaintext with a NULL
-- ciphertext, which every constraint below accepts, and keeps reading its
-- plaintext column. NO constraint here refuses plaintext; that is the later
-- CONTRACT step (L17-21), after this code has served everywhere and the
-- backfill reports 0 remaining.
--
-- Guards added now:
--   * <table>_sealed_text_empty — a sealed row keeps no plaintext copy.
--   * seal_negotiation_transcripts / seal_user_interactions — oracle_app may
--     not UPDATE these two append-only ledgers (0009, 0027), so sealing old
--     rows goes through a SECURITY DEFINER function that can only move a
--     row's own text between the two columns (encrypt, or decrypt for the
--     rollback); it cannot write new text. Platform context of the row's own
--     tenant only, at most 5,000 rows a call, SKIP LOCKED.
--   * purge_expired_platform_data (0027) also clears a sealed transcript, or
--     the retention sweep would miss every row written from now on.
--
-- Rollback (code needs plaintext back): first
--   python backfill_sealed_text.py --unseal
-- then the old release reads its plaintext columns again; the new columns
-- may stay (NULL everywhere) or be dropped.

ALTER TABLE sms_messages       ADD COLUMN IF NOT EXISTS body_ciphertext       bytea;
ALTER TABLE negotiation_events ADD COLUMN IF NOT EXISTS transcript_ciphertext bytea;
ALTER TABLE user_interactions  ADD COLUMN IF NOT EXISTS content_ciphertext    bytea;

COMMENT ON COLUMN sms_messages.body_ciphertext IS
    'pgp_sym_encrypt(message text, per-tenant HKDF key; crypto.py). When set, body is NULL. '
    'NULL = a pre-0152 row whose plaintext body backfill_sealed_text.py has not sealed yet.';
COMMENT ON COLUMN negotiation_events.transcript_ciphertext IS
    'pgp_sym_encrypt(transcript excerpt, per-tenant HKDF key; crypto.py). When set, '
    'transcript_excerpt is NULL. NULL = no excerpt, or a pre-0152 plaintext row.';
COMMENT ON COLUMN user_interactions.content_ciphertext IS
    'pgp_sym_encrypt(chat turn, per-tenant HKDF key of tenant_id; crypto.py). When set, '
    'content is ''''. NULL = a pre-0152 row whose plaintext backfill_sealed_text.py has not sealed yet.';

-- The new columns are NULL on every existing row, so each CHECK validates
-- trivially; the scan runs under the runner's lock_timeout (5 s).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sms_messages_sealed_text_empty') THEN
        ALTER TABLE sms_messages ADD CONSTRAINT sms_messages_sealed_text_empty
            CHECK (body_ciphertext IS NULL OR COALESCE(body, '') = '');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'negotiation_events_sealed_text_empty') THEN
        ALTER TABLE negotiation_events ADD CONSTRAINT negotiation_events_sealed_text_empty
            CHECK (transcript_ciphertext IS NULL OR COALESCE(transcript_excerpt, '') = '');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_interactions_sealed_text_empty') THEN
        ALTER TABLE user_interactions ADD CONSTRAINT user_interactions_sealed_text_empty
            CHECK (content_ciphertext IS NULL OR content = '');
    END IF;
END $$;

-- ── one-way storage moves for the append-only ledgers ──────────────────────
CREATE OR REPLACE FUNCTION seal_negotiation_transcripts(
    p_tenant uuid, p_key text, p_limit integer, p_unseal boolean DEFAULT false
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
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
        UPDATE negotiation_events
           SET transcript_excerpt = pgp_sym_decrypt(transcript_ciphertext, p_key),
               transcript_ciphertext = NULL
         WHERE id IN (SELECT id FROM negotiation_events
                       WHERE tenant_id = p_tenant AND transcript_ciphertext IS NOT NULL
                       ORDER BY id LIMIT p_limit
                       FOR UPDATE SKIP LOCKED);
    ELSE
        UPDATE negotiation_events
           SET transcript_ciphertext = pgp_sym_encrypt(transcript_excerpt, p_key),
               transcript_excerpt = NULL
         WHERE id IN (SELECT id FROM negotiation_events
                       WHERE tenant_id = p_tenant AND transcript_ciphertext IS NULL
                         AND COALESCE(transcript_excerpt, '') <> ''
                       ORDER BY id LIMIT p_limit
                       FOR UPDATE SKIP LOCKED);
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$$;

-- user_interactions.tenant_id is text (0009). The caller passes it exactly as
-- stored; the guard compares it with the context's tenant as text.
CREATE OR REPLACE FUNCTION seal_user_interactions(
    p_tenant text, p_key text, p_limit integer, p_unseal boolean DEFAULT false
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
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
        UPDATE user_interactions
           SET content = pgp_sym_decrypt(content_ciphertext, p_key),
               content_ciphertext = NULL
         WHERE id IN (SELECT id FROM user_interactions
                       WHERE tenant_id = p_tenant AND content_ciphertext IS NOT NULL
                       ORDER BY id LIMIT p_limit
                       FOR UPDATE SKIP LOCKED);
    ELSE
        UPDATE user_interactions
           SET content_ciphertext = pgp_sym_encrypt(content, p_key),
               content = ''
         WHERE id IN (SELECT id FROM user_interactions
                       WHERE tenant_id = p_tenant AND content_ciphertext IS NULL AND content <> ''
                       ORDER BY id LIMIT p_limit
                       FOR UPDATE SKIP LOCKED);
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END
$$;

REVOKE ALL ON FUNCTION seal_negotiation_transcripts(uuid, text, integer, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION seal_user_interactions(text, text, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION seal_negotiation_transcripts(uuid, text, integer, boolean) TO oracle_app;
GRANT EXECUTE ON FUNCTION seal_user_interactions(text, text, integer, boolean) TO oracle_app;

-- ── retention also clears a sealed transcript (0027, otherwise unchanged) ──
CREATE OR REPLACE FUNCTION purge_expired_platform_data(
    p_raw_source_days integer,
    p_transcript_days integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    source_count integer := 0;
    transcript_count integer := 0;
    cache_count integer := 0;
BEGIN
    IF NOT app_is_platform_admin() THEN
        RAISE EXCEPTION 'platform administrator context required'
            USING ERRCODE = '42501';
    END IF;
    IF p_raw_source_days NOT BETWEEN 1 AND 3650
       OR p_transcript_days NOT BETWEEN 1 AND 3650 THEN
        RAISE EXCEPTION 'retention days must be between 1 and 3650'
            USING ERRCODE = '22023';
    END IF;

    UPDATE source_records
       SET raw_payload = jsonb_build_object(
               'retention_status', 'purged',
               'payload_sha256', payload_hash
           ),
           purged_at = now()
     WHERE purged_at IS NULL
       AND (
           (expires_at IS NOT NULL AND expires_at <= now())
           OR
           (expires_at IS NULL
            AND retrieved_at < now() - make_interval(days => p_raw_source_days))
       );
    GET DIAGNOSTICS source_count = ROW_COUNT;

    UPDATE negotiation_events
       SET transcript_excerpt = NULL,
           transcript_ciphertext = NULL,
           payload = (payload - 'transcript' - 'text' - 'utterance')
                     || '{"retention_status":"transcript_purged"}'::jsonb
     WHERE event_type = 'transcript'
       AND (transcript_excerpt IS NOT NULL OR transcript_ciphertext IS NOT NULL)
       AND created_at < now() - make_interval(days => p_transcript_days);
    GET DIAGNOSTICS transcript_count = ROW_COUNT;

    DELETE FROM di_cache
     WHERE COALESCE(stale_until, expires_at) IS NOT NULL
       AND COALESCE(stale_until, expires_at) < now();
    GET DIAGNOSTICS cache_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'source_payloads_purged', source_count,
        'transcripts_purged', transcript_count,
        'cache_rows_deleted', cache_count
    );
END;
$$;
REVOKE ALL ON FUNCTION purge_expired_platform_data(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_expired_platform_data(integer, integer) TO oracle_app;
