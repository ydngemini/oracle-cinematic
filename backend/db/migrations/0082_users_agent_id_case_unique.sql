-- 0082 — one spelling per account, enforced by the database
--
-- users.agent_id has carried a case-SENSITIVE UNIQUE since 0001, while 0024
-- added a NON-unique functional index on lower(agent_id) and every lookup
-- resolves through it. So the database permitted "me@x.com" and "Me@x.com" as
-- two rows while the application treated them as one account, and
-- `_lookup_user`'s fetchrow would return an arbitrary one of them.
--
-- That ambiguity is what made the two-person role-override control bypassable:
-- login signed the typed spelling into the JWT, so one broker could request a
-- role change under one spelling and approve it under another. That is fixed in
-- the application (the token now carries the row's canonical agent_id), but the
-- fix relies on every insert path normalising. /auth/register does; nothing
-- forced it to, and nothing forces a future one to either.
--
-- This makes it structural. The unique index also closes the check-then-insert
-- race in register(), which could previously admit a duplicate under
-- concurrency and surface as a 500 rather than the intended 409.
--
-- Refuses rather than guesses: if a database already holds case-duplicates,
-- the DO block below fails with the offending values named, because silently
-- choosing a winner here would be choosing whose account survives.
--
-- Not CONCURRENTLY: run_migrations.py wraps each file in a transaction.

DO $$
DECLARE
    duplicates text;
BEGIN
    SELECT string_agg(DISTINCT lower(agent_id), ', ')
      INTO duplicates
      FROM users
     GROUP BY lower(agent_id)
    HAVING count(*) > 1;

    IF duplicates IS NOT NULL THEN
        RAISE EXCEPTION
            'users.agent_id holds case-duplicates (%): these are one account to '
            'the application and two rows to the database. Merge or rename them '
            'before applying 0082.', duplicates
            USING ERRCODE = '23505';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_agent_lower_unique
    ON users (lower(agent_id));

-- 0024's non-unique index is now redundant: the unique one serves every lookup
-- that used it, with the same expression.
DROP INDEX IF EXISTS idx_users_agent_lower;
