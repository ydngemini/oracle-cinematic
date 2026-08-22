-- 0077 — how an AI action is undone
--
-- ai_chat_actions was written by exactly one code path: the shared field-update
-- tail in ai_chat_store._execute_safe_tool. Every other mutating tool returned
-- early and wrote nothing — so add_client_note, add_client_tag, move_deal_stage,
-- archive_client, create_client and create_deal_note all mutated a record and
-- left no ledger row.
--
-- That was not merely a gap in history. `_is_record_change` broadcasts any
-- successful non-read tool to the UI as an applied receipt, and ActionReceipt
-- renders an Undo button for it — so those six produced a button that POSTs to
-- .../actions/undefined/undo. The tools whose risk class literally reads
-- "undoable through the ai_chat_actions ledger" were the ones not in it.
--
-- Undoing them is not one operation, hence undo_kind:
--
--   field_restore  put the recorded column values back (the original path)
--   row_delete     remove the rows the action inserted, listed in undo_payload
--   none           genuinely not reversible; the UI must not offer a button
--
-- NOT NULL with no default, deliberately: a new tool that forgets to declare
-- how it is undone fails at INSERT rather than shipping another dead button.
-- The table is empty on dev and the backfill covers any deployed row, all of
-- which came from the field tail.

BEGIN;

ALTER TABLE ai_chat_actions
    ADD COLUMN IF NOT EXISTS undo_kind    text,
    ADD COLUMN IF NOT EXISTS undo_payload jsonb;

UPDATE ai_chat_actions SET undo_kind = 'field_restore' WHERE undo_kind IS NULL;

ALTER TABLE ai_chat_actions
    ALTER COLUMN undo_kind SET NOT NULL;

ALTER TABLE ai_chat_actions
    DROP CONSTRAINT IF EXISTS chk_ai_chat_actions_undo_kind;
ALTER TABLE ai_chat_actions
    ADD CONSTRAINT chk_ai_chat_actions_undo_kind
        CHECK (undo_kind IN ('field_restore', 'row_delete', 'none'));

-- A row_delete that does not say what to delete is unusable; a 'none' that
-- carries a payload is claiming a reversal it will not perform.
ALTER TABLE ai_chat_actions
    DROP CONSTRAINT IF EXISTS chk_ai_chat_actions_undo_payload;
ALTER TABLE ai_chat_actions
    ADD CONSTRAINT chk_ai_chat_actions_undo_payload
        CHECK ((undo_kind = 'row_delete') = (undo_payload IS NOT NULL));

-- create_deal_note appends to transactions.notes, which is none of the three
-- record types the ledger previously allowed.
ALTER TABLE ai_chat_actions
    DROP CONSTRAINT IF EXISTS ai_chat_actions_record_type_check;
ALTER TABLE ai_chat_actions
    ADD CONSTRAINT ai_chat_actions_record_type_check
        CHECK (record_type IN ('client', 'lead', 'listing', 'deal'));

COMMIT;
