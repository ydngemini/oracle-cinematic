import { useCallback, useEffect, useState } from 'react';
import { crmGet, crmPost, crmPostDownload } from '../state/useCrmApi';
import styles from './AccountDataPanel.module.css';
import { friendlyError } from '../lib/errorMessages';

// Account & data — the brokerage owner's export, retention and closure
// controls. Owner-only (the server enforces it; this only hides the panel).
// Every irreversible step asks for the password again: a session left open on
// a shared computer must not be enough to take or destroy every record.

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, {
  year: 'numeric', month: 'long', day: 'numeric',
}) : '');


// Exports refused because data would be missing (audit 29.5): configuration or
// stored data must be fixed first, so "try again" would only fail again.
const EXPORT_SUPPORT_CODES = ['master_key_missing', 'undecryptable', 'unexported_column'];
function exportNeedsSupport(error) {
  return EXPORT_SUPPORT_CODES.some((code) => String(error || '').includes(code));
}

// What Stripe has confirmed about the subscription's renewal after a closure
// or a withdrawal (audit 22.1): the server's billing_renewal / billing state,
// never a promise made before Stripe answered. An unknown state reads as
// pending.
const RENEWAL_TEXT = {
  stop_renewal: {
    confirmed: 'Stripe has confirmed your subscription will not renew.',
    pending: 'Stopping your subscription\u2019s renewal is not confirmed by Stripe yet; Neoh keeps trying.',
    divergent: 'Neoh could not confirm with Stripe that your subscription will not renew. Neoh support has been alerted; contact support before your next renewal date.',
    no_subscription: 'You have no subscription that renews.',
  },
  renew: {
    confirmed: 'Your subscription renews as before.',
    pending: 'Restarting your subscription\u2019s renewal is not confirmed by Stripe yet; Neoh keeps trying.',
    divergent: 'Neoh could not confirm with Stripe that your subscription renews again. Neoh support has been alerted.',
    no_subscription: '',
  },
};

function renewalText(desired, state) {
  const texts = RENEWAL_TEXT[desired];
  if (!texts || state === 'skipped') return '';
  return texts[state] ?? texts.pending;
}

function retentionLabel(days) {
  if (days === null || days === undefined) return 'Kept until you delete it';
  if (days >= 365) return `${Math.round(days / 365)} years`;
  return `${days} days`;
}

export function AccountDataPanel() {
  const [state, setState] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [password, setPassword] = useState('');
  const [closing, setClosing] = useState(false);
  const [confirmName, setConfirmName] = useState('');
  const [reason, setReason] = useState('');
  const [preview, setPreview] = useState(null);

  const load = useCallback(() => (
    crmGet('/api/privacy/lifecycle')
      .then((data) => { setState(data); setError(''); })
      .catch((err) => setError(friendlyError(err, { fallback: 'Could not load your account status.' })))
  ), []);

  useEffect(() => { load(); }, [load]);

  const act = async (fn, done) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await fn();
      setNotice(typeof done === 'function' ? done(result) : done);
      setPassword('');
      await load();
    } catch (err) {
      setError(friendlyError(err, { fallback: 'That did not work. Nothing was changed.' }));
    } finally {
      setBusy(false);
    }
  };

  const exports = (state?.operations || []).filter((op) => op.kind === 'export');
  const latest = exports[0];
  const exportReady = latest?.state === 'succeeded' && latest?.artifact_expires_at
    && new Date(latest.artifact_expires_at) > new Date();
  const exportRunning = latest && ['requested', 'running'].includes(latest.state);
  const renewal = state?.billing_renewal;
  const renewalStatus = renewal ? renewalText(renewal.desired, renewal.state) : '';

  if (!state && !error) {
    return (
      <section className={styles.panel} aria-label="Account and data" aria-busy="true">
        <span className={styles.label}>Account &amp; data</span>
      </section>
    );
  }

  return (
    <section className={styles.panel} aria-label="Account and data" aria-busy={busy}>
      <span className={styles.label}>Account &amp; data</span>

      {state?.lifecycle_state === 'closing' && (
        <div className={styles.banner} role="status">
          <strong>This brokerage is closing.</strong> Agents are signed out, public links and
          phone lines are off, and nothing is being sent. Everything will be permanently
          erased on <strong>{fmtDate(state.erase_after)}</strong>
          {state.legal_hold ? ' — erasure is paused by a legal hold' : ''}. Until then you can
          export your data or keep the brokerage.
          {renewal?.desired === 'stop_renewal' && renewalStatus && (
            <> <span data-testid="renewal-status">{renewalStatus}</span></>
          )}
        </div>
      )}

      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      <label className={styles.field}>
        <span className={styles.micro}>Password (required for each action below)</span>
        <input
          type="password" className={styles.input} autoComplete="current-password"
          value={password} onChange={(e) => setPassword(e.target.value)}
        />
      </label>

      {/* ── Export ──────────────────────────────────────────────────────── */}
      <div className={styles.block}>
        <h3 className={styles.heading}>Export everything</h3>
        <p className={styles.text}>
          One archive of every client, contact, message, call note, task, transaction and
          document, with photos. Passwords and connected-account keys are never included.
          The archive is deleted from Neoh after 7 days.
        </p>
        {latest && (
          <p className={styles.text} data-testid="export-status">
            {exportRunning && 'Your export is being prepared…'}
            {latest.state === 'succeeded' && exportReady && `Ready — available until ${fmtDate(latest.artifact_expires_at)}.`}
            {latest.state === 'succeeded' && exportReady && latest.result?.completeness === 'partial_media'
              && ' Some photos or documents could not be included; the archive\u2019s manifest lists how many.'}
            {latest.state === 'succeeded' && !exportReady && 'Your last export has expired.'}
            {latest.state === 'failed' && (exportNeedsSupport(latest.error)
              ? 'The last export was stopped because it could not include all of your data. Trying again will not help: contact Neoh support.'
              : 'The last export failed. Try again.')}
          </p>
        )}
        <div className={styles.actions}>
          <button
            type="button" className={styles.button} disabled={busy || !password || exportRunning}
            onClick={() => act(() => crmPost('/api/privacy/exports', { password }),
              'Export started. It will appear here when ready.')}
          >
            Prepare export
          </button>
          {exportReady && (
            <button
              type="button" className={styles.button} disabled={busy || !password}
              onClick={() => act(
                () => crmPostDownload(`/api/privacy/exports/${latest.id}/download`, { password },
                  `neoh-export-${latest.id}.zip`),
                'Download started.',
              )}
            >
              Download
            </button>
          )}
        </div>
      </div>

      {/* ── Retention ───────────────────────────────────────────────────── */}
      <div className={styles.block}>
        <h3 className={styles.heading}>How long Neoh keeps data</h3>
        {!policy ? (
          <button
            type="button" className={styles.link}
            onClick={() => crmGet('/api/privacy/policy').then(setPolicy).catch((e) => setError(friendlyError(e, { fallback: '' })))}
          >
            Show the retention schedule
          </button>
        ) : (
          <>
            <ul className={styles.policy}>
              {policy.categories.map((c) => (
                <li key={c.category}>
                  <span>{c.category.replace(/_/g, ' ')}</span>
                  <span className={styles.dim}>{retentionLabel(c.retention_days)}</span>
                </li>
              ))}
            </ul>
            <p className={styles.dim}>
              Encrypted database backups roll over every {policy.backup_days} days and cannot be
              edited, so deleted data leaves the backups within {policy.backup_days} days.
            </p>
          </>
        )}
      </div>

      {/* ── Close / reopen ──────────────────────────────────────────────── */}
      <div className={styles.block} data-danger="true">
        {state?.lifecycle_state === 'closing' ? (
          <>
            <h3 className={styles.heading}>Keep this brokerage</h3>
            <label className={styles.field}>
              <span className={styles.micro}>Reason</span>
              <input className={styles.input} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <div className={styles.actions}>
              <button
                type="button" className={styles.button} disabled={busy || !password || reason.trim().length < 3}
                onClick={() => act(() => crmPost('/api/privacy/closure/withdraw', { password, reason }),
                  (r) => ['Closure withdrawn. Your agents can sign in again; reconnect anything you need.',
                    renewalText('renew', r?.billing)].filter(Boolean).join(' '))}
              >
                Keep the brokerage
              </button>
            </div>
          </>
        ) : !closing ? (
          <>
          {renewal?.desired === 'renew' && ['pending', 'divergent'].includes(renewal.state) && (
            <p className={styles.text} role="status" data-testid="renewal-status">{renewalStatus}</p>
          )}
          <button
            type="button" className={styles.link}
            onClick={() => {
              setClosing(true);
              crmGet('/api/privacy/closure/preview').then(setPreview).catch(() => setPreview(null));
            }}
          >
            Close this brokerage…
          </button>
          </>
        ) : (
          <>
            <h3 className={styles.heading}>Close this brokerage</h3>
            <ul className={styles.consequences}>
              <li>Every agent is signed out now; public client links and sites go offline.</li>
              <li>The AI stops answering your phone lines and nothing more is sent.</li>
              <li>Neoh tells Stripe to stop your subscription renewing, and shows here once Stripe confirms it.</li>
              <li>For {state?.grace_days ?? 30} days you can still export, or change your mind.</li>
              <li>After that everything is permanently erased. Billing records and opt-out
                lists are kept as the law requires; you will get a receipt.</li>
            </ul>
            {preview && (
              <p className={styles.text} data-testid="closure-preview">
                This would permanently erase {preview.highlights?.clients ?? 0} clients,{' '}
                {preview.highlights?.agent_contacts ?? 0} contacts, {preview.highlights?.sms_messages ?? 0} text
                messages, {preview.highlights?.inbound_voice_calls ?? 0} calls,{' '}
                {preview.highlights?.ai_chat_messages ?? 0} Neoh conversations, {preview.stored_objects ?? 0} files,
                and the accounts of {preview.highlights?.users ?? 0} people.
              </p>
            )}
            <label className={styles.field}>
              <span className={styles.micro}>Type the brokerage name: {state?.name}</span>
              <input className={styles.input} value={confirmName} onChange={(e) => setConfirmName(e.target.value)} />
            </label>
            <label className={styles.field}>
              <span className={styles.micro}>Reason</span>
              <input className={styles.input} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <div className={styles.actions}>
              <button type="button" className={styles.ghost} onClick={() => setClosing(false)} disabled={busy}>
                Cancel
              </button>
              <button
                type="button" className={styles.danger}
                disabled={busy || !password || confirmName.trim() !== (state?.name || '').trim() || reason.trim().length < 3}
                onClick={() => act(
                  () => crmPost('/api/privacy/closure', { confirm_name: confirmName, reason, password }),
                  (r) => `Closure scheduled. ${renewalText('stop_renewal', r?.billing)}`,
                ).then(() => setClosing(false))}
              >
                Close brokerage
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

export default AccountDataPanel;
