-- 0085 — let the app role actually call public_record_date_or_null()
--
-- 0050 created the function and granted the TABLE it feeds
-- (public_property_records) to oracle_app, but never granted the function
-- itself. A function's default ACL is EXECUTE to PUBLIC, so this would
-- normally not matter — except 0003's hardening revokes PUBLIC, leaving the
-- ACL as `postgres=X/postgres`: owner only.
--
-- The harvester upsert calls it inline (harvesters/base.py:716 and :907), so
-- every state whose harvest reaches the write step dies with
-- "permission denied for function public_record_date_or_null" after the fetch
-- has already been paid for. Observed live across SD, TX, UT, VT, WA and WY.
--
-- Granted to oracle_app, which oracle_app_login inherits (0003), matching how
-- 0008 grants resolve_portal_token/app_current_tenant/oracle_encrypt.
--
-- Idempotent. Re-granting an existing privilege is a no-op.

GRANT EXECUTE ON FUNCTION public_record_date_or_null(text) TO oracle_app;
