-- Runtime inserts into tables backed by identity/serial sequences must be able
-- to advance those sequences. RLS and table grants still govern the rows; this
-- grants no schema ownership and no table mutation privileges.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO oracle_app;

-- Keep future migration-created sequences usable by the inherited runtime
-- role without needing one-off grants for every serial/identity column.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO oracle_app;
