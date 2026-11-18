-- ---------------------------------------------------------------------------
-- 0178 — invitation emails are queued, not sent inside the request
-- (external audit 21.5).
--
-- A bulk invite (up to 25 addresses) sent each email one after another
-- before answering, at up to 40 s each: the owner saw a timeout, pressed Send
-- again, and that re-invite revoked the links already on their way.
--
-- The invitation row now keeps its one-time link sealed (encrypted with the
-- brokerage's key) until the delivery job has sent it; the job clears it.
-- The raw token itself is still never stored in the clear and only its
-- digest identifies the invitation; while it waits to be sent, though, the
-- link is recoverable by whoever holds the database and the master key.
-- ---------------------------------------------------------------------------

ALTER TABLE brokerage_invitations
    ADD COLUMN IF NOT EXISTS pending_link_ciphertext bytea;

-- The sealed link never outlives its use: revoked (by the owner, a reissue or
-- the privacy lifecycle) or accepted (consume_brokerage_invitation) clears
-- it, whichever path writes the row. The delivery job also clears it once
-- sent, on expiry and on its last attempt. Trigger function without SET.
CREATE OR REPLACE FUNCTION brokerage_invitations_drop_sealed_link() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.revoked_at IS NOT NULL OR NEW.consumed_at IS NOT NULL THEN
        NEW.pending_link_ciphertext := NULL;
    END IF;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS trg_brokerage_invitations_drop_sealed_link ON brokerage_invitations;
CREATE TRIGGER trg_brokerage_invitations_drop_sealed_link
    BEFORE UPDATE ON brokerage_invitations
    FOR EACH ROW EXECUTE FUNCTION brokerage_invitations_drop_sealed_link();

-- 0109 narrowed the app's UPDATE on this table to named columns; 0123 added
-- delivery_status/delivery_error without granting them, so recording whether
-- an invitation email left has always failed (silently: "visibility only").
-- The delivery job writes them, and the sealed link it clears.
GRANT UPDATE (delivery_status, delivery_error, pending_link_ciphertext)
    ON brokerage_invitations TO oracle_app;
