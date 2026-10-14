-- 0153: sealed text uses pgcrypto's single-pass S2K (s2k-mode=1).
--
-- Every key Neoh passes to pgp_sym_encrypt is a 32-byte random HKDF output
-- (crypto.derive_tenant_key), not a password, so the default iterated S2K
-- (mode 3) adds no strength and costs about 0.5 ms per row each way. Measured
-- on the local dev DB, 1,000 rows: decrypt 487 ms -> 9.8 ms, encrypt 537 ms
-- -> 61 ms. pgp_sym_decrypt reads the mode from each message, so rows sealed
-- before this migration still open, and the previous release opens rows this
-- one seals (no compatibility step).
--
-- The application SQL passes the same option (crypto.encrypt_pii,
-- ai_chat_store, the sealed-text writers and both backfill scripts). This
-- migration redefines the two 0152 one-way storage moves so the operator
-- backfill writes the same shape. Bodies are unchanged otherwise: same
-- signature, SECURITY DEFINER, search_path and grants (restated).

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
           SET transcript_ciphertext = pgp_sym_encrypt(transcript_excerpt, p_key, 's2k-mode=1'),
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
           SET content_ciphertext = pgp_sym_encrypt(content, p_key, 's2k-mode=1'),
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
