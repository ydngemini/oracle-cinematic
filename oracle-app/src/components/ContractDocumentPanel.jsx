import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPut } from '../state/useCrmApi';
import styles from './ContractVaultTab.module.css';

/**
 * The review lifecycle for one contract document: read, revise, decide, record
 * the signature.
 *
 * `contracts_api` carried all four and the vault called none of them, so a
 * document could be listed and its PDF opened and nothing else — no revision,
 * no approval or rejection, no record that it was signed. The lifecycle existed
 * end to end on the server and stopped at the screen.
 *
 * Every write here demands a written reason of at least eight characters, and
 * decision and signature both require BROKER_OWNER. That is deliberate on the
 * server's part: these are the points where a document becomes legally
 * operative, and the record of who decided and why is the product of the
 * workflow, not paperwork around it. The UI surfaces the requirement rather
 * than trying to smooth it away.
 */

export default function ContractDocumentPanel({ documentId, onChanged }) {
  const [doc, setDoc] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');

  const [revisedText, setRevisedText] = useState('');
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [signatureRef, setSignatureRef] = useState('');

  const load = useCallback(() => {
    setError('');
    return crmGet(`/api/contracts/documents/${documentId}?include_draft=true`).then(
      (payload) => {
        const record = payload?.document || payload || null;
        setDoc(record);
        setRevisedText(record?.draft_text || record?.body_text || '');
      },
      (reason_) => setError(reason_?.message || 'This document could not be read.'),
    );
  }, [documentId]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => { void load(); });
    return () => window.cancelAnimationFrame(frame);
  }, [load]);

  const act = async (label, run) => {
    if (busy) return;
    setBusy(label);
    setError('');
    setNotice('');
    try {
      await run();
      await load();
      await onChanged?.();
    } catch (reason_) {
      setError(
        reason_?.status === 403
          ? 'Only a broker owner can approve, reject, or record a signature.'
          : reason_?.message || 'The vault refused that change.',
      );
    } finally {
      setBusy('');
    }
  };

  const saveDraft = () => act('draft', async () => {
    await crmPut(`/api/contracts/documents/${documentId}/draft`, {
      revised_text: revisedText,
      attorney_reviewer: reviewer.trim(),
    });
    setNotice('Revision saved.');
  });

  const decide = (decision) => act(decision, async () => {
    await crmPost(`/api/contracts/documents/${documentId}/decision`, {
      decision,
      reason: reason.trim(),
    });
    setReason('');
    setNotice(decision === 'approved' ? 'Approved.' : 'Rejected.');
  });

  const recordSignature = () => act('signed', async () => {
    await crmPost(`/api/contracts/documents/${documentId}/signed`, {
      signature_reference: signatureRef.trim(),
      reason: reason.trim(),
    });
    setSignatureRef('');
    setReason('');
    setNotice('Signature recorded.');
  });

  const reasonOk = reason.trim().length >= 8;

  return (
    <div className={styles.clientVault}>
      <div className={styles.clientVaultHeader}>
        <div>
          <span className={styles.kicker}>Document</span>
          <h2>{doc?.title || doc?.template_key || 'Contract document'}</h2>
        </div>
        <span>{doc?.status || 'unknown'}</span>
      </div>

      {error ? <div className={styles.error} role="alert"><p>{error}</p></div> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {!doc && !error ? <div className={styles.skeleton} aria-hidden="true" /> : null}

      {doc ? (
        <>
          <label>
            <span>Revised text</span>
            <textarea
              value={revisedText}
              onChange={(event) => setRevisedText(event.target.value)}
              rows={10}
              maxLength={200000}
            />
          </label>
          <label>
            <span>Attorney reviewer</span>
            <input
              value={reviewer}
              onChange={(event) => setReviewer(event.target.value)}
              maxLength={200}
              placeholder="Who reviewed this text"
            />
          </label>
          <button
            type="button"
            onClick={saveDraft}
            disabled={revisedText.trim().length < 20 || reviewer.trim().length < 3 || busy !== ''}
          >
            {busy === 'draft' ? 'Saving…' : 'Save revision'}
          </button>

          <label>
            <span>Reason (required for every decision below, 8+ characters)</span>
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              placeholder="Why this decision is being made"
            />
          </label>

          <div>
            <button type="button" onClick={() => decide('approved')} disabled={!reasonOk || busy !== ''}>
              {busy === 'approved' ? 'Approving…' : 'Approve'}
            </button>
            <button type="button" onClick={() => decide('rejected')} disabled={!reasonOk || busy !== ''}>
              {busy === 'rejected' ? 'Rejecting…' : 'Reject'}
            </button>
          </div>

          <label>
            <span>Signature reference</span>
            <input
              value={signatureRef}
              onChange={(event) => setSignatureRef(event.target.value)}
              maxLength={500}
              placeholder="Envelope or reference id from the signing service"
            />
          </label>
          <button
            type="button"
            onClick={recordSignature}
            disabled={signatureRef.trim().length < 4 || !reasonOk || busy !== ''}
          >
            {busy === 'signed' ? 'Recording…' : 'Record signature'}
          </button>
          <p>
            Recording a signature states that execution happened elsewhere and stores the reference.
            It does not sign anything itself.
          </p>
        </>
      ) : null}
    </div>
  );
}
