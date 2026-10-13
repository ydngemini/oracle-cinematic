-- 0136: the hosted-messaging order goes through the provider side-effect
-- ledger (provider_purchases, 0123), so it is submitted at most once per route.
--
-- Audit A07 / ledger L10-06 (docs/claude-prompts/LEDGER.md): connect_messaging
-- read the route, saw no hosted_order_id and asked the provider for an order.
-- Six racing connects for one agent submitted six Telnyx hosted orders, and
-- each sent an ownership-verification text (live test,
-- tests/test_messaging_route_race_live.py, before this change). A crash after
-- the provider accepted an order and before the route was updated lost the
-- order id, so the next attempt submitted another.
--
-- Now the intent row is written before the provider is asked. The existing
-- partial unique index uq_provider_purchases_in_flight (tenant, agent,
-- provider, kind WHERE state IN intended/unknown/confirmed) allows one in
-- flight per route. An `unknown` outcome (timeout, lost answer) blocks a new
-- submission until an operator resolves it; a `confirmed` order that never
-- reached the route is reused, not re-submitted. reconciliation.py turns a
-- stale `intended` into `unknown` for every kind, but its provider search and
-- release run for kind = 'phone_number' only (same commit).
--
-- Only the two CHECK constraints widen: no new table, column or function, so
-- no new grant (0123 grants SELECT, INSERT, UPDATE on provider_purchases to
-- oracle_app) and no new privacy-map entry (provider_purchases is classified).
--
-- Rollback (application code first, since it writes these values):
--   DELETE is not granted to the app; an operator first moves any telnyx /
--   hosted_messaging_order rows out of the way, then re-creates the 0123
--   checks: provider IN ('twilio','plivo') and kind IN ('phone_number').

ALTER TABLE provider_purchases DROP CONSTRAINT IF EXISTS provider_purchases_provider_check;
ALTER TABLE provider_purchases
    ADD CONSTRAINT provider_purchases_provider_check
    CHECK (provider IN ('twilio','plivo','telnyx'));

ALTER TABLE provider_purchases DROP CONSTRAINT IF EXISTS provider_purchases_kind_check;
ALTER TABLE provider_purchases
    ADD CONSTRAINT provider_purchases_kind_check
    CHECK (kind IN ('phone_number','hosted_messaging_order'));
