import { useCallback, useEffect, useState } from 'react';

import { apiGet } from './apiClient';

// Delays before re-asking when /auth/session fails for a reason that is not
// "you are signed out" (429 after moving quickly, a 503, a dropped request).
export const SESSION_RETRY_MS = Object.freeze([1000, 3000, 6000]);

/** Only an authentication answer means signed out; anything else is unknown. */
export function isSignedOutAnswer(error) {
  return error?.status === 401 || error?.status === 403;
}

/**
 * Restore the signed-in session on load.
 *
 * Every failure of GET /auth/session used to read as "signed out": a single
 * 429 (the browser suite hit it while moving between views) put a person with
 * a perfectly valid cookie back on the sign-in form. Now only a 401/403 or an
 * explicit `authenticated: false` signs out; other failures retry, then say
 * Neoh cannot be reached and offer Try again.
 *
 * @returns {{ authed: boolean|null, unreachable: boolean, setAuthed: Function, retry: Function }}
 */
export function useSessionRestore({ bypass = false, onIdentity } = {}) {
  const [authed, setAuthed] = useState(bypass ? true : null);
  const [attempt, setAttempt] = useState(0);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    if (authed !== null || unreachable) return undefined;
    let live = true;
    let timer = 0;
    apiGet('/auth/session', { retries: 0 })
      .then((identity) => {
        if (!live) return;
        onIdentity?.(identity);
        setAuthed(Boolean(identity?.authenticated));
      })
      .catch((error) => {
        if (!live) return;
        if (isSignedOutAnswer(error)) { setAuthed(false); return; }
        if (attempt < SESSION_RETRY_MS.length) {
          timer = window.setTimeout(() => setAttempt((n) => n + 1), SESSION_RETRY_MS[attempt]);
        } else {
          setUnreachable(true);
        }
      });
    return () => { live = false; window.clearTimeout(timer); };
  }, [attempt, authed, onIdentity, unreachable]);

  const retry = useCallback(() => {
    setUnreachable(false);
    setAttempt(0);
    setAuthed(null);
  }, []);

  return { authed, unreachable, setAuthed, retry };
}
