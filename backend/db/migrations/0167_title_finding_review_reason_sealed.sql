-- ---------------------------------------------------------------------------
-- 0167 — a title finding's review keeps the reviewer's reason, sealed (prompt
-- 11, T11.03, F11.04; follow-up of ledger L11-12; CONTRACT C5.1, audit A01).
--
-- title_findings has carried review_status/reviewed_by/reviewed_at since 0027
-- and no route wrote them. POST /api/intelligence/title/findings/{id}/review
-- now lets a broker owner confirm or dismiss one finding, and requires a
-- reason. The reason is the reviewer's own words and often names the owner
-- whose record it is or is not, so it is stored only encrypted
-- (backend/sealed_text.py: pgp_sym_encrypt under the per-tenant HKDF key,
-- 's2k-mode=1'), under the key of the finding's own tenant — never the
-- caller's context, since a platform admin may decide another tenant's
-- finding. There is no plaintext column for it. It is never copied into the
-- audit ledger's metadata or the client portal; nothing in the app reads it;
-- the brokerage export decrypts it (privacy_export.DECRYPT) and tenant erasure
-- deletes it with the row.
--
-- Rows decided before this migration (outside the app) stay NULL: their
-- reason was never recorded and is not guessed.
--
-- Previous release on this schema: unaffected (it never writes the column).
--
-- Rollback: ALTER TABLE title_findings DROP COLUMN review_reason_ciphertext;
-- ---------------------------------------------------------------------------

ALTER TABLE title_findings ADD COLUMN IF NOT EXISTS review_reason_ciphertext bytea;

COMMENT ON COLUMN title_findings.review_reason_ciphertext IS
    '0167: pgp_sym_encrypt(the reviewer''s reason, the finding tenant''s HKDF key; crypto.py). '
    'Free text that may name an owner: never copied into audit metadata or the client portal. '
    'NULL until decided through the app.';
