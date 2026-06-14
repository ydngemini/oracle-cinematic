import { useState, useRef } from 'react';
import styles from './LoginVault.module.css';

// Same base-URL convention as BillingOverlay/OnboardingGate — there is no
// Vite proxy, so a relative /auth/login 404s in any deployed build.
const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

/**
 * LoginVault
 *
 * Full-screen auth gate. Fetches POST /auth/login, stores the returned JWT
 * in sessionStorage under the key "oracle_token", then fades out and calls
 * onAuthenticated() so the parent can unmount this screen and mount the
 * dashboard.
 *
 * Props
 * ─────
 *   onAuthenticated  function()  — called after the fade-out completes
 */
export function LoginVault({ onAuthenticated }) {
  const [agentId, setAgentId]       = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [error, setError]           = useState('');
  const [loading, setLoading]       = useState(false);
  const [fading, setFading]         = useState(false);

  const overlayRef = useRef(null);

  async function handleSubmit(e) {
    e.preventDefault();
    if (loading || fading) return;

    setError('');
    setLoading(true);

    try {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ agent_id: agentId, passphrase }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail ?? 'Authentication failed.');
        setLoading(false);
        return;
      }

      const { token, agent_id, tenant_id, role } = await res.json();
      sessionStorage.setItem('oracle_token', token);
      // Role gates role-specific surfaces (the platform-admin OPS tab);
      // identity keys align the WS + profile panels with the JWT identity
      // (identity.js resolution order: env → localStorage → demo default).
      if (role) sessionStorage.setItem('oracle_role', role);
      if (agent_id) localStorage.setItem('oracle_user_id', agent_id);
      if (tenant_id) localStorage.setItem('oracle_tenant_id', tenant_id);

      // Trigger the fade-out, then hand off to parent after transition ends
      setFading(true);
      setLoading(false);

      const overlay = overlayRef.current;
      const onEnd = () => {
        overlay.removeEventListener('transitionend', onEnd);
        onAuthenticated?.();
      };
      overlay.addEventListener('transitionend', onEnd);

    } catch {
      setError('Network error — backend unreachable.');
      setLoading(false);
    }
  }

  return (
    <div
      ref={overlayRef}
      className={`${styles.overlay} ${fading ? styles.fadeOut : ''}`}
    >
      <div className={styles.panel}>
        {/* Wordmark */}
        <div className={styles.wordmark}>
          <span className={styles.wordmarkKicker}>Oracle</span>
          <span className={styles.wordmarkDivider} />
          <span className={styles.wordmarkSub}>Command Center</span>
        </div>

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="agent-id">
              Agent ID
            </label>
            <input
              id="agent-id"
              className={styles.input}
              type="text"
              autoComplete="username"
              spellCheck={false}
              value={agentId}
              onChange={e => setAgentId(e.target.value)}
              disabled={loading || fading}
              placeholder="agent_id"
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="passphrase">
              Passphrase
            </label>
            <input
              id="passphrase"
              className={styles.input}
              type="password"
              autoComplete="current-password"
              value={passphrase}
              onChange={e => setPassphrase(e.target.value)}
              disabled={loading || fading}
              placeholder="••••••••"
            />
          </div>

          {error && (
            <p className={styles.errorMsg} role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            className={styles.submitBtn}
            disabled={loading || fading || !agentId || !passphrase}
          >
            {loading ? (
              <span className={styles.loadingDots}>
                <span />
                <span />
                <span />
              </span>
            ) : (
              'Authenticate'
            )}
          </button>
        </form>

        <p className={styles.clearanceNote}>
          Restricted access — authorised agents only
        </p>
      </div>
    </div>
  );
}
