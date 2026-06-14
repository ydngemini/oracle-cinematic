-- 0014 — audit_ledger attribution columns become nullable.
--
-- The ledger's record() API has always treated tenant_id/user_id as Optional:
-- system-level events (unauthenticated mutations like /auth/login, startup
-- actions) genuinely have no tenant. The NOT NULL constraints from 0005 made
-- every such insert fail, silently diverting the whole chain to the SQLite
-- fallback. AuditMiddleware now attributes from the Bearer JWT when present;
-- NULL means "no authenticated principal", which is itself signal.

ALTER TABLE audit_ledger ALTER COLUMN tenant_id DROP NOT NULL;
ALTER TABLE audit_ledger ALTER COLUMN user_id   DROP NOT NULL;

-- Agent identities are TEXT everywhere else in the system (JWT sub,
-- user_profiles.user_id, interaction-log actors) — 'analyst_01' and
-- 'ydnop@ydnhft.com' are not UUIDs. 0005 typed this column uuid, which
-- rejected every attributed insert.
ALTER TABLE audit_ledger ALTER COLUMN user_id TYPE text USING user_id::text;
