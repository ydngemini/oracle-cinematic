-- ---------------------------------------------------------------------------
-- 0148 — the audit hash chain is verified on a schedule (L17-12).
--
-- audit_ledger.verify_chain() existed but nothing called it on a schedule,
-- so a broken or tampered chain would be found only if someone asked (ISO
-- 27001 Annex A assessment, 2026-10-14). The worker now walks the chain in
-- bounded segments (audit_chain_verify periodic task): each run continues
-- from a cursor, checks at most N links, and starts a new pass from the expiry
-- anchor (audit_chain_checkpoints) when it reaches the head. A full pass
-- therefore re-checks every row, old ones included, at a bounded cost per run.
--
-- One row (id = 1) holds the cursor, the pass bookkeeping and the first broken
-- link found, so a break alerts once, not on every run.
--
-- Platform-level: no tenant column, no customer data (seq numbers, hashes,
-- timestamps). Readable and writable only by a platform-admin context (the
-- platform login, 0120), so a request connection cannot move the cursor past
-- a break or clear it. No DELETE for the app.
--
-- Additive: the previous release neither reads nor writes this table.
-- Rollback: DROP TABLE audit_chain_verification (nothing references it).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_chain_verification (
    id                     smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    cursor_seq             bigint,
    cursor_hash            text,
    pass_started_at        timestamptz,
    rows_in_pass           bigint      NOT NULL DEFAULT 0,
    last_complete_pass_at  timestamptz,
    last_pass_rows         bigint,
    last_run_at            timestamptz,
    last_status            text        CHECK (last_status IN ('intact', 'broken', 'empty')),
    broken_seq             bigint,
    broken_detected_at     timestamptz,
    alerted_broken_seq     bigint
);

ALTER TABLE audit_chain_verification ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_chain_verification FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_chain_verification_platform ON audit_chain_verification;
CREATE POLICY audit_chain_verification_platform ON audit_chain_verification
    USING ((SELECT app_is_platform_admin()))
    WITH CHECK ((SELECT app_is_platform_admin()));

REVOKE ALL ON audit_chain_verification FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON audit_chain_verification FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON audit_chain_verification TO oracle_app;
