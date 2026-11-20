import { useState } from 'react';
import { crmPost } from '../state/useCrmApi';
import styles from './SalesWorkspace.module.css';
import { friendlyError } from '../lib/errorMessages';

/**
 * The TCPA consent ledger: record consent, record an opt-out, and dry-run the
 * gate before a campaign goes out.
 *
 * All three endpoints shipped with no caller, which is the wrong shape for this
 * particular feature. The gate that blocks non-compliant outreach was already
 * enforced server-side on every send — so the product was compliant — but there
 * was no way for a person to WRITE the consent an agent obtained on a call, or
 * to honour an opt-out someone gave verbally, or to check a number before
 * loading it into a dialer. Compliance you cannot record is compliance you
 * cannot prove.
 *
 * Opt-out defaults to every channel. TCPA requires a STOP to be honoured across
 * the board within ten business days, so a per-channel default here would be
 * the wrong side to err on.
 *
 * Evidence (external audit 26.6, 0183): written consent and a voiceprint
 * release are stored as "documented" only when the server sees a capture
 * method, the disclosure text the person agreed to and an evidence reference.
 * The server decides — this panel only shows what came back. An unverified
 * claim is kept but authorises no AI call, voiceprint recording or marketing
 * text, and the panel says so in plain words.
 */

const CHANNELS = [['sms', 'SMS'], ['voice', 'Voice'], ['email', 'Email']];

const CONSENT_TYPES = [
  ['express_written', 'Express written'],
  ['express_oral', 'Express oral'],
  ['prior_business', 'Prior business relationship'],
  ['biometric_voiceprint', 'Biometric voiceprint'],
];

// The capture methods whose evidence can be retrieved later (mirrors the
// server's DOCUMENTED_PROOF_SOURCES). "Not documented" sends none.
const CAPTURE_METHODS = [
  ['', 'Not documented'],
  ['web_form', 'Web form'],
  ['e_signature', 'E-signature'],
  ['signed_document', 'Signed document'],
  ['recorded_call', 'Recorded call'],
  ['sms_keyword_reply', 'Text keyword reply'],
  ['ivr_keypress', 'Phone keypress (IVR)'],
];

// Written consent and voiceprint releases are the types the gate reads only
// when documented, so they are the ones that ask for evidence.
const EVIDENCE_TYPES = new Set(['express_written', 'biometric_voiceprint']);

const PURPOSES = [['marketing', 'Marketing'], ['informational', 'Informational']];

const UNVERIFIED_PLAIN = 'Recorded as an unverified claim. Until it is documented it authorises no AI call, '
  + 'no voiceprint recording and no marketing text.';

const OPT_OUT_REASONS = [
  ['manual_dnc', 'Asked not to be contacted'],
  ['stop_keyword', 'STOP keyword'],
  ['litigator', 'Known litigator'],
  ['regulatory', 'Regulatory'],
];

export default function OutreachConsentPanel() {
  const [contact, setContact] = useState('');
  const [channel, setChannel] = useState('sms');
  const [stateCode, setStateCode] = useState('');
  const [consentType, setConsentType] = useState('express_written');
  const [proof, setProof] = useState('');
  const [captureMethod, setCaptureMethod] = useState('');
  const [evidenceRef, setEvidenceRef] = useState('');
  const [purpose, setPurpose] = useState('marketing');
  const [recorded, setRecorded] = useState(null);
  const [optOutReason, setOptOutReason] = useState('manual_dnc');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [notice, setNotice] = useState('');

  const act = async (label, run) => {
    if (!contact.trim() || busy) return;
    setBusy(label);
    setError('');
    setNotice('');
    setResult(null);
    setRecorded(null);
    try {
      await run();
    } catch (reason) {
      setError(friendlyError(reason, { fallback: 'The consent ledger refused that.' }));
    } finally {
      setBusy('');
    }
  };

  const check = () => act('check', async () => {
    const decision = await crmPost('/api/compliance/outreach/check', {
      contact: contact.trim(),
      channel,
      state_code: stateCode.trim().toUpperCase() || null,
      purpose,
    });
    setResult(decision || null);
  });

  const recordConsent = () => act('consent', async () => {
    const payload = {
      contact: contact.trim(),
      channel: voiceprint ? 'voice' : channel,
      consent_type: consentType,
    };
    if (stateCode.trim()) payload.state_code = stateCode.trim().toUpperCase();
    if (proof.trim()) payload.proof_text = proof.trim();
    if (needsEvidence && captureMethod) payload.proof_source = captureMethod;
    if (needsEvidence && evidenceRef.trim()) payload.evidence_ref = evidenceRef.trim();
    const saved = await crmPost('/api/compliance/outreach/consent', payload);
    if (needsEvidence) {
      // Whatever the server stored, never what was hoped for: no status back
      // means it is not known to be documented.
      setRecorded({
        documented: saved?.evidence_status === 'documented',
        detail: saved?.evidence_notice || '',
      });
    } else {
      setNotice('Consent recorded.');
    }
  });

  const recordOptOut = () => act('optout', async () => {
    await crmPost('/api/compliance/outreach/opt-out', {
      contact: contact.trim(),
      channel: '*',
      reason: optOutReason,
      source_text: proof.trim() || null,
    });
    setNotice('Opt-out recorded across every channel.');
  });

  const allowed = result?.allowed ?? result?.permitted;
  const blockers = Array.isArray(result?.blockers) ? result.blockers : [];
  // A voiceprint release only counts on the voice channel, so the picker
  // shows (and sends) voice while that consent type is chosen.
  const voiceprint = consentType === 'biometric_voiceprint';
  const needsEvidence = EVIDENCE_TYPES.has(consentType);

  return (
    <section className={styles.panel} aria-labelledby="consent-title">
      <header className={styles.panelHeader}>
        <h4 id="consent-title">Consent ledger</h4>
      </header>
      <div className={styles.panelBody}>
        <p>
          Record what a person actually agreed to, honour an opt-out they gave you directly, and
          check a number before it reaches a dialer. The send-time gate enforces this either way —
          this is how the record gets written.
        </p>

        {error ? <div className={styles.error} role="alert">{error}</div> : null}
        {notice ? <div className={styles.success} role="status">{notice}</div> : null}
        {recorded?.documented ? (
          <div className={styles.success} role="status">Consent recorded with documented evidence.</div>
        ) : null}
        {recorded && !recorded.documented ? (
          <div className={styles.notice} role="status">
            <strong>{UNVERIFIED_PLAIN}</strong>
            {recorded.detail ? ` ${recorded.detail}` : ''}
          </div>
        ) : null}

        <label>
          <span>Contact (phone or email)</span>
          <input value={contact} onChange={(event) => setContact(event.target.value)} maxLength={320} />
        </label>
        <label>
          <span>Channel</span>
          <select
            value={voiceprint ? 'voice' : channel}
            disabled={voiceprint}
            onChange={(event) => setChannel(event.target.value)}
          >
            {CHANNELS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label>
          <span>State</span>
          <input value={stateCode} onChange={(event) => setStateCode(event.target.value)} maxLength={2} placeholder="DE" />
        </label>

        <label>
          <span>Message type (for the check)</span>
          <select value={purpose} onChange={(event) => setPurpose(event.target.value)}>
            {PURPOSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>

        <button
          type="button"
          className={styles.secondaryButton}
          onClick={check}
          disabled={!contact.trim() || busy !== ''}
        >
          {busy === 'check' ? 'Checking…' : 'Check before sending'}
        </button>

        {result ? (
          <div className={allowed ? styles.success : styles.error} role="status">
            {allowed ? 'Contactable on this channel.' : 'Blocked.'}
            {result.reason ? ` ${result.reason}` : ''}
            {result.detail ? ` ${result.detail}` : ''}
            {!allowed && blockers.length ? ` ${blockers.join('; ')}` : ''}
          </div>
        ) : null}

        <label>
          <span>Consent type</span>
          <select value={consentType} onChange={(event) => setConsentType(event.target.value)}>
            {CONSENT_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        {needsEvidence ? (
          <label>
            <span>How it was captured</span>
            <select value={captureMethod} onChange={(event) => setCaptureMethod(event.target.value)}>
              {CAPTURE_METHODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        ) : null}
        <label>
          <span>{needsEvidence ? 'Disclosure text the person agreed to' : 'Proof / source text'}</span>
          <input
            value={proof}
            onChange={(event) => setProof(event.target.value)}
            placeholder={needsEvidence
              ? 'The exact wording they agreed to'
              : 'Where this consent or opt-out came from'}
          />
        </label>
        {needsEvidence ? (
          <label>
            <span>Evidence reference</span>
            <input
              value={evidenceRef}
              onChange={(event) => setEvidenceRef(event.target.value)}
              maxLength={500}
              placeholder="Signed-event, envelope, document, message or recording ID"
            />
          </label>
        ) : null}
        {needsEvidence ? (
          <p>
            Written consent and voiceprint releases count only when documented: a capture method,
            the disclosure text and an evidence reference. Without them the claim is kept, but it
            authorises no AI call, voiceprint recording or marketing text.
          </p>
        ) : null}

        <button
          type="button"
          className={styles.secondaryButton}
          onClick={recordConsent}
          disabled={!contact.trim() || busy !== ''}
        >
          {busy === 'consent' ? 'Recording…' : 'Record consent'}
        </button>

        <label>
          <span>Opt-out reason</span>
          <select value={optOutReason} onChange={(event) => setOptOutReason(event.target.value)}>
            {OPT_OUT_REASONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <button
          type="button"
          className={styles.secondaryButton}
          onClick={recordOptOut}
          disabled={!contact.trim() || busy !== ''}
        >
          {busy === 'optout' ? 'Recording…' : 'Record opt-out (all channels)'}
        </button>
        <p>
          An opt-out suppresses every channel, not just the one selected above. TCPA requires a
          STOP to be honoured across the board.
        </p>
      </div>
    </section>
  );
}
