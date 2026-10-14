import { useMemo, useState } from 'react';
import { apiPost, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';
import styles from './TwoStepSetup.module.css';

/**
 * QR modules from the server (rows of '0'/'1', backend/qr_code.py) drawn as
 * one SVG path with the standard 4-module quiet zone. No QR library.
 */
export function QrCode({ rows, label }) {
  const path = useMemo(() => {
    if (!Array.isArray(rows)) return '';
    const parts = [];
    rows.forEach((row, y) => {
      for (let x = 0; x < row.length; x += 1) {
        if (row[x] === '1') parts.push(`M${x + 4} ${y + 4}h1v1h-1z`);
      }
    });
    return parts.join('');
  }, [rows]);
  if (!path) return null;
  const size = rows.length + 8;
  return (
    <svg
      className={styles.qr}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect width={size} height={size} fill="#ffffff" />
      <path d={path} fill="#000000" />
    </svg>
  );
}

function downloadCodes(codes) {
  const text = [
    'Neoh two-step sign-in recovery codes',
    'Each code works once. Keep them somewhere safe and private.',
    '',
    ...codes,
    '',
  ].join('\n');
  try {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'neoh-recovery-codes.txt';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch { /* download unavailable — the codes stay on screen to copy */ }
}

/**
 * Turn on two-step sign-in with an authenticator app:
 *   1. POST /auth/mfa/totp/setup   → QR code + manual key (password only if
 *      the server asks: the sign-in was more than 10 minutes ago)
 *   2. POST /auth/mfa/totp/confirm → the first code; returns recovery codes
 *   3. show the recovery codes once; the person confirms they saved them
 * `onDone(result)` runs after step 3.
 */
export function TwoStepSetup({ onDone, onCancel, intro }) {
  const [phase, setPhase] = useState('start');
  const [setup, setSetup] = useState(null);
  const [password, setPassword] = useState('');
  const [needPassword, setNeedPassword] = useState(false);
  const [code, setCode] = useState('');
  const [codes, setCodes] = useState([]);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);

  async function begin(event) {
    event?.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const body = needPassword ? { password } : {};
      const payload = await apiPost('/auth/mfa/totp/setup', body, { retries: 0 });
      setSetup(payload);
      setPassword('');
      setPhase('scan');
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && !needPassword) {
        setNeedPassword(true);
        setError('For your security, enter your password to start.');
      } else {
        setError(err instanceof ApiError ? formatApiError(err) : 'Could not start two-step setup.');
      }
    } finally {
      setBusy(false);
    }
  }

  async function confirm(event) {
    event.preventDefault();
    if (busy || code.length !== 6) return;
    setBusy(true);
    setError('');
    try {
      const payload = await apiPost('/auth/mfa/totp/confirm', { code }, { retries: 0 });
      setCodes(Array.isArray(payload.recovery_codes) ? payload.recovery_codes : []);
      setResult(payload);
      setPhase('codes');
    } catch (err) {
      setCode('');
      setError(err instanceof ApiError ? formatApiError(err) : 'Could not check that code.');
    } finally {
      setBusy(false);
    }
  }

  if (phase === 'start') {
    return (
      <form className={styles.flow} onSubmit={begin}>
        {intro && <p className={styles.copy}>{intro}</p>}
        <ol className={styles.steps}>
          <li>Install an authenticator app on your phone (for example Google Authenticator, Microsoft Authenticator, 1Password or Authy).</li>
          <li>Scan the code Neoh shows you, or type the key.</li>
          <li>Enter the 6-digit code the app shows, then save your recovery codes.</li>
        </ol>
        {needPassword && (
          <label className={styles.field}>
            <span className={styles.label}>Password</span>
            <input
              className={styles.input}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={busy}
            />
          </label>
        )}
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.actions}>
          <button type="submit" className={styles.primary} disabled={busy || (needPassword && !password)}>
            {busy ? 'Starting…' : 'Set up authenticator app'}
          </button>
          {onCancel && <button type="button" className={styles.tertiary} onClick={onCancel} disabled={busy}>Cancel</button>}
        </div>
      </form>
    );
  }

  if (phase === 'scan') {
    return (
      <form className={styles.flow} onSubmit={confirm}>
        <p className={styles.copy}>Scan this with your authenticator app.</p>
        <div className={styles.qrWrap}>
          <QrCode rows={setup?.qr} label="QR code for your authenticator app" />
        </div>
        <p className={styles.copy}>
          On this phone? <a className={styles.link} href={setup?.otpauth_uri}>Open in your authenticator app</a>.
        </p>
        <div className={styles.keyBox}>
          <span className={styles.label}>Or type this key</span>
          <code className={styles.key} data-testid="totp-manual-key">{setup?.secret}</code>
          <span className={styles.hint}>Account: {setup?.account} · time-based · 6 digits</span>
        </div>
        <label className={styles.field}>
          <span className={styles.label}>Code from the app</span>
          <input
            className={styles.input}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="123456"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            disabled={busy}
            aria-describedby="two-step-code-hint"
          />
        </label>
        <span id="two-step-code-hint" className={styles.hint}>A new code appears every 30 seconds.</span>
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.actions}>
          <button type="submit" className={styles.primary} disabled={busy || code.length !== 6}>
            {busy ? 'Checking…' : 'Turn on two-step sign-in'}
          </button>
          {onCancel && <button type="button" className={styles.tertiary} onClick={onCancel} disabled={busy}>Cancel</button>}
        </div>
      </form>
    );
  }

  return (
    <div className={styles.flow}>
      <p className={styles.success} role="status">Two-step sign-in is on.</p>
      <p className={styles.copy}>
        Save these recovery codes. Each one signs you in once if you lose your phone. This is the only time Neoh shows them.
      </p>
      <ul className={styles.codes} aria-label="Recovery codes">
        {codes.map((c) => <li key={c}><code>{c}</code></li>)}
      </ul>
      <div className={styles.actions}>
        <button type="button" className={styles.secondary} onClick={() => downloadCodes(codes)}>Download codes</button>
        <button
          type="button"
          className={styles.secondary}
          onClick={() => { try { navigator.clipboard?.writeText(codes.join('\n')); } catch { /* copy unavailable */ } }}
        >
          Copy codes
        </button>
      </div>
      <label className={styles.checkRow}>
        <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />
        <span>I saved my recovery codes somewhere safe.</span>
      </label>
      <div className={styles.actions}>
        <button type="button" className={styles.primary} disabled={!saved} onClick={() => onDone?.(result)}>
          Continue
        </button>
      </div>
    </div>
  );
}

export default TwoStepSetup;
