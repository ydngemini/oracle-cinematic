-- ---------------------------------------------------------------------------
-- 0183 — a consent record carries its evidence (external audit 26.6, and the
-- two L09-13 blockers). Rules chosen conservatively by the Claude session under
-- the owner's instruction of 2026-11-20 ("continue all" on the open
-- decisions); the owner may revise them; not counsel-reviewed.
--
-- 26.6: POST /api/compliance/outreach/consent stored any 'express_written' or
-- 'biometric_voiceprint' row on the caller's word (proof optional), and the
-- send gate (outreach_compliance.ConsentLedger.gather_state) treated every such
-- active row as prior express WRITTEN consent for AI voice and as a BIPA
-- release. Now each row says whether its evidence was documented:
--
--   evidence_status  'documented' only when the SERVER saw a recognised capture
--                    method (proof_source), the disclosure text the person
--                    agreed to (proof_text), an evidence reference and a
--                    capture time not in the future. Anything else is stored
--                    'attested_unverified'. The client never sets it.
--   evidence_ref     the signed-event / envelope / document / message /
--                    recording id the evidence can be retrieved by.
--   recorded_by      the signed-in user (agent id) who recorded the row,
--                    set by the server.
--
-- The gate counts only 'documented' rows as written consent (AI voice,
-- marketing texts) or as a voiceprint release. Existing rows keep the default
-- 'attested_unverified' (deliberately no backfill: nobody documented them), so
-- an AI call, a marketing text or a recorded Illinois call that relied on one
-- is blocked until the consent is recorded again with its evidence.
--
-- L09-13: outreach_consent.expires_at is CONSENT VALIDITY (the gate already
-- reads it that way); 0015's "retention floor" wording is superseded by the
-- column comment below. Voiceprint releases stored under channel 'sms' are not
-- re-labelled: they stay blocked (a BIPA release is a voice release, recorded
-- again with evidence).
--
-- Personal data: evidence_ref is a reference id (free text, entered by the
-- agent); recorded_by is the recording user's email. Both are erased with the
-- consent row (privacy_data_map: outreach_consent, ERASE).
--
-- The database enforces the rule too (review of PR #170): a BEFORE INSERT
-- trigger downgrades a row claiming 'documented' to 'attested_unverified'
-- unless the evidence holds (it never upgrades one), and a row is immutable
-- after insert except revoked_at, which may be set once and never cleared.
-- "documented" still only means the recorder filled the fields; Neoh keeps no
-- evidence artifact and does not check the reference (LEDGER L09-17).
--
-- Additive and idempotent. The previous release keeps working: its INSERTs omit
-- the new columns and get 'attested_unverified', which the CHECKs and the
-- trigger accept; it never UPDATEs this table.
-- Rollback: application-only is safe (the old gate ignores the columns). To
-- remove the schema: DROP TRIGGER trg_outreach_consent_evidence_guard; DROP
-- FUNCTION outreach_consent_evidence_guard(); DROP CONSTRAINT
-- chk_consent_documented_evidence, chk_consent_evidence_status; DROP COLUMN
-- recorded_by, evidence_ref, evidence_status — which also discards which rows
-- were documented.
-- ---------------------------------------------------------------------------

ALTER TABLE outreach_consent
    ADD COLUMN IF NOT EXISTS evidence_status text NOT NULL DEFAULT 'attested_unverified',
    ADD COLUMN IF NOT EXISTS evidence_ref    text,
    ADD COLUMN IF NOT EXISTS recorded_by     text;

ALTER TABLE outreach_consent DROP CONSTRAINT IF EXISTS chk_consent_evidence_status;
ALTER TABLE outreach_consent ADD CONSTRAINT chk_consent_evidence_status
    CHECK (evidence_status IN ('documented', 'attested_unverified'));

-- Belt and braces: whatever writes the row (a future code path, a seed, a
-- console), 'documented' needs the method, the disclosure text and the
-- reference. "Non-blank" = holds a character outside [[:space:]] (btrim would
-- strip only spaces; the application also strips tabs, line breaks and
-- Unicode spaces before storing). The capture-time rule needs the clock, so it
-- lives in the trigger below.
ALTER TABLE outreach_consent DROP CONSTRAINT IF EXISTS chk_consent_documented_evidence;
ALTER TABLE outreach_consent ADD CONSTRAINT chk_consent_documented_evidence
    CHECK (evidence_status <> 'documented' OR (
        proof_source IN ('web_form', 'e_signature', 'signed_document', 'recorded_call',
                         'sms_keyword_reply', 'ivr_keypress')
        AND coalesce(proof_text, '') ~ '[^[:space:]]'
        AND coalesce(evidence_ref, '') ~ '[^[:space:]]'));

-- The same rule, with the clock, on every insert: a claim of 'documented' that
-- does not hold is stored 'attested_unverified' (the application reads the
-- stored status back with RETURNING, so its answer follows). Never upgrades.
-- After insert the evidence cannot be rewritten: only revoked_at may change,
-- once (a revocation is not undone). Trigger function without SET (no custom
-- parameter, plain invoker rights), executed by the trigger only.
CREATE OR REPLACE FUNCTION outreach_consent_evidence_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.evidence_status = 'documented' AND NOT coalesce(
               NEW.proof_source IN ('web_form', 'e_signature', 'signed_document', 'recorded_call',
                                    'sms_keyword_reply', 'ivr_keypress')
               AND coalesce(NEW.proof_text, '') ~ '[^[:space:]]'
               AND coalesce(NEW.evidence_ref, '') ~ '[^[:space:]]'
               AND NEW.captured_at <= now(), false) THEN
            NEW.evidence_status := 'attested_unverified';
        END IF;
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - 'revoked_at') IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at')
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'outreach_consent rows are immutable: only revoked_at may be set, once'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION outreach_consent_evidence_guard() FROM PUBLIC;
DROP TRIGGER IF EXISTS trg_outreach_consent_evidence_guard ON outreach_consent;
CREATE TRIGGER trg_outreach_consent_evidence_guard
    BEFORE INSERT OR UPDATE ON outreach_consent
    FOR EACH ROW EXECUTE FUNCTION outreach_consent_evidence_guard();

COMMENT ON COLUMN outreach_consent.expires_at IS
    'Consent validity: after this time the consent no longer authorises contact '
    '(the send gate ignores it). NULL = no stated end. Not a retention date: '
    'how long the row is kept is governed by the retention policy (privacy data '
    'map), not by this column. Supersedes the "retention floor" note in 0015.';
COMMENT ON COLUMN outreach_consent.evidence_status IS
    'documented = the recorder supplied a recognised capture method, the disclosure '
    'text agreed to, an evidence reference and a capture time not in the future '
    '(Neoh does not check the reference or keep the evidence); attested_unverified = '
    'anything else. Only documented written consent and documented voiceprint '
    'releases authorise AI calls, marketing texts and voiceprint recording. Set by '
    'the server, never by the client; enforced again by '
    'trg_outreach_consent_evidence_guard (0183).';
COMMENT ON COLUMN outreach_consent.captured_at IS
    'When the person gave consent, as the recorder states it (defaults to the '
    'recording time; may be earlier, with no lower bound; a time after the '
    'recording is kept but the row is not documented). created_at is the trusted '
    'time the row was recorded (0183).';
COMMENT ON COLUMN outreach_consent.evidence_ref IS
    'Signed-event / envelope / document / message / recording id the evidence '
    'can be retrieved by (0183).';
COMMENT ON COLUMN outreach_consent.recorded_by IS
    'Agent id (email) of the signed-in user who recorded this consent; set by '
    'the server (0183).';
