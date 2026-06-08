-- ===========================================================================
-- 0006_pii_encryption.sql
-- Oracle — Column-level PII encryption via pgcrypto (Decision 009).
--
-- Layers added here:
--   1. pgcrypto extension  — pgp_sym_encrypt / pgp_sym_decrypt primitives
--   2. Encrypted columns   — leads.encrypted_payload, clients.encrypted_contact
--   3. Helper SQL functions — oracle_encrypt() / oracle_decrypt() wrapping
--                             pgcrypto with the per-tenant GUC key
--
-- KEY MODEL:
--   The encryption key is injected per-request as a session-local GUC:
--     SELECT set_config('app.encryption_key', '<tenant-derived-key>', true);
--   This mirrors the existing app.current_tenant / app.current_role pattern
--   (0001).  The key is derived outside the database (Auth Gatekeeper) from a
--   per-tenant secret stored in AWS Secrets Manager; the database never sees
--   the master secret.  At-rest column encryption therefore provides defence in
--   depth: an attacker who exfiltrates the raw database file or a cold snapshot
--   cannot decrypt PII without also compromising the key-derivation service.
--
-- WHAT IS ENCRYPTED:
--   leads.encrypted_payload   — full contact info blob (name, email, phone,
--                               mailing address) for motivated-seller leads.
--   clients.encrypted_contact — same set for brokerage clients.
--   Both columns replace (not duplicate) the plaintext fields over time; the
--   migration adds the columns without removing the originals to allow a safe
--   rolling backfill.  A follow-on migration drops the plaintext columns after
--   backfill is confirmed complete.
--
-- NOTES:
--   * pgcrypto is already loaded in 0001 (gen_random_uuid()) — CREATE EXTENSION
--     IF NOT EXISTS is idempotent and documents the dependency explicitly here.
--   * PGP symmetric encryption (pgp_sym_encrypt) uses AES-256 by default when
--     the key is >= 32 bytes.  The helper functions pass the raw GUC value;
--     key length enforcement is the caller's responsibility.
--   * oracle_decrypt returns NULL (not an error) when the GUC key is missing,
--     so unauthenticated reads surface NULL rather than raising an exception
--     that could leak timing information.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. pgcrypto — provides pgp_sym_encrypt, pgp_sym_decrypt, digest, gen_salt.
--    Already present from 0001 but declared here for explicit dependency audit.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 2. Encrypted payload columns.
--
--    BYTEA not TEXT because PGP output is binary; casting via base64 in the DB
--    wastes space and forces a double-copy on every read.  The application
--    layer handles any base64 serialisation needed for JSON transport.
-- ---------------------------------------------------------------------------
ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS encrypted_payload bytea;

COMMENT ON COLUMN leads.encrypted_payload IS
    'pgp_sym_encrypt(JSONB payload of sensitive contact info, app.encryption_key). '
    'Key is a per-tenant AES-256 secret injected via GUC at request start by the '
    'Auth Gatekeeper; never stored in the DB.  NULL = not yet backfilled or '
    'encryption key unavailable for this session.';

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS encrypted_contact bytea;

COMMENT ON COLUMN clients.encrypted_contact IS
    'pgp_sym_encrypt(JSONB of {full_name, email, phone, address}, app.encryption_key). '
    'Key is a per-tenant AES-256 secret injected via GUC at request start by the '
    'Auth Gatekeeper; never stored in the DB.  NULL = not yet backfilled or '
    'encryption key unavailable for this session.';

-- ---------------------------------------------------------------------------
-- 3. Helper functions.
--
--    oracle_encrypt — thin wrapper so application code does not hard-code the
--                     GUC name or pgcrypto call signature.
--    oracle_decrypt — returns NULL (not an error) when the GUC key is absent
--                     so an unauthenticated connection cannot infer key status
--                     from exception type.
--
--    Both are SECURITY INVOKER (default) — they run as the calling role, so
--    oracle_app's RLS policies still bind when the app calls them.  They are
--    STABLE (no side-effects, repeatable within transaction) which allows
--    the planner to inline / cache within a query.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION oracle_encrypt(plaintext text, key text)
RETURNS bytea
LANGUAGE sql STABLE AS
$$
    SELECT pgp_sym_encrypt(plaintext, key)
$$;

COMMENT ON FUNCTION oracle_encrypt(text, text) IS
    'Encrypts plaintext with AES-256 via pgp_sym_encrypt. '
    'Callers should pass current_setting(''app.encryption_key'', true) as key. '
    'The key is a per-tenant secret injected via GUC (app.encryption_key) by the '
    'Auth Gatekeeper before every request; it is never persisted in the database.';

CREATE OR REPLACE FUNCTION oracle_decrypt(ciphertext bytea, key text)
RETURNS text
LANGUAGE plpgsql STABLE AS
$$
BEGIN
    -- Guard: null ciphertext or null/empty key returns NULL silently.
    IF ciphertext IS NULL OR key IS NULL OR key = '' THEN
        RETURN NULL;
    END IF;
    RETURN pgp_sym_decrypt(ciphertext, key);
EXCEPTION
    -- Wrong key or corrupted ciphertext: surface NULL, not a stack trace.
    -- The caller must treat NULL as "access denied / key mismatch".
    WHEN OTHERS THEN
        RETURN NULL;
END
$$;

COMMENT ON FUNCTION oracle_decrypt(bytea, text) IS
    'Decrypts a pgp_sym_encrypt ciphertext. Returns NULL on wrong key or '
    'corrupted data (no exception raised — prevents timing-based key oracle). '
    'Callers should pass current_setting(''app.encryption_key'', true) as key. '
    'The key is a per-tenant secret injected via GUC (app.encryption_key) by the '
    'Auth Gatekeeper; it is NEVER stored in the database.';

-- ---------------------------------------------------------------------------
-- 4. Grants — oracle_app can call both helpers; PUBLIC cannot.
-- ---------------------------------------------------------------------------
REVOKE ALL ON FUNCTION oracle_encrypt(text, text)   FROM PUBLIC;
REVOKE ALL ON FUNCTION oracle_decrypt(bytea, text)  FROM PUBLIC;

GRANT  EXECUTE ON FUNCTION oracle_encrypt(text, text)  TO oracle_app;
GRANT  EXECUTE ON FUNCTION oracle_decrypt(bytea, text) TO oracle_app;

-- ---------------------------------------------------------------------------
-- 5. pgaudit reader coverage — log every SELECT on the two PII tables that
--    now carry encrypted columns (no-op if already granted in 0003, idempotent).
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgaudit_reader') THEN
        EXECUTE 'GRANT SELECT ON leads   TO pgaudit_reader';
        EXECUTE 'GRANT SELECT ON clients TO pgaudit_reader';
    END IF;
END $$;
