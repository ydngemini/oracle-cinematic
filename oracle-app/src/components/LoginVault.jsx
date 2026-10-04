import { useState, useRef } from 'react';
import styles from './LoginVault.module.css';
import { apiPost, ApiError } from '../lib/apiClient';
import { useNetwork } from '../context/useNetwork';

const TITLES = { login: 'Sign in', signup: 'Create your account', forgot: 'Reset your password', reset: 'Set a new password' };
const BAD_CREDENTIALS = 'That email and password don’t match. Try again, or reset your password.';
const CTAS = { login: 'Sign in', signup: 'Create account', forgot: 'Send reset link', reset: 'Set password & sign in' };

/**
 * LoginVault — full-screen auth gate. Supports four modes:
 *   login  → POST /auth/login   (agent_id/email + passphrase)
 *   signup → POST /auth/register (self-serve broker signup → new tenant)
 *   forgot → POST /auth/forgot   (emails a reset link)
 *   reset  → POST /auth/reset    (auto-entered when the URL has ?reset=<token>)
 * On success the API installs an HttpOnly session cookie, then this gate fades out and calls
 * onAuthenticated() so the parent mounts the dashboard.
 */
export function LoginVault({ onAuthenticated }) {
  const { formatError } = useNetwork();
  const initialReset = (() => {
    try { return new URLSearchParams(window.location.search).get('reset') || ''; } catch { return ''; }
  })();
  const [mode, setMode] = useState(initialReset ? 'reset' : 'login');
  const [resetToken] = useState(initialReset);

  const [agentId, setAgentId] = useState('');
  const [passphrase, setPassphrase] = useState('');
  // The operator account's second factor: shown only after the server answers
  // OTP_REQUIRED for a correct passphrase.
  const [otp, setOtp] = useState('');
  const [otpRequired, setOtpRequired] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [company, setCompany] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);
  const [fading, setFading] = useState(false);
  const overlayRef = useRef(null);

  function finishAuth(data) {
    if (data.role) sessionStorage.setItem('oracle_role', data.role);
    if (data.agent_id) localStorage.setItem('oracle_user_id', data.agent_id);
    if (data.tenant_id) localStorage.setItem('oracle_tenant_id', data.tenant_id);
    // Strip ?reset= so a refresh doesn't re-trigger reset mode.
    if (window.location.search) window.history.replaceState({}, '', window.location.pathname);
    setLoading(false);
    setFading(true);
    // Mount the dashboard on the fade's end — but NEVER depend on transitionend
    // alone: if the overlay has no transition (reduced-motion, or the fade class
    // doesn't animate), the event never fires and the screen hangs after auth.
    // A timeout fallback guarantees we proceed. Guarded so it runs exactly once.
    let done = false;
    const finish = () => { if (done) return; done = true; onAuthenticated?.(); };
    const overlay = overlayRef.current;
    if (overlay) overlay.addEventListener('transitionend', finish, { once: true });
    setTimeout(finish, 600);
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (loading || fading) return;
    setError(''); setNotice(''); setLoading(true);
    try {
      if (mode === 'login') {
        const payload = otpRequired && otp ? { agent_id: agentId, passphrase, otp } : { agent_id: agentId, passphrase };
        const body = await apiPost('/auth/login', payload, { retries: 0 });
        finishAuth(body);
      } else if (mode === 'signup') {
        const body = await apiPost('/auth/register', { email, password, full_name: fullName, company }, { retries: 0 });
        finishAuth(body);
      } else if (mode === 'forgot') {
        const body = await apiPost('/auth/forgot', { email }, { retries: 0 });
        setLoading(false);
        setNotice(body.detail ?? 'If that email has an account, a reset link is on its way.');
      } else {
        const body = await apiPost('/auth/reset', { token: resetToken, new_password: password }, { retries: 0 });
        finishAuth(body);
      }
    } catch (err) {
      setLoading(false);
      if (err instanceof ApiError && (err.code === 'OTP_REQUIRED' || err.code === 'OTP_INVALID')) {
        setOtpRequired(true);
        setOtp('');
        setError(err.code === 'OTP_INVALID' ? err.message : '');
        setNotice(err.code === 'OTP_REQUIRED' ? err.message : '');
      } else if (err instanceof ApiError && mode === 'login' && err.status === 401) {
        // A wrong password is not an ended session; the generic 401 copy told
        // someone who had just typed their password to "sign in again".
        setError(BAD_CREDENTIALS);
      } else if (err instanceof ApiError) {
        setError(formatError(err));
      } else {
        setError('Couldn’t reach Neoh. Check your connection and try again.');
      }
    }
  }

  // Emailed codes expire and retire after five wrong tries; signing in again
  // without a code asks the server for a fresh one.
  const resendCode = async () => {
    setError(''); setNotice(''); setOtp(''); setLoading(true);
    try {
      finishAuth(await apiPost('/auth/login', { agent_id: agentId, passphrase }, { retries: 0 }));
    } catch (err) {
      setLoading(false);
      if (err instanceof ApiError && err.code === 'OTP_REQUIRED') setNotice(err.message);
      else setError(err instanceof ApiError ? formatError(err) : 'Couldn’t reach Neoh. Check your connection and try again.');
    }
  };

  const busy = loading || fading;
  const disabled = busy
    || (mode === 'login' && (!agentId || !passphrase || (otpRequired && otp.length !== 6)))
    || (mode === 'signup' && (!email || password.length < 10))
    || (mode === 'forgot' && !email)
    || (mode === 'reset' && password.length < 10);

  const switchTo = (m) => () => { setMode(m); setError(''); setNotice(''); };
  const navLink = (to, label) => <button type="button" className={styles.linkBtn} onClick={switchTo(to)}>{label}</button>;

  const field = (id, label, value, set, type, auto, ph) => (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>{label}</label>
      <input id={id} className={styles.input} type={type} autoComplete={auto} spellCheck={false}
        value={value} onChange={(e) => set(e.target.value)} disabled={busy} placeholder={ph} />
    </div>
  );

  return (
    <div ref={overlayRef} className={`${styles.overlay} ${fading ? styles.fadeOut : ''}`}>
      <div className={styles.panel}>
        <h1 className={styles.wordmark}>
          <span className={styles.wordmarkKicker}>Neoh</span>
          <span className={styles.wordmarkSub}>{TITLES[mode]}</span>
        </h1>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          {mode === 'login' && (
            <>
              {field('agent-id', 'Email or Agent ID', agentId, setAgentId, 'text', 'username', 'you@brokerage.com')}
              {field('passphrase', 'Password', passphrase, setPassphrase, 'password', 'current-password', '••••••••')}
              {otpRequired && field('otp', 'Sign-in code', otp, (v) => setOtp(v.replace(/\D/g, '').slice(0, 6)), 'text', 'one-time-code', '123456')}
              {otpRequired && <button type="button" className={styles.linkBtn} onClick={resendCode} disabled={busy}>Send a new code</button>}
            </>
          )}
          {mode === 'signup' && (
            <>
              {field('full-name', 'Your name', fullName, setFullName, 'text', 'name', 'Jane Broker')}
              {field('company', 'Brokerage (optional)', company, setCompany, 'text', 'organization', 'Acme Realty')}
              {field('email', 'Email', email, setEmail, 'email', 'username', 'you@brokerage.com')}
              {field('password', 'Password (10+ chars)', password, setPassword, 'password', 'new-password', '••••••••••')}
            </>
          )}
          {mode === 'forgot' && field('email', 'Email', email, setEmail, 'email', 'username', 'you@brokerage.com')}
          {mode === 'reset' && field('password', 'New password (10+ chars)', password, setPassword, 'password', 'new-password', '••••••••••')}

          {error && <p className={styles.errorMsg} role="alert">{error}</p>}
          {notice && <p className={styles.noticeMsg} role="status">{notice}</p>}

          <button type="submit" className={styles.submitBtn} disabled={disabled}>
            {loading ? <span className={styles.loadingDots} role="status" aria-label="Working…"><span /><span /><span /></span> : CTAS[mode]}
          </button>
        </form>

        <p className={styles.clearanceNote}>
          {mode === 'login' && <>New broker? {navLink('signup', 'Create an account')} · {navLink('forgot', 'Forgot password?')}</>}
          {mode === 'signup' && <>Already have an account? {navLink('login', 'Sign in')}</>}
          {mode === 'forgot' && <>Remembered it? {navLink('login', 'Back to sign in')}</>}
          {mode === 'reset' && navLink('login', 'Back to sign in')}
        </p>
      </div>
    </div>
  );
}
