import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost, apiPut, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';
import { TwoStepSetup } from './TwoStepSetup';
import { MFA_ENROLMENT_EVENT } from './TwoStepGate';
import styles from './TwoStepSettings.module.css';

// The server's own sentence is the useful one for these answers (wrong code,
// locked, wrong password); the generic 401/403 copy would say "sign in again".
const OWN_MESSAGE_CODES = new Set([
  'OTP_INVALID', 'OTP_REQUIRED', 'MFA_LOCKED', 'MFA_NOT_ENABLED', 'MFA_ALREADY_ENABLED', 'MFA_UNAVAILABLE',
]);

function twoStepError(err, fallback) {
  if (err instanceof ApiError) {
    if (OWN_MESSAGE_CODES.has(err.code)) return err.message;
    if (err.status === 403 && typeof err.detail === 'string') return err.detail;
    return formatApiError(err);
  }
  return fallback;
}

function ProtectedForm({ submitLabel, busyLabel, onSubmit, onCancel, danger }) {
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const ready = password && (code.replace(/\s/g, '').length === 6 || code.replace(/[^0-9a-z]/gi, '').length === 16);

  async function submit(event) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError('');
    try {
      await onSubmit({ password, code: code.trim() });
    } catch (err) {
      setCode('');
      setError(twoStepError(err, 'That did not work. Try again.'));
      setBusy(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <label className={styles.field}>
        <span className={styles.label}>Password</span>
        <input className={styles.input} type="password" autoComplete="current-password" value={password}
          onChange={(e) => setPassword(e.target.value)} disabled={busy} />
      </label>
      <label className={styles.field}>
        <span className={styles.label}>Code from your app (or a recovery code)</span>
        <input className={styles.input} autoComplete="one-time-code" value={code} placeholder="123456"
          onChange={(e) => setCode(e.target.value.slice(0, 19))} disabled={busy} />
      </label>
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div className={styles.actions}>
        <button type="submit" className={danger ? styles.danger : styles.primary} disabled={!ready || busy}>
          {busy ? busyLabel : submitLabel}
        </button>
        <button type="button" className={styles.tertiary} onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}

/**
 * Settings → Security → Two-step sign-in (L17-19): turn it on, get new
 * recovery codes, turn it off (or, when it is required, move it to a new
 * phone), and for a broker owner, require it for the whole brokerage.
 */
export function TwoStepSettings({ isOwner }) {
  const [status, setStatus] = useState(null);
  const [brokerage, setBrokerage] = useState(null);
  const [mode, setMode] = useState('idle');
  const [codes, setCodes] = useState([]);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(() => (
    Promise.all([
      apiGet('/auth/mfa', { retries: 1 }),
      isOwner ? apiGet('/api/brokerage/security', { retries: 1 }).catch(() => null) : Promise.resolve(null),
    ])
      .then(([mfa, security]) => { setStatus(mfa); setBrokerage(security); setError(''); })
      .catch((err) => setError(twoStepError(err, 'Two-step sign-in settings could not load.')))
  ), [isOwner]);

  useEffect(() => { load(); }, [load]);

  if (status && status.available === false) return null;

  const required = status?.required === true;
  const reasonText = status?.required_reason === 'brokerage'
    ? 'Your brokerage requires it for everyone.'
    : 'Required for your role.';

  async function newCodes(values) {
    const payload = await apiPost('/auth/mfa/recovery-codes', values, { retries: 0 });
    setCodes(Array.isArray(payload.recovery_codes) ? payload.recovery_codes : []);
    setMode('codes');
    load();
  }

  async function turnOff(values) {
    const payload = await apiPost('/auth/mfa/disable', values, { retries: 0 });
    setMode('idle');
    if (payload.enrolment_required) {
      // Required for this account: straight back to setup with the new phone.
      window.dispatchEvent(new CustomEvent(MFA_ENROLMENT_EVENT));
    } else {
      setNotice('Two-step sign-in is off. Your other sessions were signed out.');
    }
    load();
  }

  async function toggleBrokerage(next) {
    setError('');
    try {
      setBrokerage(await apiPut('/api/brokerage/security', { require_mfa: next }, { retries: 0 }));
      setNotice(next
        ? 'Everyone in your brokerage will set up two-step sign-in at their next sign-in.'
        : 'Two-step sign-in is optional for agents again. Owners still use it.');
    } catch (err) {
      setError(twoStepError(err, 'The brokerage setting could not be saved.'));
    }
  }

  return (
    <section className={styles.panel} aria-label="Two-step sign-in">
      <span className={styles.sectionLabel}>Two-step sign-in</span>
      {!status && !error && <p className={styles.copy} role="status">Loading…</p>}
      {status && (
        <p className={styles.copy}>
          <span className={styles.badge} data-on={status.enabled ? 'true' : 'false'}>
            {status.enabled ? 'On' : 'Off'}
          </span>
          {' '}
          {status.enabled
            ? `Signing in asks for a code from your authenticator app. ${status.recovery_codes_remaining} recovery code${status.recovery_codes_remaining === 1 ? '' : 's'} left.`
            : 'Add a code from an authenticator app to your sign-in, so a stolen password alone cannot open your account.'}
          {required && <> {reasonText}</>}
        </p>
      )}
      {notice && <p className={styles.notice} role="status">{notice}</p>}
      {error && <p className={styles.error} role="alert">{error}</p>}

      {status && !status.enabled && mode === 'idle' && (
        <div className={styles.actions}>
          <button type="button" className={styles.primary} onClick={() => { setNotice(''); setMode('setup'); }}>
            Turn on two-step sign-in
          </button>
        </div>
      )}
      {mode === 'setup' && (
        <TwoStepSetup
          onCancel={() => setMode('idle')}
          onDone={() => { setMode('idle'); setNotice('Two-step sign-in is on. Your other sessions were signed out.'); load(); }}
        />
      )}

      {status?.enabled && mode === 'idle' && (
        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={() => { setNotice(''); setMode('codes-form'); }}>
            New recovery codes
          </button>
          <button type="button" className={styles.secondary} onClick={() => { setNotice(''); setMode('off-form'); }}>
            {required ? 'Move to a new phone' : 'Turn off'}
          </button>
        </div>
      )}
      {mode === 'codes-form' && (
        <ProtectedForm submitLabel="Get new codes" busyLabel="Working…" onSubmit={newCodes} onCancel={() => setMode('idle')} />
      )}
      {mode === 'off-form' && (
        <>
          <p className={styles.copy}>
            {required
              ? 'This removes the app you use now; you set up the new phone right after.'
              : 'Signing in will need only your password.'}
          </p>
          <ProtectedForm danger submitLabel={required ? 'Remove and set up again' : 'Turn off'} busyLabel="Working…"
            onSubmit={turnOff} onCancel={() => setMode('idle')} />
        </>
      )}
      {mode === 'codes' && (
        <>
          <p className={styles.copy}>Your old recovery codes no longer work. Save these; Neoh will not show them again.</p>
          <ul className={styles.codes} aria-label="Recovery codes">
            {codes.map((c) => <li key={c}><code>{c}</code></li>)}
          </ul>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={() => { setCodes([]); setMode('idle'); }}>Done</button>
          </div>
        </>
      )}

      {isOwner && brokerage && (
        <label className={styles.checkRow}>
          <input
            type="checkbox"
            checked={brokerage.require_mfa === true}
            onChange={(e) => toggleBrokerage(e.target.checked)}
          />
          <span>Require two-step sign-in for everyone in my brokerage (owners always use it).</span>
        </label>
      )}
    </section>
  );
}

export default TwoStepSettings;
