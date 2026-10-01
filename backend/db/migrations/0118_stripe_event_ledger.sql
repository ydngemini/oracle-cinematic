-- ---------------------------------------------------------------------------
-- 0118 — a Stripe event changes billing state at most once, and never backwards.
--
-- Stripe retries a webhook for up to three days, re-signing every delivery so
-- the 300 s signature tolerance always passes, and does not guarantee order.
-- The handlers stored no event identity and set status unconditionally, so a
-- retried checkout.session.completed or invoice.paid arriving after
-- customer.subscription.deleted put a cancelled brokerage back to 'active', and
-- an older subscription.updated could overwrite a newer one (review BILL-3).
--
-- stripe_webhook_events: one row per processed event id, written in the SAME
-- transaction as the event's effect — a delivery that fails rolls its row back
-- and Stripe's retry is processed; a delivery that succeeded makes every
-- replay a no-op.
-- subscriptions.last_event_created: the Stripe `created` time of the newest
-- event applied to the row; handlers only write when theirs is not older.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id      text PRIMARY KEY,
    event_type    text NOT NULL,
    event_created bigint NOT NULL,
    received_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stripe_webhook_events_id_shape CHECK (event_id ~ '^evt_[A-Za-z0-9]{1,250}$')
);

COMMENT ON TABLE stripe_webhook_events IS
    'Processed Stripe webhook event ids (0118): replay and duplicate-delivery defence.';

-- Platform-level, not tenant data: no tenant column, so no RLS. The app may
-- record and read event ids; nothing may rewrite or erase them.
-- Default privileges grant the app arwd on every new table, so the narrower
-- grant must also revoke explicitly (caught by tests/rls_security_review.sql).
REVOKE ALL ON stripe_webhook_events FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON stripe_webhook_events FROM oracle_app;
GRANT SELECT, INSERT ON stripe_webhook_events TO oracle_app;

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS last_event_created bigint NOT NULL DEFAULT 0;

COMMENT ON COLUMN subscriptions.last_event_created IS
    'Stripe created-time of the newest webhook event applied (0118); older events are ignored.';
