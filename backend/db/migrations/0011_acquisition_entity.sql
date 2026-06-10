-- ===========================================================================
-- 0011_acquisition_entity.sql
-- Oracle — entity-of-record tracking per deal.
--
-- Institutional buyers take title in a per-deal LLC. The entity is formed
-- OUTSIDE this system (attorney, registered agent, or a service like Stripe
-- Atlas via its dashboard — Atlas has no formation API, verified against
-- Stripe docs 2026-06-10) and recorded here once it exists, so the legal
-- agent can inject the correct purchaser into the P&S package.
--
-- Shape: {"entity_name", "ein", "formation_state", "notes", "recorded_at"}
-- Column inherits the leads table's RLS policies and grants.
-- ===========================================================================

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS acquisition_entity jsonb;

COMMENT ON COLUMN leads.acquisition_entity IS
    'Entity of record taking title: {"entity_name","ein","formation_state","notes","recorded_at"} — written via PUT /api/leads/{id}/entity';
