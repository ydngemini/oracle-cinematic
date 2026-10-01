-- ---------------------------------------------------------------------------
-- 0115 — let a reconstruction job actually record 'failed_quality_gate'.
--
-- 0101 widened the status set by adding reconstruction_jobs_status_chk, but the
-- original chk_recon_status from 0023 was never dropped (0101 dropped a name
-- that did not exist). Both CHECKs apply, so their intersection still excluded
-- 'failed_quality_gate': every capture refused at the quality gate crashed the
-- worker's status write, and the job was marked 'failed' with a constraint
-- violation as its error — hiding the "recapture" answer 0101 existed to give.
-- Found when Mission 8 first ran reconstruction jobs through the production
-- topology (API replicas + separate worker).
-- ---------------------------------------------------------------------------

ALTER TABLE reconstruction_jobs DROP CONSTRAINT IF EXISTS chk_recon_status;
