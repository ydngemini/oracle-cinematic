import { useState, useRef } from 'react';
import styles from './LoginVault.module.css';

// Same base-URL convention as BillingOverlay/OnboardingGate — there is no Vite
// proxy, so a relative /auth/* 404s in any deployed build.
const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

const TITLES = { login: 'Command Center', signup: 'Create account', forgot: 'Reset password', reset: 'Set new password' };
const CTAS = { login: 'Authenticate', signup: 'Create account', forgot: 'Send reset link', reset: 'Set password & sign in' };

/**
 * LoginVault — full-screen auth gate. Supports four modes:
 *   login  → POST /auth/login   (agent_id/email + passphrase)
 *   signup → POST /auth/register (self-serve broker signup → new tenant)
 *   forgot → POST /auth/forgot   (emails a reset link)
 *   reset  → POST /auth/reset    (auto-entered when the URL has ?reset=<token>)
 * On success it stores the JWT in sessionStorage, fades out, and calls
 * onAuthenticated() so the parent mounts the dashboard.
 */
export function LoginVault({ onAuthenticated }) {
  // Lazy init from the URL so a ?reset=<token> link opens the reset form
  // directly — done in the initializer (not an effect) to avoid setState-in-effect.
  const initialReset = (() => {
    try { return new URLSearchParams(window.location.search).get('reset') || ''; } catch { return ''; }
  })();
  const [mode, setMode] = useState(initialReset ? 'reset' : 'login');
  const [resetToken] = useState(initialReset);

  const [agentId, setAgentId] = useState('');
  const [passphrase, setPassphrase] = useState('');
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
    sessionStorage.setItem('oracle_token', data.token);
    if (data.role) sessionStorage.setItem('oracle_role', data.role);
    if (data.agent_id) localStorage.setItem('oracle_user_id', data.agent_id);
    if (data.tenant_id) localStorage.setItem('oracle_tenant_id', data.tenant_id);
    // Strip ?reset= so a refresh doesn't re-trigger reset mode.
    if (window.location.search) window.history.replaceState({}, '', window.location.pathname);
    setFading(true);
    setLoading(false);
    const overlay = overlayRef.current;
    const onEnd = () => { overlay.removeEventListener('transitionend', onEnd); onAuthenticated?.(); };
    overlay.addEventListener('transitionend', onEnd);
  }

  async function post(path, payload) {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    return { ok: res.ok, body };
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (loading || fading) return;
    setError(''); setNotice(''); setLoading(true);
    try {
      if (mode === 'login') {
        const { ok, body } = await post('/auth/login', { agent_id: agentId, passphrase });
        if (!ok) { setError(body.detail ?? 'Authentication failed.'); setLoading(false); return; }
        finishAuth(body);
      } else if (mode === 'signup') {
        const { ok, body } = await post('/auth/register', { email, password, full_name: fullName, company });
        if (!ok) { setError(body.detail ?? 'Could not create account.'); setLoading(false); return; }
        finishAuth(body);
      } else if (mode === 'forgot') {
        const { ok, body } = await post('/auth/forgot', { email });
        setLoading(false);
        if (!ok) { setError(body.detail ?? 'Could not send reset link.'); return; }
        setNotice(body.detail ?? 'If that email has an account, a reset link is on its way.');
      } else {
        const { ok, body } = await post('/auth/reset', { token: resetToken, new_password: password });
        if (!ok) { setError(body.detail ?? 'Could not reset password.'); setLoading(false); return; }
        finishAuth(body);
      }
    } catch {
      setError('Network error — backend unreachable.');
      setLoading(false);
    }
  }

  const busy = loading || fading;
  const disabled = busy
    || (mode === 'login' && (!agentId || !passphrase))
    || (mode === 'signup' && (!email || password.length < 10))
    || (mode === 'forgot' && !email)
    || (mode === 'reset' && password.length < 10);

  const switchTo = (m) => () => { setMode(m); setError(''); setNotice(''); };
  const linkStyle = { background: 'none', border: 'none', color: 'var(--oracle-amber, #d8a657)', cursor: 'pointer', textDecoration: 'underline', font: 'inherit', padding: 0 };
  const navLink = (to, label) => <button type="button" style={linkStyle} onClick={switchTo(to)}>{label}</button>;

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
        <div className={styles.wordmark}>
          <span className={styles.wordmarkKicker}>Neoh</span>
          <span className={styles.wordmarkDivider} />
          <span className={styles.wordmarkSub}>{TITLES[mode]}</span>
        </div>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          {mode === 'login' && (
            <>
              {field('agent-id', 'Email or Agent ID', agentId, setAgentId, 'text', 'username', 'you@brokerage.com')}
              {field('passphrase', 'Passphrase', passphrase, setPassphrase, 'password', 'current-password', '••••••••')}
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
          {notice && <p className={styles.errorMsg} role="status" style={{ color: 'var(--oracle-amber, #d8a657)' }}>{notice}</p>}

          <button type="submit" className={styles.submitBtn} disabled={disabled}>
            {loading ? <span className={styles.loadingDots}><span /><span /><span /></span> : CTAS[mode]}
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
