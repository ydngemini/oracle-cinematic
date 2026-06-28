-- 0024_user_auth.sql — DB-backed credentials + self-serve signup.
-- The users table (0001) had agent_id/role/is_active but no password material, so
-- auth ran off an in-memory demo dict + a single env operator account. Add scrypt
-- password hashes + display fields so login reads from the DB and brokers can
-- self-register (auth.py POST /auth/register creates a tenant + a broker_owner
-- user). agent_id stays the login handle (= JWT sub = the normalized email).
-- Idempotent.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email         text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name     text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS company       text;

-- Case-insensitive login lookups (we normalize email -> lower() in the app, and
-- agent_id is already UNIQUE, but the functional index keeps the lookup fast).
CREATE INDEX IF NOT EXISTS idx_users_agent_lower ON users (lower(agent_id));
