-- 0091 — grant the extension functions the app actually calls
--
-- 0003 hardens the schema with:
--
--     REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
--
-- Extensions install into `public`, so that statement also revoked pgcrypto's
-- pgp_sym_* and earthdistance's ll_to_earth/earth_distance. Nothing ever
-- granted them back.
--
-- 0006 and 0008 grant oracle_encrypt/oracle_decrypt and look like they cover
-- it. They do not: those wrappers are `LANGUAGE sql STABLE`, NOT SECURITY
-- DEFINER, so they execute with the CALLER's privileges and hit exactly the
-- same wall. Granting a wrapper without granting what it calls buys nothing —
-- the same shape as 0050 (granted the table, not the function) and 0008
-- (granted four RLS helpers, missed app_current_agent).
--
-- Measured on the running stack as oracle_app_login before this migration:
--
--     SELECT pgp_sym_encrypt('x','k')     ERROR: permission denied
--     SELECT oracle_encrypt('x','k')      ERROR: permission denied for
--                                                function pgp_sym_encrypt
--     SELECT earth_distance(ll_to_earth(...), ll_to_earth(...))
--                                         ERROR: permission denied
--
-- What that breaks, all of it silently or misleadingly:
--
--   * ai_chat_store.py:40,44 encrypt and decrypt EVERY chat message body
--     directly with pgp_sym_*. No message can be written or read.
--   * commands_api.py stores and reads provider OAuth access/refresh tokens
--     and PKCE verifiers through crypto.encrypt_pii/decrypt_pii.
--   * state_compliance/routes_mls.py and routes_market.py do radius search
--     with earth_distance(ll_to_earth(...)).
--
-- And crypto.py turns the permission error into
-- "pgcrypto is not installed or pgp_sym_* functions are unavailable. Ensure
-- migration 0006 has been applied" — which sends the reader to an extension
-- that is installed and a migration that ran. That misdirection is why this
-- survived: the log names the wrong cause.
--
-- Granted to oracle_app, which oracle_app_login inherits (0003).
-- Idempotent; re-granting an existing privilege is a no-op.

-- pgcrypto — symmetric PII encryption.
GRANT EXECUTE ON FUNCTION pgp_sym_encrypt(text, text)         TO oracle_app;
GRANT EXECUTE ON FUNCTION pgp_sym_encrypt(text, text, text)   TO oracle_app;
GRANT EXECUTE ON FUNCTION pgp_sym_decrypt(bytea, text)        TO oracle_app;
GRANT EXECUTE ON FUNCTION pgp_sym_decrypt(bytea, text, text)  TO oracle_app;
GRANT EXECUTE ON FUNCTION pgp_sym_encrypt_bytea(bytea, text)  TO oracle_app;
GRANT EXECUTE ON FUNCTION pgp_sym_decrypt_bytea(bytea, text)  TO oracle_app;

-- earthdistance — radius search over lat/lng.
GRANT EXECUTE ON FUNCTION ll_to_earth(float8, float8)         TO oracle_app;
GRANT EXECUTE ON FUNCTION earth_distance(earth, earth)        TO oracle_app;
