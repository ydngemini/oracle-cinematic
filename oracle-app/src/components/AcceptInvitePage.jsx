import { useCallback, useEffect, useState } from 'react';

import { apiGet, apiPost } from '../lib/apiClient';
import styles from './BrokerageSetupPanel.module.css';

/**
 * The invitee's first screen.
 *
 * Unauthenticated by necessity — this person has no account yet, and the
 * token in the URL is the whole capability. It renders outside the auth shell
 * for the same reason PropertyUploadPage and SecureDossierPage do.
 *
 * They never type a tenant id, a brokerage code, or their own email: all three
 * come from the invitation the server already holds.
 */

function tokenFromUrl() {
  try {
    return new URLSearchParams(window.location.search).get('token') || '';
  } catch {
    return '';
  }
}

export function AcceptInvitePage() {
  const [token] = useState(tokenFromUrl);
  const [invite, setInvite] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [fullName, setFullName] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // A promise chain rather than async/await: setState must not run
  // synchronously inside the effect. The missing-token case is derived during
  // render (see `missing` below) instead of being stored, for the same reason.
  const load = useCallback(() => {
    if (!token) return Promise.resolve();
    return apiGet(`/auth/invitation?token=${encodeURIComponent(token)}`)
      .then(setInvite)
      .catch((err) => setLoadError(err?.message || 'This invitation link is not valid.'));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const accept = async () => {
    setBusy(true);
    setError('');
    try {
      await apiPost('/auth/accept-invite', { token, password, full_name: fullName.trim() });
      // The session cookie is set by the response; land them inside Neoh.
      window.location.assign('/');
    } catch (err) {
      setError(err?.message || 'Could not accept this invitation.');
      setBusy(false);
    }
  };

  const missing = !token ? 'This invitation link is missing its token.' : '';
  const problem = missing || loadError;

  if (problem) {
    return (
      <main className={styles.panel} style={{ maxWidth: '28rem', margin: '10vh auto', padding: '0 1rem' }}>
        <h1 className={styles.title}>This invitation cannot be used</h1>
        <p className={styles.error} role="alert">{problem}</p>
        <p className={styles.muted}>Ask whoever invited you to send a new one.</p>
      </main>
    );
  }

  if (!invite) {
    return (
      <main className={styles.panel} style={{ maxWidth: '28rem', margin: '10vh auto', padding: '0 1rem' }}>
        <p className={styles.muted}>Checking your invitation…</p>
      </main>
    );
  }

  // A dead invitation is reported with the reason, because "expired" and
  // "withdrawn" call for different next steps.
  if (invite.state !== 'pending') {
    const says = {
      accepted: 'This invitation has already been used. Sign in instead.',
      revoked: `${invite.brokerage} withdrew this invitation.`,
      expired: 'This invitation has expired. Ask for a new one.',
    }[invite.state] || 'This invitation cannot be used.';
    return (
      <main className={styles.panel} style={{ maxWidth: '28rem', margin: '10vh auto', padding: '0 1rem' }}>
        <h1 className={styles.title}>{invite.brokerage}</h1>
        <p className={styles.error} role="alert">{says}</p>
        <a className={styles.ghost} href="/">Go to sign in</a>
      </main>
    );
  }

  return (
    <main className={styles.panel} style={{ maxWidth: '28rem', margin: '10vh auto', padding: '0 1rem' }}>
      <h1 className={styles.title}>Join {invite.brokerage} on Neoh</h1>
      <p className={styles.muted}>
        {invite.invited_by} invited <strong>{invite.email}</strong>
        {invite.role === 'broker_owner' ? ' as an owner.' : ' as an agent.'}
      </p>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}

      <div className={styles.block}>
        <label className={styles.field}>
          <span>Your name</span>
          <input
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            maxLength={160}
            autoComplete="name"
          />
        </label>
        {/* The hint sits outside the label and is attached with
            aria-describedby: inside it, the field's accessible name becomes
            "Choose a passwordAt least 10 characters." */}
        <label className={styles.field}>
          <span>Choose a password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={10}
            autoComplete="new-password"
            aria-describedby="accept-invite-password-hint"
          />
        </label>
        <p id="accept-invite-password-hint" className={styles.muted}>At least 10 characters.</p>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.primary}
            onClick={accept}
            disabled={busy || password.length < 10 || !fullName.trim()}
          >
            {busy ? 'Joining…' : `Join ${invite.brokerage}`}
          </button>
        </div>
      </div>
    </main>
  );
}

export default AcceptInvitePage;
