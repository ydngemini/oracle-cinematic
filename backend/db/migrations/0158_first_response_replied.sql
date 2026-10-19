-- ---------------------------------------------------------------------------
-- 0158 — a lead's reply is recorded on its first response (prompt 04, T04.07,
-- ledger L04-03).
--
-- 0133 kept staged, approved, accepted and delivered apart; the last stage the
-- product promises, "responded" (the lead answered), was not stored anywhere
-- (L04-01 next_task), and routing_metrics had no honest way to report it (its
-- old `responded` key counted staged drafts and was removed in 0133).
--
-- A reply is recorded as a timestamp and its evidence, NOT as a new
-- disposition: a reply comes on top of a delivery, so the row stays
-- 'delivered' (or 'accepted' where no receipt exists) — the delivered count,
-- and lead_engaged metering, which happens at delivery and re-reads
-- disposition = 'delivered', are untouched.
--
--   replied_at            when the first inbound text from the same client or
--                         contact arrived, at or after the response was
--                         accepted and within outreach_lifecycle.REPLY_WINDOW
--   reply_sms_message_id  that inbound text (the evidence); SET NULL if the
--                         message is erased, replied_at stays (a fact, no PII)
--
-- Written once (COALESCE), by outreach_lifecycle.record_reply in the platform
-- context of the message's own tenant, from messaging_data's inbound path.
-- An opt-out text is not a reply. Email and calls are not read yet (no inbox
-- reader, L10-07).
--
-- Previous release on this schema: unaffected (two nullable columns, no
-- default, metadata-only ADD COLUMN; it never reads them).
--
-- Rollback: DROP INDEX idx_lead_response_events_reply_client,
-- idx_lead_response_events_reply_contact; ALTER TABLE lead_response_events
-- DROP COLUMN reply_sms_message_id, DROP COLUMN replied_at.
-- ---------------------------------------------------------------------------

ALTER TABLE lead_response_events
    ADD COLUMN IF NOT EXISTS replied_at timestamptz,
    ADD COLUMN IF NOT EXISTS reply_sms_message_id uuid;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lead_response_events_reply_sms_fk') THEN
        ALTER TABLE lead_response_events
            ADD CONSTRAINT lead_response_events_reply_sms_fk
            FOREIGN KEY (reply_sms_message_id) REFERENCES sms_messages(id) ON DELETE SET NULL;
    END IF;
END $$;

-- The reply lookup: the newest unanswered response for a client / contact.
CREATE INDEX IF NOT EXISTS idx_lead_response_events_reply_client
    ON lead_response_events (tenant_id, client_id, accepted_at DESC)
    WHERE client_id IS NOT NULL AND replied_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lead_response_events_reply_contact
    ON lead_response_events (tenant_id, contact_id, accepted_at DESC)
    WHERE contact_id IS NOT NULL AND replied_at IS NULL;

COMMENT ON COLUMN lead_response_events.replied_at IS
    '0158 (L04-03): first inbound text from the same client/contact after acceptance, within the reply window. Not a disposition: the row stays delivered/accepted.';
