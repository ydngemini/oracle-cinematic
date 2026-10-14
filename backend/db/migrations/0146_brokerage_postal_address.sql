-- 0146: the brokerage's postal address, for the footer of commercial email.
--
-- CAN-SPAM (15 U.S.C. 7704(a)(5)(A)(iii)) requires every commercial email to
-- carry the sender's valid physical postal address. Neoh sends client and
-- lead outreach email for a brokerage (commands_api._execute_command_job,
-- email_outbox.deliver), but tenants has never had an address column
-- (0001 id/slug/name; 0109 org_type/primary_state/website; 0122 lifecycle;
-- 0127 is_demo), so no footer could carry one. commercial_email.py adds the
-- address when it is set and the brokerage setup screen says "setup required"
-- while it is not. Nothing is backfilled: an address is not invented.
--
-- The 10DLC brand address in tenant_messaging_brands (0108) is a registration
-- record for a different purpose and is NOT copied here; an owner confirms
-- the address their email should show.
--
-- Plain text, not encrypted: it is printed in every commercial email the
-- brokerage sends, so it is published by design. For an independent agent it
-- can be a home address, so it is personal data: privacy_data_map.py and the
-- erasure scrub (privacy_lifecycle._phase_tenant_row) clear it.
--
-- Additive and nullable, no function, no policy change: tenants keeps its
-- 0109 policy. The previous release never reads the column.
--
-- Rollback: ALTER TABLE tenants DROP COLUMN postal_address; (the address is
-- lost, the footer goes back to the unsubscribe link only).

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS postal_address text;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_tenants_postal_address') THEN
        ALTER TABLE tenants
            ADD CONSTRAINT chk_tenants_postal_address CHECK (
                postal_address IS NULL
                OR (char_length(postal_address) BETWEEN 5 AND 300
                    AND postal_address = btrim(postal_address)));
    END IF;
END $$;

COMMENT ON COLUMN tenants.postal_address IS
    'Physical postal address printed in the footer of commercial email '
    '(CAN-SPAM). Set by the broker owner; never inferred. Scrubbed on erasure.';
