import { Suspense, lazy, useEffect, useState } from 'react';
import { apiGet, ApiError } from '../lib/apiClient';
import { formatApiError } from '../lib/errorMessages';
// Loaded only when a session actually owes setup.
const TwoStepSetup = lazy(() => import('./TwoStepSetup'));
import styles from './TwoStepGate.module.css';

export const MFA_ENROLMENT_EVENT = 'auth:mfa-enrolment-required';

/**
 * After the policy gate: a session that signed in without the two-step
 * sign-in its account must use (broker owners; everyone, when the brokerage
 * requires it) is held here. The API answers every other route 403
 * `mfa_enrolment_required` until setup is done (backend tenancy.py), so this
 * screen is the only way forward — or sign out.
 */
export function TwoStepGate({ onReady, onSignOut }) {
  const [phase, setPhase] = useState('loading');
  const [reason, setReason] = useState(null);
  const [error, setError] = useState('');
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let active = true;
    apiGet('/auth/mfa', { retries: 1 })
      .then((status) => {
        if (!active) return;
        if (status?.enrolment_required === true) {
          setReason(status.required_reason || null);
          setPhase('required');
        } else {
          setPhase('resolved');
          onReady?.();
        }
      })
      .catch((err) => {
        if (!active) return;
        // An older API without the route, or a network blip: the server still
        // enforces the requirement, so the app shows its own 403s if it is owed.
        if (err instanceof ApiError && err.status === 404) {
          setPhase('resolved');
          onReady?.();
          return;
        }
        setError(err instanceof ApiError ? formatApiError(err) : 'Could not check your sign-in settings.');
        setPhase('error');
      });
    return () => { active = false; };
  }, [onReady, retryKey]);

  useEffect(() => {
    const reopen = () => { setReason(null); setPhase('required'); };
    window.addEventListener(MFA_ENROLMENT_EVENT, reopen);
    return () => window.removeEventListener(MFA_ENROLMENT_EVENT, reopen);
  }, []);

  if (phase === 'resolved') return null;
  if (phase === 'loading') {
    return <div className={styles.loading} role="status" aria-live="polite">Checking sign-in security…</div>;
  }
  if (phase === 'error') {
    return (
      <section className={styles.overlay} role="dialog" aria-modal="true" aria-labelledby="two-step-error-title">
        <div className={styles.panel}>
          <p className={styles.eyebrow}>Account security</p>
          <h1 id="two-step-error-title">Sign-in security could not be checked</h1>
          <p className={styles.error} role="alert">{error}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.tertiary} onClick={() => setRetryKey((k) => k + 1)}>Try again</button>
            <button type="button" className={styles.tertiary} onClick={onSignOut}>Sign out</button>
          </div>
        </div>
      </section>
    );
  }

  const why = reason === 'brokerage'
    ? 'Your brokerage requires two-step sign-in for everyone.'
    : 'Brokerage owners must use two-step sign-in: your account can reach every client record in the brokerage.';

  return (
    <section className={styles.overlay} role="dialog" aria-modal="true" aria-labelledby="two-step-title">
      <div className={styles.panel}>
        <p className={styles.eyebrow}>Account security</p>
        <h1 id="two-step-title">Set up two-step sign-in</h1>
        <p className={styles.copy}>
          {why} After this, signing in asks for your password and a 6-digit code from an app on your phone.
        </p>
        <Suspense fallback={<p className={styles.copy} role="status">Loading…</p>}>
          <TwoStepSetup onDone={() => { setPhase('resolved'); onReady?.(); }} />
        </Suspense>
        <div className={styles.actions}>
          <button type="button" className={styles.tertiary} onClick={onSignOut}>Sign out</button>
        </div>
      </div>
    </section>
  );
}

export default TwoStepGate;
