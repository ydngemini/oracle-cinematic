-- 0042 — Seed platform operator Memory Core profile.
-- The platform admin user (ydnop@ydnhft.com) must have a user_profiles row
-- so the Memory Core reports restored=true in the SESSION_RESTORED WS frame.
-- Migration 0009 created the tables but no rows were seeded; ignite_memory.py
-- was not run in the Azure deployment (DB is private-access only).
--
-- DEPENDENCY: Requires migration 0009 (user_profiles table creation).
-- Migration runner sorts by filename (0042 > 0009) so order is guaranteed.

INSERT INTO user_profiles (user_id, tenant_id, target_mao_pct, target_markets)
     VALUES ('ydnop@ydnhft.com',
             '00000000-0000-0000-0000-000000000000',
             0.70,
             '["New Castle","Kent"]'::jsonb)
ON CONFLICT (user_id) DO NOTHING;
