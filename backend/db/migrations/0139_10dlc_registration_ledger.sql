-- 0139: 10DLC brand and campaign registration go through the provider
-- side-effect ledger (provider_purchases, 0123), so a brokerage registers at
-- most one brand and one campaign at the provider.
--
-- Audit A07 follow-up / ledger L10-09 (docs/claude-prompts/LEDGER.md):
-- register_brand asked Telnyx for a NEW brand on every PUT and then overwrote
-- provider_brand_id, orphaning the earlier brand (each costs money at the
-- carrier); register_campaign submitted a new campaign on every call. Six
-- racing PUTs registered six brands, and six campaigns
-- (tests/test_messaging_registration_race_live.py, before this change).
--
-- Now the intent row is written under the brokerage's brand-row lock before
-- the provider is asked. A brand or campaign that already exists at the
-- provider is refused (409); an in-flight or `unknown` submission (timeout,
-- lost answer) blocks another until an operator resolves it; a `confirmed`
-- one that never reached the brand/campaign table is reused, not
-- re-submitted. reconciliation.py turns a stale `intended` into `unknown` for
-- every kind; its provider search and release stay kind = 'phone_number' only.
--
-- Only the kind CHECK widens (provider 'telnyx' is already allowed, 0136): no
-- new table, column or function, so no new grant (0123 grants SELECT, INSERT,
-- UPDATE on provider_purchases to oracle_app) and no new privacy-map entry
-- (provider_purchases is classified). Nothing here needs superuser.
--
-- Rollback (application code first, since it writes these values):
--   DELETE is not granted to the app; an operator first moves any
--   10dlc_brand / 10dlc_campaign rows out of the way, then re-creates the
--   0136 check: kind IN ('phone_number','hosted_messaging_order').

ALTER TABLE provider_purchases DROP CONSTRAINT IF EXISTS provider_purchases_kind_check;
ALTER TABLE provider_purchases
    ADD CONSTRAINT provider_purchases_kind_check
    CHECK (kind IN ('phone_number','hosted_messaging_order','10dlc_brand','10dlc_campaign'));
