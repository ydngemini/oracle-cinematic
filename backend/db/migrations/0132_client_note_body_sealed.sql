-- 0132: client notes are sealed at rest (audit A01, ledger L17-03).
--
-- AI call notes (commands_api.deliver_call_notes, since aec14c4) are what a
-- customer said on a call, and they were written to client_notes.body, a
-- plaintext text column (0019). From this migration on the app writes every
-- note's text only to body_ciphertext — pgp_sym_encrypt under the per-tenant
-- HKDF key, the ai_chat_messages / agent_contacts scheme (crypto.py) — and
-- leaves body ''. The key never reaches the database, so this file cannot
-- encrypt the existing rows: backend/backfill_client_notes.py does that in
-- bounded, restartable batches. Until it has run, readers open both shapes
-- (backend/client_note_crypto.py).
--
-- Guards:
--   * client_notes_sealed_body_empty — a sealed row keeps no plaintext copy.
--   * client_notes_before_write — an AI call note can no longer be written in
--     plaintext (F17.03). It applies only to the AI author: other notes are
--     sealed by every app path, but direct SQL fixtures still insert plaintext,
--     and pre-0132 rows stay legal until the backfill reaches them. Editing
--     only the review state or pin of an old plaintext AI note stays legal.
--   * the same trigger now keeps updated_at: a seal-only rewrite done by the
--     backfill (app.client_note_reseal = 'on', transaction-local) does not
--     change what the note says, so it must not show the note as "edited".
--
-- Rollback (code first needs plaintext back): run
--   python backfill_client_notes.py --unseal
-- AFTER dropping the trigger (it refuses plaintext AI call notes):
--   DROP TRIGGER trg_client_notes_before_write ON client_notes;
--   CREATE TRIGGER trg_client_notes_updated BEFORE UPDATE ON client_notes
--       FOR EACH ROW EXECUTE FUNCTION set_updated_at();
--   ALTER TABLE client_notes DROP CONSTRAINT client_notes_sealed_body_empty;
-- then the column may stay (NULL everywhere) or be dropped.
-- (0129 is reserved by the open missions PR #11.)

ALTER TABLE client_notes ADD COLUMN IF NOT EXISTS body_ciphertext bytea;

COMMENT ON COLUMN client_notes.body_ciphertext IS
    'pgp_sym_encrypt(note text, per-tenant HKDF key; crypto.py). When set, body is ''''. '
    'NULL = a pre-0132 row whose plaintext body backfill_client_notes.py has not sealed yet.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'client_notes_sealed_body_empty') THEN
        ALTER TABLE client_notes
            ADD CONSTRAINT client_notes_sealed_body_empty
            CHECK (body_ciphertext IS NULL OR body = '');
    END IF;
END $$;

CREATE OR REPLACE FUNCTION client_notes_before_write()
RETURNS trigger
LANGUAGE plpgsql AS
$$
BEGIN
    -- commands_api.AI_CALL_NOTE_AUTHOR
    IF NEW.author_id = 'neoh-ai-call' AND NEW.body_ciphertext IS NULL AND NEW.body <> ''
       AND (TG_OP = 'INSERT' OR NEW.body IS DISTINCT FROM OLD.body) THEN
        RAISE EXCEPTION 'an AI call note must be sealed (client_notes.body_ciphertext)'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF current_setting('app.client_note_reseal', true) = 'on'
           AND (NEW.tenant_id, NEW.client_id, NEW.author_id, NEW.pinned, NEW.created_at,
                NEW.review_state, NEW.reviewed_by, NEW.reviewed_at, NEW.updated_at)
               IS NOT DISTINCT FROM
               (OLD.tenant_id, OLD.client_id, OLD.author_id, OLD.pinned, OLD.created_at,
                OLD.review_state, OLD.reviewed_by, OLD.reviewed_at, OLD.updated_at) THEN
            RETURN NEW;  -- only the storage of the same text changed
        END IF;
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION client_notes_before_write() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION client_notes_before_write() TO oracle_app;

DROP TRIGGER IF EXISTS trg_client_notes_updated ON client_notes;
DROP TRIGGER IF EXISTS trg_client_notes_before_write ON client_notes;
CREATE TRIGGER trg_client_notes_before_write
    BEFORE INSERT OR UPDATE ON client_notes
    FOR EACH ROW EXECUTE FUNCTION client_notes_before_write();
