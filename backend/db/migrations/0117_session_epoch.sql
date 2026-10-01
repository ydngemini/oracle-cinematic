-- ---------------------------------------------------------------------------
-- 0117 — sessions end when the account says so, not when the JWT expires.
--
-- A session token was checked only for signature and expiry. Changing or
-- resetting a password, demoting a broker or removing an agent left every
-- token already issued working for the rest of its 24 hours — and
-- GET /auth/policy-acceptance re-minted a fresh one from the old claims, so
-- "the rest of its 24 hours" was in practice forever (security review
-- AUTH-1/AUTH-2, 2026-10-01).
--
-- users.session_epoch is signed into every token. Bumping it ends every
-- session the account has. app_begin_session() is what tenancy.apply_rls_context
-- now runs at the start of every tenant transaction: it sets the same three
-- GUCs set_config() always did and, for a request-derived identity, refuses
-- the transaction unless the account still exists in that tenant, is active,
-- holds the token's role, and is on the token's epoch. No extra round trip:
-- the check rides the statement that was already there.
--
-- SECURITY INVOKER on purpose. The GUCs are set before the lookup, so the
-- users policy (admin OR tenant_id = app_current_tenant()) already admits the
-- caller's own row; a definer function would be one more privileged surface.
-- The lookup is by primary key (tokens carry users.id as "uid") because an
-- expression index on lower(agent_id) cannot be used under FORCE RLS (lower()
-- is not leakproof); the agent-id branch exists only for tokens minted before this
-- migration and disappears from traffic within one token lifetime.
-- ---------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN IF NOT EXISTS session_epoch integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN users.session_epoch IS
    'Signed into every session token; incrementing it ends all of this account''s sessions (0117).';

CREATE OR REPLACE FUNCTION app_begin_session(
    p_tenant text,
    p_role   text,
    p_agent  text,
    p_check  boolean,
    p_user   uuid,
    p_epoch  integer
) RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM set_config('app.current_tenant', p_tenant, true),
            set_config('app.current_role',   p_role,   true),
            set_config('app.current_agent',  p_agent,  true);

    IF NOT p_check THEN
        RETURN;
    END IF;

    IF p_user IS NOT NULL THEN
        PERFORM 1 FROM public.users u
         WHERE u.id = p_user
           AND u.tenant_id = p_tenant::uuid
           AND u.role = p_role
           AND u.is_active
           AND u.session_epoch = p_epoch;
    ELSE
        PERFORM 1 FROM public.users u
         WHERE lower(u.agent_id) = lower(p_agent)
           AND u.tenant_id = p_tenant::uuid
           AND u.role = p_role
           AND u.is_active
           AND u.session_epoch = p_epoch;
    END IF;

    IF NOT FOUND THEN
        -- 28000 invalid_authorization_specification: mapped to 401 by the API.
        RAISE EXCEPTION 'session is no longer valid' USING ERRCODE = '28000';
    END IF;
END
$$;

-- 0003 revoked EXECUTE from PUBLIC on every function; grant explicitly.
REVOKE ALL ON FUNCTION app_begin_session(text, text, text, boolean, uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_begin_session(text, text, text, boolean, uuid, integer) TO oracle_app;
