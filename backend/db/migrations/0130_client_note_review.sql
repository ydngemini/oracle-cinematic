-- 0130: an AI call note can wait for the agent's review.
--
-- Owner decision D-2026-10-12-03 (docs/claude-prompts/LEDGER.md): AI call
-- notes support both modes, chosen per agent:
--   review (default) — the note is saved at once but marked 'proposed'; the
--                      agent approves, edits or discards it. Proposed notes
--                      never feed AI context.
--   direct           — the note is saved 'approved', as before.
-- Every existing note was written or accepted by a person, so existing rows
-- default to 'approved'. (0129 is reserved by the open missions PR #11.)

ALTER TABLE client_notes
    ADD COLUMN IF NOT EXISTS review_state text NOT NULL DEFAULT 'approved';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'client_notes_review_state_check') THEN
        ALTER TABLE client_notes
            ADD CONSTRAINT client_notes_review_state_check
            CHECK (review_state IN ('proposed', 'approved'));
    END IF;
END $$;

ALTER TABLE client_notes ADD COLUMN IF NOT EXISTS reviewed_by text;
ALTER TABLE client_notes ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

-- The review queue is "proposed notes for this client", read on every Notes
-- open; keep it off a scan of the client's whole note history.
CREATE INDEX IF NOT EXISTS idx_client_notes_proposed
    ON client_notes (client_id, created_at DESC)
    WHERE review_state = 'proposed';
